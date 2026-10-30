import threading

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src.dynamodb_run_store import UNAVAILABLE, DynamoRunStore
from src.run_history_schema import IDEMPOTENCY_TTL_SECONDS, LOCK_LEASE_SECONDS
from src.run_store import ActiveRunError, RunStoreError
from tests.fake_dynamodb import FakeDynamoDB
from tests.test_run_store_contract import Clock

LOCK = ("LOCK#pipeline", "LOCK")


@pytest.fixture
def env():
    fake, clock = FakeDynamoDB(), Clock()
    return fake, clock, DynamoRunStore("runs-table", fake, clock=clock)


def _lock(fake):
    item = fake.items.get(LOCK)
    return None if item is None else (item["run_id"]["S"], int(item["lease_expires_at"]["N"]))


def test_creating_a_run_takes_the_lock_with_a_lease(env):
    fake, clock, store = env

    run = store.create_run(2)

    owner, lease = _lock(fake)
    assert owner == run["run_id"]
    assert lease >= int(clock.now) + LOCK_LEASE_SECONDS - 5


def test_expired_lease_lets_a_new_run_take_over(env):
    fake, clock, store = env
    stuck = store.create_run(2)
    with pytest.raises(ActiveRunError):
        store.create_run(2)

    clock.advance(LOCK_LEASE_SECONDS + 1)
    fresh = store.create_run(2)

    assert _lock(fake)[0] == fresh["run_id"]
    assert store.get_run(stuck["run_id"])["status"] == "queued"


def test_recovery_after_takeover_interrupts_only_the_stale_run(env):
    fake, clock, store = env
    stale = store.create_run(2)
    clock.advance(LOCK_LEASE_SECONDS + 1)
    live = store.create_run(2)

    assert store.recover_interrupted() == 1

    assert store.get_run(stale["run_id"])["status"] == "interrupted"
    assert store.get_run(live["run_id"])["status"] == "queued"
    assert _lock(fake)[0] == live["run_id"]


def test_terminal_update_releases_only_its_own_lock(env):
    fake, clock, store = env
    first = store.create_run(2)
    store.update_run(first["run_id"], status="running")
    store.update_run(first["run_id"], status="succeeded", stage="completed")
    assert _lock(fake) is None

    stale = store.create_run(2)
    clock.advance(LOCK_LEASE_SECONDS + 1)
    taker = store.create_run(2)
    assert store.update_run(stale["run_id"], status="failed")

    assert _lock(fake)[0] == taker["run_id"]


def test_progress_is_a_heartbeat_that_extends_the_lease(env):
    fake, clock, store = env
    run = store.create_run(2)
    first_lease = _lock(fake)[1]

    clock.advance(LOCK_LEASE_SECONDS - 60)
    store.update_run(run["run_id"], status="running", stage="gold")

    assert _lock(fake)[1] > first_lease
    clock.advance(120)  # past the original lease, inside the extended one
    with pytest.raises(ActiveRunError):
        store.create_run(2)


def test_a_live_lease_protects_a_running_run_from_another_tasks_startup_recovery(env):
    _, clock, store = env
    run = store.create_run(2)
    store.update_run(run["run_id"], status="running")
    other_task = DynamoRunStore("runs-table", env[0], clock=clock)

    assert other_task.recover_interrupted() == 0
    assert store.get_run(run["run_id"])["status"] == "running"


def test_late_progress_cannot_revive_a_finished_run(env):
    _, _, store = env
    run = store.create_run(2)
    store.update_run(run["run_id"], status="failed")

    assert store.update_run(run["run_id"], stage="gold") is False
    assert store.update_run(run["run_id"], status="running") is False
    saved = store.get_run(run["run_id"])
    assert (saved["status"], saved["stage"]) == ("failed", "queued")


def test_real_outcome_overrides_an_interrupted_guess_but_not_another_outcome(env):
    _, clock, store = env
    run = store.create_run(2)
    clock.advance(LOCK_LEASE_SECONDS + 1)
    store.recover_interrupted()

    assert store.update_run(run["run_id"], status="succeeded", stage="completed")
    assert store.update_run(run["run_id"], status="failed") is False
    assert store.get_run(run["run_id"])["status"] == "succeeded"


def test_terminal_status_sets_finished_at_and_allows_bookkeeping_afterwards(env):
    _, _, store = env
    run = store.create_run(2)

    store.update_run(run["run_id"], status="failed", error="X: boom", error_code="INTERNAL_ERROR")
    assert "finished_at" in store.get_run(run["run_id"])

    assert store.update_run(run["run_id"], finished_at="2026-10-30T10:00:00+00:00")
    assert store.get_run(run["run_id"])["finished_at"] == "2026-10-30T10:00:00+00:00"


def test_repeat_of_a_started_request_does_not_touch_the_lock_or_create_items(env):
    fake, _, store = env
    first = store.create_run(2, requested_by="alice", idempotency_key="retry-key-0001")
    before = dict(fake.items)
    transactions = fake.calls.count("transact_write_items")

    again = store.create_run(2, requested_by="alice", idempotency_key="retry-key-0001")

    assert again["run_id"] == first["run_id"]
    assert fake.items == before
    assert fake.calls.count("transact_write_items") == transactions


def test_simultaneous_duplicates_create_one_run_and_return_the_same_id(env):
    fake, _, store = env
    barrier, results = threading.Barrier(6), []

    def attempt():
        barrier.wait()
        try:
            results.append(store.create_run(2, requested_by="alice", idempotency_key="race-key-0001")["run_id"])
        except ActiveRunError:
            results.append("conflict")

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    run_items = [k for k in fake.items if k[0].startswith("RUN#")]
    assert len(run_items) == 1
    assert set(results) == {run_items[0][0].removeprefix("RUN#")}


