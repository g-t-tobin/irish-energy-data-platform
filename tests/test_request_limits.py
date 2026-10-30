import uuid
from unittest.mock import MagicMock

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from src import api  # noqa: E402
from src.auth import JwksKeyProvider  # noqa: E402
from src.settings import Settings  # noqa: E402
from tests import auth_helpers as h  # noqa: E402

VIEWER = h.auth_header(h.token(groups=["viewer"]))
OPERATOR = h.auth_header(h.token(groups=["operator"]))


@pytest.fixture
def client():
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    app = api.create_app(Settings(api_env="local", cognito=h.CONFIG), key_provider=provider)
    return TestClient(app)


def test_oversized_body_is_rejected_before_authentication(client):
    response = client.post("/api/runs", content=b"x" * (api.MAX_BODY_BYTES + 1))

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large."}


def test_oversized_body_never_reaches_the_executor(client):
    fake = MagicMock()
    client.app.dependency_overrides[api.get_executor] = lambda: fake

    response = client.post(
        "/api/runs",
        content=b'{"days_back": 2, "pad": "' + b"x" * api.MAX_BODY_BYTES + b'"}',
        headers={**OPERATOR, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    fake.start.assert_not_called()


def test_body_at_the_limit_is_accepted(client):
    body = b'{"days_back": 2}'.ljust(api.MAX_BODY_BYTES, b" ")

    response = client.post(
        "/api/runs", content=body, headers={**OPERATOR, "Content-Type": "application/json"}
    )

    assert response.status_code == 503  # disabled, but not rejected for size


def test_chunked_bodies_without_a_length_are_refused(client):
    response = client.post(
        "/api/runs",
        content=iter([b'{"days_back"', b": 2}"]),
        headers={**OPERATOR, "Content-Type": "application/json"},
    )

    assert response.status_code == 411
    assert response.json() == {"detail": "Content-Length is required."}


def test_invalid_content_length_is_refused(client):
    response = client.get("/api/runs", headers={**VIEWER, "Content-Length": "abc"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Content-Length."}


def test_rejected_requests_still_carry_request_id_and_security_headers(client):
    response = client.post("/api/runs", content=b"x" * (api.MAX_BODY_BYTES + 1))

    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_normal_reads_are_unaffected(client, tmp_path, monkeypatch):
    from src.run_store import RunStore

    monkeypatch.setattr(api, "_store", RunStore(tmp_path / "runs.sqlite3"))

    assert client.get("/api/runs", headers=VIEWER).status_code == 200


@pytest.mark.parametrize(
    "run_id", ["missing", "x", "1; DROP TABLE runs", "A" * 36, "%00"]
)
def test_malformed_run_ids_get_404_without_touching_storage(client, monkeypatch, run_id):
    store = MagicMock()
    monkeypatch.setattr(api, "_store", store)

    response = client.get(f"/api/runs/{run_id}", headers=VIEWER)

    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found."}
    store.get_run.assert_not_called()


def test_malformed_run_ids_are_still_authenticated_first(client):
    assert client.get("/api/runs/not-a-uuid").status_code == 401


def test_well_formed_unknown_run_id_reaches_storage(client, tmp_path, monkeypatch):
    from src.run_store import RunStore

    store = RunStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(api, "_store", store)
    created = store.create_run(2)

    assert client.get(f"/api/runs/{created['run_id']}", headers=VIEWER).status_code == 200
    assert client.get(f"/api/runs/{uuid.uuid4()}", headers=VIEWER).status_code == 404
