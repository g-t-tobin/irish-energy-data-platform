from unittest.mock import patch

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from src import api  # noqa: E402
from src.auth import JwksKeyProvider  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402
from src.run_store import RunStore  # noqa: E402
from src.settings import RateLimits, Settings  # noqa: E402
from tests import auth_helpers as h  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make_client(**limits):
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    settings = Settings(
        api_env="local", cognito=h.CONFIG, rate_limits=RateLimits(**limits)
    )
    return TestClient(api.create_app(settings, key_provider=provider))


def headers_for(sub, groups=("viewer",)):
    return h.auth_header(h.token(sub=sub, groups=list(groups)))


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    run_store = RunStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(api, "_store", run_store)
    return run_store


# Limiter unit tests


def test_bucket_allows_capacity_then_blocks_with_retry_after():
    limiter = RateLimiter({"b": (3, 60.0)}, clock=Clock())

    assert all(limiter.consume("b", "u").allowed for _ in range(3))
    blocked = limiter.consume("b", "u")

    assert not blocked.allowed
    assert blocked.retry_after == 20


def test_bucket_refills_over_time():
    clock = Clock()
    limiter = RateLimiter({"b": (2, 60.0)}, clock=clock)
    limiter.consume("b", "u")
    limiter.consume("b", "u")
    assert not limiter.consume("b", "u").allowed

    clock.now += 30

    assert limiter.consume("b", "u").allowed
    assert not limiter.consume("b", "u").allowed


def test_keys_and_buckets_are_independent():
    limiter = RateLimiter({"a": (1, 60.0), "b": (1, 60.0)}, clock=Clock())

    assert limiter.consume("a", "u1").allowed
    assert not limiter.consume("a", "u1").allowed
    assert limiter.consume("a", "u2").allowed
    assert limiter.consume("b", "u1").allowed


def test_has_capacity_does_not_consume():
    limiter = RateLimiter({"b": (1, 60.0)}, clock=Clock())

    for _ in range(5):
        assert limiter.has_capacity("b", "u").allowed
    assert limiter.consume("b", "u").allowed
    assert not limiter.has_capacity("b", "u").allowed


def test_tracked_keys_are_bounded():
    limiter = RateLimiter({"b": (1, 60.0)}, clock=Clock(), max_keys=50)

    for n in range(500):
        limiter.consume("b", f"user-{n}")

    assert len(limiter._state) <= 50


# HTTP behaviour


def test_read_limit_returns_429_with_retry_after():
    client = make_client(read_per_minute=2)
    headers = headers_for("alice")

    statuses = [client.get("/api/runs", headers=headers).status_code for _ in range(3)]
    blocked = client.get("/api/runs", headers=headers)

    assert statuses == [200, 200, 429]
    assert blocked.json() == {"detail": "Too many requests."}
    assert int(blocked.headers["retry-after"]) >= 1


def test_read_bucket_is_shared_by_run_routes_and_per_user():
    client = make_client(read_per_minute=2)
    alice = headers_for("alice")

    assert client.get("/api/runs", headers=alice).status_code == 200
    assert client.get("/api/runs/x", headers=alice).status_code == 404
    assert client.get("/api/runs", headers=alice).status_code == 429
    assert client.get("/api/runs", headers=headers_for("bob")).status_code == 200


def test_analytics_has_its_own_limit_and_skips_the_reader_when_blocked():
    client = make_client(analytics_per_minute=1, read_per_minute=100)
    headers = headers_for("alice")

    with patch("src.analytics.get_daily_demand", return_value={"source": "s", "days": []}) as read:
        first = client.get("/api/analytics/daily-demand", headers=headers)
        second = client.get("/api/analytics/daily-demand", headers=headers)

    assert (first.status_code, second.status_code) == (200, 429)
    read.assert_called_once()
    assert client.get("/api/runs", headers=headers).status_code == 200


def test_limits_apply_to_operators_too():
    client = make_client(read_per_minute=1)
    operator = headers_for("op", ("operator",))

    assert client.get("/api/runs", headers=operator).status_code == 200
    assert client.get("/api/runs", headers=operator).status_code == 429


def test_forbidden_requests_do_not_consume_the_budget():
    client = make_client(read_per_minute=1)
    nobody = h.auth_header(h.token(sub="alice", groups=None))

    for _ in range(5):
        assert client.get("/api/runs", headers=nobody).status_code == 403

    assert client.get("/api/runs", headers=headers_for("alice")).status_code == 200


def test_write_limit_is_enforced_before_disabled_response(store):
    client = make_client(write_per_hour=2)
    operator = headers_for("op", ("operator",))

    statuses = [
        client.post("/api/runs", json={}, headers=operator).status_code for _ in range(3)
    ]

    assert statuses == [503, 503, 429]
    assert store.list_runs() == []


def test_viewer_post_attempts_do_not_consume_the_write_budget():
    client = make_client(write_per_hour=1)

    for _ in range(3):
        assert client.post("/api/runs", json={}, headers=headers_for("v")).status_code == 403

    operator = headers_for("v", ("operator",))
    assert client.post("/api/runs", json={}, headers=operator).status_code == 503


def test_repeated_authentication_failures_become_429():
    client = make_client(auth_failures_per_minute=3)

    statuses = [
        client.get("/api/runs", headers=h.auth_header("garbage")).status_code
        for _ in range(5)
    ]

    assert statuses == [401, 401, 401, 429, 429]


def test_valid_tokens_are_never_blocked_by_failure_limit():
    client = make_client(auth_failures_per_minute=1)
    for _ in range(4):
        client.get("/api/runs", headers=h.auth_header("garbage"))

    assert client.get("/api/runs", headers=headers_for("alice")).status_code == 200


def test_failure_429_carries_retry_after():
    client = make_client(auth_failures_per_minute=1)
    client.get("/api/runs")

    response = client.get("/api/runs")

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


def test_health_is_not_rate_limited():
    client = make_client(read_per_minute=1, auth_failures_per_minute=1)

    assert all(client.get("/health").status_code == 200 for _ in range(20))
