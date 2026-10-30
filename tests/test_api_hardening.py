import io
import json
import logging
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

pytest.importorskip("httpx")
import requests  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import analytics, api  # noqa: E402
from src.analytics import AnalyticsUnavailable, clear_analytics_cache  # noqa: E402
from src.auth import JwksKeyProvider  # noqa: E402
from src.logging_config import JsonFormatter  # noqa: E402
from src.run_store import (  # noqa: E402
    PUBLIC_ERROR_MESSAGES,
    RunStore,
    RunStoreError,
    classify_error,
    to_public_run,
)
from src.settings import Settings, SettingsError  # noqa: E402
from tests import auth_helpers as h  # noqa: E402

VIEWER = h.auth_header(h.token(groups=["viewer"]))
BUCKET = "irish-energy-data-platform-123456789012"
SECRET_KEY = "gold/eirgrid/daily_demand/run_date=2026-10-29/daily_demand.parquet"


def make_client(**kwargs):
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    app = api.create_app(Settings(api_env="local", cognito=h.CONFIG), key_provider=provider)
    return TestClient(app, **kwargs)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    run_store = RunStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(api, "_store", run_store)
    clear_analytics_cache()
    yield run_store
    clear_analytics_cache()


# Run-history sanitization


def _failed_run(store):
    run = store.create_run(2)
    store.update_run(
        run["run_id"],
        status="partial_failure",
        stage="glue_registration",
        bronze_key=f"bronze/{BUCKET}/a.json",
        silver_key="silver/b.parquet",
        gold_key=SECRET_KEY,
        error=f"ClientError: arn:aws:s3:::{BUCKET}/{SECRET_KEY} denied",
        error_code="ACCESS_DENIED",
        started_at="2026-10-29T10:00:00+00:00",
        finished_at="2026-10-29T10:01:00+00:00",
    )
    return run["run_id"]


def test_run_history_hides_keys_buckets_and_raw_errors(store):
    run_id = _failed_run(store)
    client = make_client()

    listing = client.get("/api/runs", headers=VIEWER)
    single = client.get(f"/api/runs/{run_id}", headers=VIEWER)

    for response in (listing, single):
        text = response.text
        assert response.status_code == 200
        assert BUCKET not in text
        assert "arn:aws" not in text
        assert SECRET_KEY not in text
        assert "bronze_key" not in text and "gold_key" not in text
    run = single.json()
    assert run["error_code"] == "ACCESS_DENIED"
    assert run["error"] == PUBLIC_ERROR_MESSAGES["ACCESS_DENIED"]
    assert {"run_id", "status", "stage", "days_back", "created_at"} <= set(run)
    assert listing.json()["runs"][0] == run


def test_internal_store_keeps_full_detail_for_diagnostics(store):
    run_id = _failed_run(store)

    internal = store.get_run(run_id)

    assert internal["gold_key"] == SECRET_KEY
    assert BUCKET in internal["error"]


def test_successful_runs_have_no_error_fields(store):
    run = store.create_run(2)
    store.update_run(run["run_id"], status="succeeded", stage="completed")

    public = to_public_run(store.get_run(run["run_id"]))

    assert "error" not in public and "error_code" not in public


def test_unknown_error_codes_fall_back_to_generic(store):
    public = to_public_run({"run_id": "r", "error": "raw", "error_code": "SOMETHING_ELSE"})

    assert public["error_code"] == "INTERNAL_ERROR"
    assert public["error"] == PUBLIC_ERROR_MESSAGES["INTERNAL_ERROR"]


def test_interrupted_runs_get_fixed_public_message(store):
    run = store.create_run(2)
    store.recover_interrupted()

    public = to_public_run(store.get_run(run["run_id"]))

    assert public["status"] == "interrupted"
    assert public["error_code"] == "INTERRUPTED"
    assert "outcome is unknown" in public["error"]


def test_existing_database_is_migrated_with_error_code(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " stage TEXT NOT NULL, days_back INTEGER NOT NULL, created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,"
            " bronze_key TEXT, silver_key TEXT, gold_key TEXT, error TEXT)"
        )
        conn.execute(
            "INSERT INTO runs VALUES ('r1','failed','bronze',2,'t','t',NULL,NULL,NULL,NULL,NULL,'boom')"
        )

    migrated = RunStore(path)

    assert migrated.update_run("r1", error_code="STORAGE_ERROR")
    assert migrated.get_run("r1")["error_code"] == "STORAGE_ERROR"


