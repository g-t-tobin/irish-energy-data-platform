import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from src import api
from src.settings import load_settings

COGNITO_ENV = {
    "COGNITO_REGION": "eu-west-1",
    "COGNITO_USER_POOL_ID": "eu-west-1_AbC123xyz",
    "COGNITO_APP_CLIENT_ID": "abcdefghij1234567890",
    "RUN_HISTORY_BACKEND": "dynamodb",
    "RUN_HISTORY_TABLE": "irish-energy-run-history",
}


def _client(**env):
    return TestClient(api.create_app(load_settings({**COGNITO_ENV, **env})))


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_are_not_served_by_default(path):
    assert _client().get(path).status_code == 404
    assert _client(API_ENV="local").get(path).status_code == 404


def test_docs_served_only_when_explicitly_enabled_locally():
    client = _client(API_ENV="local", ENABLE_API_DOCS="true")

    assert client.get("/openapi.json").status_code == 200


def test_health_remains_public_and_minimal():
    response = _client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_allowed_origin_receives_cors_headers():
    client = _client(CORS_ALLOWED_ORIGINS="https://app.example.com")

    response = client.options(
        "/api/runs",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-credentials" not in response.headers


def test_other_origins_are_not_allowed():
    client = _client(CORS_ALLOWED_ORIGINS="https://app.example.com")

    response = client.options(
        "/api/runs",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_production_default_has_no_cors_origins():
    response = _client().options(
        "/api/runs",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_local_default_allows_the_vite_dev_origin():
    response = _client(API_ENV="local").options(
        "/api/runs",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
