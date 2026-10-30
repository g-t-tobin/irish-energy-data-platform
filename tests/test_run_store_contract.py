"""Behaviour every RunHistory backend must share (SQLite and DynamoDB via a test double)."""

import threading

import pytest

from src.dynamodb_run_store import DynamoRunStore
from src.run_history_schema import LOCK_LEASE_SECONDS
from src.run_store import ActiveRunError, RunStore, to_public_run
from tests.fake_dynamodb import FakeDynamoDB


class Clock:
    """Advances a millisecond per call so run ordering is stable."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        self.now += 0.001
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Backend:
    def __init__(self, kind, tmp_path):
        self.kind = kind
        self.clock = Clock()
        self.fake = FakeDynamoDB()
        self.path = tmp_path / "runs.sqlite3"

    def open(self):
        if self.kind == "sqlite":
            return RunStore(self.path)
        return DynamoRunStore("runs-table", self.fake, clock=self.clock)

    def expire_leases(self):
        # SQLite assumes a single owning process, so a restart is enough.
        if self.kind == "dynamodb":
            self.clock.advance(LOCK_LEASE_SECONDS + 1)


@pytest.fixture(params=["sqlite", "dynamodb"])
def backend(request, tmp_path):
    return Backend(request.param, tmp_path)


@pytest.fixture
def store(backend):
    return backend.open()


def test_create_and_get_run(store):
    run = store.create_run(3)

    assert (run["status"], run["stage"], run["days_back"]) == ("queued", "queued", 3)
    saved = store.get_run(run["run_id"])
    assert saved["created_at"] == run["created_at"]
    assert saved["days_back"] == 3
    assert store.get_run("missing") is None


def test_update_status_and_stage(store):
    run = store.create_run(2)

    assert store.update_run(run["run_id"], status="running", stage="bronze", bronze_key="bronze/a.json")

    saved = store.get_run(run["run_id"])
    assert (saved["status"], saved["stage"], saved["bronze_key"]) == ("running", "bronze", "bronze/a.json")
    assert store.update_run("missing", status="failed") is False


def test_update_rejects_unknown_fields(store):
    run = store.create_run(2)

    for field in ("days_back", "traceback", "requested_by", "gsi1pk"):
        with pytest.raises(ValueError):
            store.update_run(run["run_id"], **{field: "x"})


@pytest.mark.parametrize("value", [0, 31, -1, True, "2", None, 1.5])
def test_create_validates_days_back(store, value):
    with pytest.raises(ValueError):
        store.create_run(value)
    assert store.list_runs() == []


@pytest.mark.parametrize("key", ["short", "has space in it", "x" * 65, "bad/chars!!"])
def test_create_validates_idempotency_key(store, key):
    with pytest.raises(ValueError):
        store.create_run(2, idempotency_key=key)


def test_lists_newest_first_with_limit(store):
    ids = []
    for _ in range(3):
        run = store.create_run(1)
        store.update_run(run["run_id"], status="succeeded")
        ids.append(run["run_id"])

    assert [r["run_id"] for r in store.list_runs()] == ids[::-1]
    assert len(store.list_runs(limit=2)) == 2


def test_records_persist_across_reinitialisation(backend):
    first = backend.open()
    run = first.create_run(4)
    first.update_run(run["run_id"], status="succeeded", stage="completed")

    second = backend.open()

    assert second.get_run(run["run_id"])["status"] == "succeeded"
    assert second.get_run(run["run_id"])["days_back"] == 4


def test_overlapping_active_runs_are_prevented(store):
    first = store.create_run(2)

    with pytest.raises(ActiveRunError):
        store.create_run(2)
    store.update_run(first["run_id"], status="running")
    with pytest.raises(ActiveRunError):
        store.create_run(2)

    store.update_run(first["run_id"], status="failed")
    assert store.create_run(2)["status"] == "queued"


def test_concurrent_creates_allow_exactly_one_active_run(store):
    results = []

    def attempt():
        try:
            results.append(store.create_run(2))
        except ActiveRunError:
            results.append(None)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([r for r in results if r]) == 1
    assert len(store.list_runs()) == 1


def test_duplicate_request_returns_the_original_run(store):
    first = store.create_run(2, requested_by="alice", idempotency_key="retry-key-0001")

    again = store.create_run(2, requested_by="alice", idempotency_key="retry-key-0001")

    assert again["run_id"] == first["run_id"]
    assert again["duplicate"] is True
    assert len(store.list_runs()) == 1


def test_idempotency_keys_are_scoped_to_the_requester(store):
    store.create_run(2, requested_by="alice", idempotency_key="shared-key-0001")

    with pytest.raises(ActiveRunError):
        store.create_run(2, requested_by="bob", idempotency_key="shared-key-0001")


def test_recovery_marks_runs_left_active_by_a_restart_as_interrupted(backend):
    before = backend.open()
    run = before.create_run(2)
    before.update_run(run["run_id"], status="running", stage="silver")

    backend.expire_leases()
    after = backend.open()

    assert after.recover_interrupted() == 1
    saved = after.get_run(run["run_id"])
    assert (saved["status"], saved["stage"], saved["error_code"]) == ("interrupted", "silver", "INTERRUPTED")
    assert "outcome is unknown" in saved["error"]
    assert "finished_at" in saved
    assert after.recover_interrupted() == 0
    assert after.create_run(2)["status"] == "queued"


def test_recovery_leaves_finished_runs_alone(store):
    run = store.create_run(2)
    store.update_run(run["run_id"], status="succeeded", stage="completed")

    assert store.recover_interrupted() == 0
    assert store.get_run(run["run_id"])["status"] == "succeeded"


def test_public_view_matches_the_frontend_contract(store):
    run = store.create_run(2, requested_by="alice", idempotency_key="public-key-0001")
    store.update_run(run["run_id"], status="failed", error="raw detail", error_code="ACCESS_DENIED")

    public = to_public_run(store.get_run(run["run_id"]))

    assert {"run_id", "status", "stage", "days_back", "created_at", "error", "error_code"} <= set(public)
    assert not {"requested_by", "idempotency_key", "pk", "sk", "gsi1pk", "gsi1sk"} & set(public)
    assert "raw detail" not in str(public)