@pytest.mark.parametrize(
    "exc,code",
    [
        (ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"), "ACCESS_DENIED"),
        (ClientError({"Error": {"Code": "ExpiredToken"}}, "PutObject"), "ACCESS_DENIED"),
        (PermissionError("nope"), "ACCESS_DENIED"),
        (ClientError({"Error": {"Code": "SlowDown"}}, "PutObject"), "UPSTREAM_UNAVAILABLE"),
        (requests.ConnectionError("down"), "UPSTREAM_UNAVAILABLE"),
        (sqlite3.OperationalError("locked"), "STORAGE_ERROR"),
        (RunStoreError("x"), "STORAGE_ERROR"),
        (ValueError("bad data"), "VALIDATION_FAILED"),
        (RuntimeError("anything"), "INTERNAL_ERROR"),
    ],
)
def test_classify_error(exc, code):
    assert classify_error(exc) == code


def test_worker_records_error_code_and_public_view_stays_generic(store, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    run = store.create_run(2)

    with (
        patch("src.ingestion.eirgrid.fetch_demand_data", return_value={"Rows": []}),
        patch(
            "src.ingestion.eirgrid.save_to_s3",
            side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"),
        ),
    ):
        api.execute_pipeline(run["run_id"], 2)

    internal = store.get_run(run["run_id"])
    assert internal["error_code"] == "ACCESS_DENIED"
    assert internal["status"] == "failed"
    public = make_client().get(f"/api/runs/{run['run_id']}", headers=VIEWER).json()
    assert public["error"] == PUBLIC_ERROR_MESSAGES["ACCESS_DENIED"]
    assert "PutObject" not in json.dumps(public)


# Analytics sanitization, validation and caching


def _s3_access_denied():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": f"denied on arn:aws:s3:::{BUCKET}"}},
        "ListObjectsV2",
    )
    return s3


def test_analytics_failure_returns_generic_503_without_aws_details(monkeypatch, caplog):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    caplog.set_level(logging.ERROR)

    with patch("src.analytics.boto3.client", return_value=_s3_access_denied()):
        response = make_client().get("/api/analytics/daily-demand", headers=VIEWER)

    assert response.status_code == 503
    assert response.json() == {"detail": "Gold analytics are temporarily unavailable."}
    for leaked in (BUCKET, "AccessDenied", "arn:aws", "ListObjectsV2"):
        assert leaked not in response.text
    assert "AccessDenied" in caplog.text  # diagnostic detail stays server-side


@pytest.mark.parametrize(
    "detail",
    [
        "S3_BUCKET is not configured on the API.",
        f"Gold object {SECRET_KEY} is missing columns: ['date']",
        f"Unable to read Gold data from S3 (AccessDenied) {BUCKET}",
    ],
)
def test_every_analytics_failure_message_is_generic(detail):
    with patch("src.analytics.get_daily_demand", side_effect=AnalyticsUnavailable(detail)):
        response = make_client().get("/api/analytics/daily-demand", headers=VIEWER)

    assert response.json() == {"detail": "Gold analytics are temporarily unavailable."}


def test_analytics_failures_are_not_cached_and_do_not_poison_later_reads(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    good = MagicMock()
    good.get_paginator.return_value.paginate.return_value = [{}]
    client = make_client()

    with patch("src.analytics.boto3.client", side_effect=[_s3_access_denied(), good]):
        first = client.get("/api/analytics/daily-demand", headers=VIEWER)
        second = client.get("/api/analytics/daily-demand", headers=VIEWER)

    assert (first.status_code, second.status_code) == (503, 200)
    assert second.json()["days"] == []


def test_analytics_success_exposes_no_bucket_name(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{}]

    with patch("src.analytics.boto3.client", return_value=s3):
        body = make_client().get("/api/analytics/daily-demand", headers=VIEWER).json()

    assert body["source"] == "gold/eirgrid/daily_demand"
    assert BUCKET not in json.dumps(body)
    assert set(body) == {"source", "expected_interval_minutes", "days"}


@pytest.mark.parametrize("days", ["0", "-1", "91", "abc", "1.5", ""])
def test_invalid_days_rejected_before_reading_s3(days):
    with patch("src.analytics.get_daily_demand") as read:
        response = make_client().get(f"/api/analytics/daily-demand?days={days}", headers=VIEWER)

    assert response.status_code == 422
    read.assert_not_called()


def test_cache_entries_are_bounded_by_the_days_range():
    with patch("src.analytics.read_gold_daily_demand", return_value={"source": "s", "days": []}):
        for days in range(1, 91):
            analytics.get_daily_demand(days)
            analytics.get_daily_demand(days)

    assert len(analytics._cache) == 90
    assert len(analytics._key_locks) == 90


@pytest.mark.parametrize(
    "value,expected",
    [("inf", 300.0), ("nan", 300.0), ("abc", 300.0), ("999999", 3600.0), ("60", 60.0)],
)
def test_cache_ttl_is_always_finite_and_capped(monkeypatch, value, expected):
    monkeypatch.setenv("ANALYTICS_CACHE_TTL_SECONDS", value)

    assert analytics._cache_ttl() == expected


# Error handling, headers and logging


def test_unhandled_errors_return_sanitized_500_with_request_id(caplog):
    client = make_client(raise_server_exceptions=False)
    secret = f"boom {BUCKET} secret=abc123"
    caplog.set_level(logging.ERROR)

    with patch("src.analytics.get_daily_demand", side_effect=RuntimeError(secret)):
        response = client.get("/api/analytics/daily-demand", headers=VIEWER)

    body = response.json()
    assert response.status_code == 500
    assert body == {"detail": "Internal server error", "request_id": response.headers["x-request-id"]}
    assert BUCKET not in response.text and "Traceback" not in response.text
    assert "RuntimeError" in caplog.text  # detail is logged server-side


def test_security_headers_on_success_and_error_responses():
    client = make_client()

    for response in (
        client.get("/health"),
        client.get("/api/runs"),
        client.get("/api/runs", headers=VIEWER),
        client.get("/no-such-route"),
    ):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-request-id"]


def test_request_ids_are_unique_and_never_taken_from_the_client():
    client = make_client()

    first = client.get("/health", headers={"X-Request-ID": "attacker-chosen"})
    second = client.get("/health")

    assert first.headers["x-request-id"] != "attacker-chosen"
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_cors_headers_are_present_on_500_responses():
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    settings = Settings(
        api_env="local",
        cognito=h.CONFIG,
        cors_allowed_origins=("https://app.example.com",),
    )
    client = TestClient(api.create_app(settings, key_provider=provider), raise_server_exceptions=False)

    with patch("src.analytics.get_daily_demand", side_effect=RuntimeError("x")):
        response = client.get(
            "/api/analytics/daily-demand",
            headers={**VIEWER, "Origin": "https://app.example.com"},
        )

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"


def test_request_log_has_context_and_never_the_token():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("src")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    bearer = h.token(groups=["viewer"])
    try:
        client = make_client()
        client.get("/api/runs", headers=h.auth_header(bearer))
        client.get("/api/runs", headers=h.auth_header(h.wrongly_signed()))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    output = stream.getvalue()
    lines = [json.loads(line) for line in output.splitlines()]
    requests_logged = [line for line in lines if line["message"].startswith("request ")]
    assert any("route=/api/runs status=200" in line["message"] and "sub=user-123" in line["message"] for line in requests_logged)
    assert any("status=401" in line["message"] for line in requests_logged)
    assert all(line["request_id"] != "-" for line in requests_logged)
    assert bearer not in output
    assert bearer.split(".")[1] not in output


# Production configuration


def test_importing_app_fails_clearly_in_production_without_cognito(monkeypatch):
    monkeypatch.setattr(api, "_app", None)
    monkeypatch.setenv("API_ENV", "production")

    with pytest.raises(SettingsError, match="requires COGNITO"):
        api.__getattr__("app")


def test_app_is_built_from_environment_in_local_mode(monkeypatch):
    monkeypatch.setattr(api, "_app", None)
    monkeypatch.setenv("API_ENV", "local")

    app = api.__getattr__("app")

    assert isinstance(app, FastAPI)
    assert api.__getattr__("app") is app
    assert app.state.verifier is None  # unconfigured auth fails closed, never open


def test_production_app_with_cognito_requires_tokens(monkeypatch):
    monkeypatch.setattr(api, "_app", None)
    monkeypatch.setenv("API_ENV", "production")
    monkeypatch.setenv("COGNITO_REGION", "eu-west-1")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "eu-west-1_AbC123xyz")
    monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "abcdefghij1234567890")
    monkeypatch.setenv("RUN_HISTORY_BACKEND", "dynamodb")
    monkeypatch.setenv("RUN_HISTORY_TABLE", "irish-energy-run-history")

    client = TestClient(api.__getattr__("app"))

    assert client.get("/health").status_code == 200
    assert client.get("/api/runs").status_code == 401
    assert client.get("/docs").status_code == 404


def test_unknown_module_attributes_still_raise():
    with pytest.raises(AttributeError):
        api.__getattr__("nope")
