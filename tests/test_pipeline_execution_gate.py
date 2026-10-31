from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from src import api
from src.auth import JwksKeyProvider
from src.executor import DisabledExecutor, ExecutionDisabledError
from src.run_store import RunStore
from src.settings import Settings, load_settings
from tests import auth_helpers
from tests.auth_helpers import auth_header, token

PIPELINE_ENTRY_POINTS = [
    "src.api.execute_pipeline",
    "src.ingestion.eirgrid.fetch_demand_data",
    "src.ingestion.eirgrid.save_to_s3",
    "src.transformation.process_bronze_object",
    "src.gold.process_silver_object",
    "src.pipeline.register_gold_partition",
]
OPERATOR = auth_header(token(groups=["operator"]))
VIEWER = auth_header(token(groups=["viewer"]))


@pytest.fixture
def pipeline_spies():
    patches = [patch(target) for target in PIPELINE_ENTRY_POINTS]
    spies = [p.start() for p in patches]
    try:
        yield spies
    finally:
        for p in patches:
            p.stop()


def _no_run_records(tmp_path):
    return RunStore(tmp_path / "guard.sqlite3").list_runs() == []


def test_default_executor_is_disabled():
    assert isinstance(api.get_executor(), DisabledExecutor)
    with pytest.raises(ExecutionDisabledError):
        api.get_executor().start(2)


def test_operator_post_returns_503_and_starts_nothing(auth_app, pipeline_spies, tmp_path):
    with patch("src.api.threading.Thread") as thread:
        response = auth_app.post("/api/runs", json={"days_back": 2}, headers=OPERATOR)

    assert response.status_code == 503
    assert response.json() == {"detail": "Pipeline execution is disabled."}
    thread.assert_not_called()
    for spy in pipeline_spies:
        spy.assert_not_called()
    assert _no_run_records(tmp_path)


def test_operator_post_default_body_is_also_disabled(auth_app, pipeline_spies):
    response = auth_app.post("/api/runs", json={}, headers=OPERATOR)

    assert response.status_code == 503
    assert response.json() == {"detail": "Pipeline execution is disabled."}
    for spy in pipeline_spies:
        spy.assert_not_called()


@pytest.mark.parametrize(
    "headers,expected_status",
    [({}, 401), (VIEWER, 403), (auth_header("garbage"), 401)],
)
def test_unauthorized_post_never_invokes_pipeline(
    auth_app, pipeline_spies, tmp_path, headers, expected_status
):
    response = auth_app.post("/api/runs", json={"days_back": 2}, headers=headers)

    assert response.status_code == expected_status
    for spy in pipeline_spies:
        spy.assert_not_called()
    assert _no_run_records(tmp_path)


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"json": {"days_back": 0}},
        {"json": {"days_back": 31}},
        {"json": {"days_back": "abc"}},
        {"content": "not json", "headers": {"Content-Type": "text/plain"}},
        {"content": b""},
    ],
)
def test_invalid_operator_requests_cannot_invoke_pipeline(
    auth_app, pipeline_spies, tmp_path, request_kwargs
):
    headers = {**request_kwargs.pop("headers", {}), **OPERATOR}

    response = auth_app.post("/api/runs", headers=headers, **request_kwargs)

    assert response.status_code == 422
    for spy in pipeline_spies:
        spy.assert_not_called()
    assert _no_run_records(tmp_path)


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_methods_on_runs_are_rejected(auth_app, pipeline_spies, method):
    response = getattr(auth_app, method)("/api/runs", headers=OPERATOR)

    assert response.status_code == 405
    for spy in pipeline_spies:
        spy.assert_not_called()


def test_get_requests_never_invoke_pipeline(auth_app, pipeline_spies):
    for path in ("/health", "/api/runs", "/api/runs/missing"):
        auth_app.get(path, headers=OPERATOR)

    for spy in pipeline_spies:
        spy.assert_not_called()


def test_no_role_or_request_hint_can_enable_execution(auth_app, pipeline_spies):
    attempts = [
        {"headers": {**OPERATOR, "X-Pipeline-Execution": "enabled"}},
        {"headers": OPERATOR, "params": {"enabled": "true", "role": "operator"}},
    ]
    for kwargs in attempts:
        assert auth_app.post("/api/runs", json={}, **kwargs).status_code == 503

    for spy in pipeline_spies:
        spy.assert_not_called()


def test_executor_dependency_is_the_only_path_to_execution(pipeline_spies):
    """A custom executor is reachable only by overriding the dependency."""
    fake = MagicMock()
    fake.start.return_value = {"run_id": "r", "status": "queued"}
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=auth_helpers.jwks)
    app = api.create_app(
        Settings(api_env="local", cognito=auth_helpers.CONFIG), key_provider=provider
    )
    app.dependency_overrides[api.get_executor] = lambda: fake
    client = TestClient(app)

    assert client.post("/api/runs", json={"days_back": 3}, headers=VIEWER).status_code == 403
    fake.start.assert_not_called()
    assert client.post("/api/runs", json={"days_back": 3}, headers=OPERATOR).status_code == 202
    fake.start.assert_called_once_with(3, requested_by="user-123", idempotency_key=None)
    for spy in pipeline_spies:
        spy.assert_not_called()


def test_environment_cannot_enable_execution_without_cognito(monkeypatch):
    monkeypatch.setenv("API_ENV", "local")
    monkeypatch.setenv("PIPELINE_EXECUTION_ENABLED", "true")

    with pytest.raises(Exception, match="requires Cognito authentication"):
        load_settings()
