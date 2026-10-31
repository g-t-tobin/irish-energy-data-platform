from unittest.mock import patch

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from src import api
from src.run_store import RunStore
from src.settings import Settings
from tests import auth_helpers as h

PROTECTED_GETS = ["/api/runs", "/api/runs/some-id", "/api/analytics/daily-demand"]
VIEWER = h.auth_header(h.token(groups=["viewer"]))
OPERATOR = h.auth_header(h.token(groups=["operator"]))
NO_GROUPS = h.auth_header(h.token(groups=None))
UNAUTHENTICATED_BODY = {"detail": "Not authenticated"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    run_store = RunStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(api, "_store", run_store)
    return run_store


def test_health_is_public(auth_app):
    response = auth_app.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_reads_require_authentication(auth_app, path):
    response = auth_app.get(path)

    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED_BODY
    assert response.headers["www-authenticate"] == "Bearer"


def test_post_requires_authentication(auth_app):
    response = auth_app.post("/api/runs", json={"days_back": 2})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer a b"},
        {"Authorization": "Token abc"},
        {"Authorization": "Bearer not-a-jwt"},
        h.auth_header(h.wrongly_signed()),
        h.auth_header(h.token(exp=1, iat=0)),
        h.auth_header(h.unsigned()),
        h.auth_header(h.hs256_forged_with_public_key()),
        h.auth_header(h.token(iss="https://evil.example.com")),
        h.auth_header(h.token(client_id="someotherclient123")),
        h.auth_header(h.token(token_use="id")),
        h.auth_header(h.token(kid="unknown")),
    ],
)
def test_every_authentication_failure_looks_identical(auth_app, headers):
    response = auth_app.get("/api/runs", headers=headers)

    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED_BODY
    assert response.headers["www-authenticate"] == "Bearer"


def test_duplicate_authorization_headers_rejected(auth_app):
    response = auth_app.get(
        "/api/runs",
        headers=[("Authorization", f"Bearer {h.token()}"), ("Authorization", f"Bearer {h.token()}")],
    )

    assert response.status_code == 401


def test_bearer_scheme_is_case_insensitive(auth_app, store):
    response = auth_app.get("/api/runs", headers={"Authorization": f"bearer {h.token()}"})

    assert response.status_code == 200


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_user_without_role_gets_403(auth_app, path):
    response = auth_app.get(path, headers=NO_GROUPS)

    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient permissions."}
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "groups", [["Viewer"], ["admin"], "viewer", [1], {"viewer": True}]
)
def test_untrusted_group_claims_get_403(auth_app, groups):
    response = auth_app.get("/api/runs", headers=h.auth_header(h.token(groups=groups)))

    assert response.status_code == 403


@pytest.mark.parametrize("headers", [VIEWER, OPERATOR])
def test_viewer_and_operator_can_read_run_history(auth_app, store, headers):
    run = store.create_run(2)

    listing = auth_app.get("/api/runs", headers=headers)
    single = auth_app.get(f"/api/runs/{run['run_id']}", headers=headers)

    assert listing.status_code == 200
    assert [r["run_id"] for r in listing.json()["runs"]] == [run["run_id"]]
    assert single.status_code == 200
    assert single.json()["run_id"] == run["run_id"]


def test_missing_run_is_404_only_after_authorization(auth_app, store):
    assert auth_app.get("/api/runs/missing").status_code == 401
    assert auth_app.get("/api/runs/missing", headers=NO_GROUPS).status_code == 403
    assert auth_app.get("/api/runs/missing", headers=VIEWER).status_code == 404


@pytest.mark.parametrize("headers", [VIEWER, OPERATOR])
def test_viewer_and_operator_can_read_analytics(auth_app, headers):
    payload = {"source": "s", "days": []}
    with patch("src.analytics.get_daily_demand", return_value=payload) as read:
        response = auth_app.get("/api/analytics/daily-demand?days=7", headers=headers)

    assert response.status_code == 200
    assert response.json() == payload
    read.assert_called_once_with(7)


def test_analytics_not_read_when_unauthorized(auth_app):
    with patch("src.analytics.get_daily_demand") as read:
        auth_app.get("/api/analytics/daily-demand")
        auth_app.get("/api/analytics/daily-demand", headers=NO_GROUPS)

    read.assert_not_called()


def test_analytics_input_validation_still_applies(auth_app):
    for days in ("0", "91", "abc"):
        response = auth_app.get(f"/api/analytics/daily-demand?days={days}", headers=VIEWER)
        assert response.status_code == 422


def test_viewer_cannot_post_and_operator_gets_disabled(auth_app, store):
    viewer = auth_app.post("/api/runs", json={"days_back": 2}, headers=VIEWER)
    operator = auth_app.post("/api/runs", json={"days_back": 2}, headers=OPERATOR)

    assert viewer.status_code == 403
    assert operator.status_code == 503
    assert operator.json() == {"detail": "Pipeline execution is disabled."}
    assert store.list_runs() == []


def test_authentication_is_checked_before_body_validation(auth_app):
    bad_body = {"json": {"days_back": 0}}

    assert auth_app.post("/api/runs", **bad_body).status_code == 401
    assert auth_app.post("/api/runs", headers=VIEWER, **bad_body).status_code == 403
    assert auth_app.post("/api/runs", headers=OPERATOR, **bad_body).status_code == 422


def test_role_hints_from_client_are_ignored(auth_app, store):
    hints = {
        **VIEWER,
        "X-Role": "operator",
        "X-User-Groups": "operator",
        "X-Amzn-Oidc-Groups": "operator",
    }

    response = auth_app.post(
        "/api/runs?role=operator&groups=operator", json={"days_back": 2, "role": "operator"}, headers=hints
    )

    assert response.status_code == 403


def test_unconfigured_authentication_fails_closed(store):
    client = TestClient(api.create_app(Settings(api_env="local")))

    assert client.get("/health").status_code == 200
    for headers in ({}, VIEWER, OPERATOR):
        response = client.get("/api/runs", headers=headers)
        assert response.status_code == 503
        assert response.json() == {"detail": "Authentication is not configured."}
    assert client.post("/api/runs", json={}, headers=OPERATOR).status_code == 503
    assert store.list_runs() == []


def test_cors_allows_authorization_header_for_configured_origin():
    client = TestClient(
        api.create_app(Settings(api_env="local", cors_allowed_origins=("https://app.example.com",)))
    )

    response = client.options(
        "/api/runs",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers


def test_existing_run_history_contract_is_preserved(auth_app, store):
    run = store.create_run(3)
    store.update_run(run["run_id"], status="failed", stage="bronze", error="X: boom")

    body = auth_app.get("/api/runs", headers=VIEWER).json()

    assert list(body) == ["runs"]
    assert {"run_id", "status", "stage", "days_back", "created_at", "error"} <= set(body["runs"][0])


def test_storage_errors_remain_generic_for_authorized_users(auth_app, monkeypatch):
    from unittest.mock import MagicMock

    from src.run_store import RunStoreError

    broken = MagicMock()
    broken.list_runs.side_effect = RunStoreError("Run history storage is unavailable.")
    monkeypatch.setattr(api, "_store", broken)

    response = auth_app.get("/api/runs", headers=VIEWER)

    assert response.status_code == 503
    assert response.json() == {"detail": "Run history storage is unavailable."}
