import sqlite3
import threading

import pytest

from src.run_store import (
    INTERRUPTED_MESSAGE,
    ActiveRunError,
    RunStore,
    RunStoreError,
    default_db_path,
    safe_error_message,
)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs.sqlite3")


def test_initialises_database_and_schema(tmp_path):
    path = tmp_path / "nested" / "runs.sqlite3"

    RunStore(path)

    assert path.exists()
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert {"run_id", "status", "stage", "days_back", "created_at", "updated_at",
            "error"} <= columns
    assert "traceback" not in columns
    RunStore(path)  # re-initialising is idempotent


def test_default_path_is_configurable_and_untracked(monkeypatch, tmp_path):
    monkeypatch.setenv("RUN_HISTORY_DB_PATH", str(tmp_path / "x.sqlite3"))
    assert default_db_path() == tmp_path / "x.sqlite3"

    monkeypatch.delenv("RUN_HISTORY_DB_PATH")
    assert ".local" in default_db_path().parts


def test_create_and_get_run(store):
    run = store.create_run(3)

    assert run["status"] == "queued"
    assert run["stage"] == "queued"
    assert run["days_back"] == 3
    assert store.get_run(run["run_id"])["created_at"] == run["created_at"]
    assert store.get_run("missing") is None


def test_update_status_and_stage(store):
    run = store.create_run(2)

    assert store.update_run(
        run["run_id"], status="running", stage="bronze", bronze_key="bronze/a.json"
    )

    saved = store.get_run(run["run_id"])
    assert (saved["status"], saved["stage"]) == ("running", "bronze")
    assert saved["bronze_key"] == "bronze/a.json"
    assert "error" not in saved
    assert store.update_run("missing", status="failed") is False


def test_update_rejects_unknown_fields(store):
    run = store.create_run(2)

    with pytest.raises(ValueError):
        store.update_run(run["run_id"], days_back=5)
    with pytest.raises(ValueError):
        store.update_run(run["run_id"], traceback="x")


@pytest.mark.parametrize("value", [0, 31, -1, True, "2", None, 1.5])
def test_create_run_validates_days_back(store, value):
    with pytest.raises(ValueError):
        store.create_run(value)

    assert store.list_runs() == []


def test_lists_newest_first(store):
    ids = []
    for _ in range(3):
        run = store.create_run(1)
        store.update_run(run["run_id"], status="succeeded")
        ids.append(run["run_id"])

    assert [r["run_id"] for r in store.list_runs()] == ids[::-1]
    assert len(store.list_runs(limit=2)) == 2


def test_records_survive_reinitialisation(tmp_path):
    path = tmp_path / "runs.sqlite3"
    first = RunStore(path)
    run = first.create_run(4)
    first.update_run(run["run_id"], status="succeeded", stage="completed")

    second = RunStore(path)

    saved = second.get_run(run["run_id"])
    assert saved["status"] == "succeeded"
    assert saved["days_back"] == 4


def test_prevents_overlapping_active_runs(store):
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

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([r for r in results if r]) == 1
    assert len(store.list_runs()) == 1


def test_concurrent_updates_do_not_corrupt_database(store):
    runs = []
    for _ in range(5):
        run = store.create_run(1)
        store.update_run(run["run_id"], status="succeeded")
        runs.append(run["run_id"])

    def work(run_id):
        for n in range(20):
            store.update_run(run_id, stage=f"stage-{n}")

    threads = [threading.Thread(target=work, args=(r,)) for r in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(store.get_run(r)["stage"] == "stage-19" for r in runs)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_recovery_marks_active_runs_interrupted_after_restart(tmp_path):
    path = tmp_path / "runs.sqlite3"
    before = RunStore(path)
    running = before.create_run(2)
    before.update_run(running["run_id"], status="running", stage="silver")
    with pytest.raises(ActiveRunError):  # simulated crash leaves the run active
        before.create_run(2)

    after = RunStore(path)
    assert after.recover_interrupted() == 1

    saved = after.get_run(running["run_id"])
    assert saved["status"] == "interrupted"
    assert saved["stage"] == "silver"
    assert saved["error"] == INTERRUPTED_MESSAGE
    assert "finished_at" in saved
    assert after.recover_interrupted() == 0
    assert after.create_run(2)["status"] == "queued"  # no longer blocked


def test_recovery_leaves_terminal_runs_untouched(store):
    run = store.create_run(2)
    store.update_run(run["run_id"], status="succeeded", stage="completed")

    assert store.recover_interrupted() == 0
    assert store.get_run(run["run_id"])["status"] == "succeeded"


def test_safe_error_message_redacts_and_truncates():
    class CredentialError(Exception):
        pass

    message = safe_error_message(
        CredentialError("key AKIAABCDEFGHIJKLMNOP and secret_key=abc123 " + "x" * 500)
    )

    assert "AKIAABCDEFGHIJKLMNOP" not in message
    assert "abc123" not in message
    assert len(message) <= 300
    assert "Traceback" not in message


def test_safe_error_message_uses_only_aws_error_code():
    from botocore.exceptions import ClientError

    exc = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "arn:aws:iam::123:user/x"}},
        "PutObject",
    )

    assert safe_error_message(exc) == "ClientError (AccessDenied)"


def test_storage_failure_is_wrapped_without_details(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")

    with pytest.raises(RunStoreError) as exc:
        RunStore(blocker / "runs.sqlite3")

    assert str(exc.value) == "Run history storage is unavailable."
    assert str(tmp_path) not in str(exc.value)