def test_idempotency_key_expires_with_its_ttl(env):
    _, clock, store = env
    first = store.create_run(2, requested_by="alice", idempotency_key="expiring-key-1")
    store.update_run(first["run_id"], status="succeeded")

    clock.advance(IDEMPOTENCY_TTL_SECONDS + 1)
    second = store.create_run(2, requested_by="alice", idempotency_key="expiring-key-1")

    assert second["run_id"] != first["run_id"]
    assert "duplicate" not in second


def test_listing_returns_only_runs_without_internal_attributes(env):
    fake, _, store = env
    store.create_run(2, requested_by="alice", idempotency_key="listing-key-01")

    runs = store.list_runs()

    assert len(runs) == 1
    assert set(runs[0]) <= {
        "run_id", "status", "stage", "days_back", "created_at", "updated_at", "started_at",
        "finished_at", "bronze_key", "silver_key", "gold_key", "error", "error_code", "requested_by",
    }
    assert len(fake.items) == 3  # run, lock, idempotency


def test_listing_limit_is_capped(env):
    fake, _, store = env
    store.list_runs(limit=10_000)

    assert fake.calls[-1] == "query"


@pytest.mark.parametrize(
    "failure",
    [
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:dynamodb:secret-table"}}, "GetItem"),
        ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "Query"),
        EndpointConnectionError(endpoint_url="https://dynamodb.internal.example"),
    ],
)
@pytest.mark.parametrize("operation", ["create", "get", "list", "update", "recover"])
def test_storage_failures_are_generic(env, failure, operation):
    fake, _, store = env
    run = store.create_run(2)
    fake.fail_with = failure

    with pytest.raises(RunStoreError) as exc:
        {
            "create": lambda: store.create_run(2),
            "get": lambda: store.get_run(run["run_id"]),
            "list": store.list_runs,
            "update": lambda: store.update_run(run["run_id"], stage="gold"),
            "recover": store.recover_interrupted,
        }[operation]()

    assert str(exc.value) == UNAVAILABLE
    assert "secret-table" not in str(exc.value) and "internal.example" not in str(exc.value)


def test_cancelled_transaction_for_other_reasons_is_a_storage_error(env):
    fake, _, store = env
    fake.fail_with = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "x"},
            "CancellationReasons": [{"Code": "ThrottlingError"}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )

    with pytest.raises(RunStoreError):
        store.create_run(2)


def test_nothing_secret_or_token_shaped_is_stored(env):
    fake, _, store = env
    store.create_run(2, requested_by="sub-123", idempotency_key="private-key-001")

    stored = str(fake.items)

    assert "private-key-001" not in stored  # only a hash of the key is kept
    assert "sub-123" in stored


# Backend selection and API compatibility

from src import api  # noqa: E402
from src.run_store import RunStore  # noqa: E402
from src.run_store_factory import build_run_store  # noqa: E402
from src.settings import RunHistorySettings  # noqa: E402


def test_factory_defaults_to_sqlite_and_never_touches_aws():
    assert isinstance(build_run_store(RunHistorySettings()), RunStore)


def test_factory_builds_dynamodb_store_from_the_injected_client_only():
    fake = FakeDynamoDB()

    store = build_run_store(RunHistorySettings(backend="dynamodb", table="irish-energy-runs"), client=fake)

    assert isinstance(store, DynamoRunStore)
    store.create_run(2)
    assert fake.items  # used the injected client; the boto3 guard would fail otherwise


def test_get_store_uses_sqlite_unless_dynamodb_is_configured(monkeypatch):
    monkeypatch.setattr(api, "_store", None)

    assert isinstance(api.get_store(), RunStore)


def test_run_history_endpoints_are_unchanged_on_the_shared_store(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.auth import JwksKeyProvider
    from src.settings import Settings
    from tests import auth_helpers as h

    store = DynamoRunStore("runs-table", FakeDynamoDB(), clock=Clock())
    monkeypatch.setattr(api, "_store", store)
    run = store.create_run(2, requested_by="alice", idempotency_key="compat-key-0001")
    store.update_run(
        run["run_id"], status="partial_failure", stage="glue_registration",
        gold_key="gold/x.parquet", error="ClientError (AccessDenied)", error_code="ACCESS_DENIED",
    )
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    client = TestClient(api.create_app(Settings(api_env="local", cognito=h.CONFIG), key_provider=provider))
    viewer = h.auth_header(h.token(groups=["viewer"]))

    listing = client.get("/api/runs", headers=viewer).json()
    single = client.get(f"/api/runs/{run['run_id']}", headers=viewer).json()

    assert listing == {"runs": [single]}
    assert {"run_id", "status", "stage", "days_back", "created_at", "error", "error_code"} <= set(single)
    assert not {"gold_key", "requested_by", "pk", "gsi1pk"} & set(single)
    assert "AccessDenied" not in str(single)
    assert client.get("/api/runs/00000000-0000-4000-8000-000000000000", headers=viewer).status_code == 404


def test_startup_recovery_runs_against_the_shared_store(monkeypatch):
    import asyncio

    from src.settings import Settings

    clock = Clock()
    store = DynamoRunStore("runs-table", FakeDynamoDB(), clock=clock)
    monkeypatch.setattr(api, "_store", store)
    run = store.create_run(2)
    clock.advance(LOCK_LEASE_SECONDS + 1)

    async def start():
        async with api.lifespan(api.create_app(Settings(api_env="local"))):
            pass

    asyncio.run(start())

    assert store.get_run(run["run_id"])["status"] == "interrupted"
