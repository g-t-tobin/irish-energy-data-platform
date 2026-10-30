"""Test guards: no real AWS, network or credential files can be used."""

import urllib.request

import boto3
import pytest
import requests

from tests import auth_helpers

_SETTINGS_VARS = (
    "API_ENV",
    "CORS_ALLOWED_ORIGINS",
    "PIPELINE_EXECUTION_ENABLED",
    "ENABLE_API_DOCS",
    "COGNITO_REGION",
    "COGNITO_USER_POOL_ID",
    "COGNITO_APP_CLIENT_ID",
)
_AWS_VARS = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def _forbidden(*_args, **_kwargs):
    raise AssertionError("Real AWS or network access attempted in a test.")


@pytest.fixture(autouse=True)
def _safety_guards(monkeypatch, tmp_path):
    for name in _SETTINGS_VARS + _AWS_VARS:
        monkeypatch.delenv(name, raising=False)

    # Point credential/config lookups at nothing so no real file is read.
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("RUN_HISTORY_DB_PATH", str(tmp_path / "guard.sqlite3"))

    # Tests that need a client patch these explicitly with a mock.
    monkeypatch.setattr(boto3, "client", _forbidden)
    monkeypatch.setattr(boto3, "resource", _forbidden)
    monkeypatch.setattr(boto3.session.Session, "client", _forbidden)
    monkeypatch.setattr(boto3.session.Session, "resource", _forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)

    try:
        from src import api
    except ImportError:  # API dependencies are optional for non-API tests
        return
    # Never reuse a run store created by an earlier test.
    monkeypatch.setattr(api, "_store", None)


@pytest.fixture
def auth_app():
    """App with Cognito configured against a local JWKS stub (no network)."""
    from fastapi.testclient import TestClient

    from src import api
    from src.auth import JwksKeyProvider
    from src.settings import Settings

    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=auth_helpers.jwks)
    settings = Settings(api_env="local", cognito=auth_helpers.CONFIG)
    return TestClient(api.create_app(settings, key_provider=provider))
