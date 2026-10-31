import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException

from src import api
from src.run_store import RunStore, RunStoreError
from src.settings import Settings

FRONTEND_FIELDS = {"run_id", "status", "stage", "days_back", "created_at"}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    run_store = RunStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(api, "_store", run_store)
    return run_store


def test_list_runs_matches_frontend_contract(store):
    first = store.create_run(2)
    store.update_run(first["run_id"], status="failed", stage="bronze", error="X: boom")
    second = store.create_run(5)

    body = api.list_runs()

    assert list(body) == ["runs"]
    assert [r["run_id"] for r in body["runs"]] == [second["run_id"], first["run_id"]]
    for run in body["runs"]:
        assert FRONTEND_FIELDS <= set(run)
    # Legacy rows without a code get a generic public message, never raw text.
    assert body["runs"][1]["error"] == "The pipeline run failed."
    assert body["runs"][1]["error_code"] == "INTERNAL_ERROR"
    json.dumps(body)


def test_empty_history_shape():
    assert api.list_runs() == {"runs": []}


def test_get_run_and_not_found(store):
    started = store.create_run(2)

    assert api.get_run(started["run_id"])["run_id"] == started["run_id"]
    with pytest.raises(HTTPException) as exc:
        api.get_run("missing")
    assert exc.value.status_code == 404


def test_history_survives_api_restart(store, tmp_path, monkeypatch):
    started = store.create_run(2)

    monkeypatch.setattr(api, "_store", RunStore(tmp_path / "runs.sqlite3"))

    assert api.get_run(started["run_id"])["status"] == "queued"


def test_startup_recovery_marks_interrupted_and_unblocks(store):
    started = store.create_run(2)
    store.update_run(started["run_id"], status="running", stage="gold")

    async def start():
        async with api.lifespan(api.create_app(Settings(api_env="local"))):
            pass

    asyncio.run(start())

    run = api.get_run(started["run_id"])
    assert run["status"] == "interrupted"
    assert "outcome is unknown" in run["error"]
    assert store.create_run(2)["status"] == "queued"


def test_startup_does_not_start_a_run(store):
    with patch("src.api.threading.Thread") as thread:
        asyncio.run(api.lifespan(api.create_app(Settings(api_env="local"))).__aenter__())

    thread.assert_not_called()
    assert store.list_runs() == []


def test_storage_errors_do_not_leak_details(monkeypatch):
    broken = MagicMock()
    broken.list_runs.side_effect = RunStoreError("Run history storage is unavailable.")
    broken.get_run.side_effect = RunStoreError("Run history storage is unavailable.")
    monkeypatch.setattr(api, "_store", broken)

    for call in (api.list_runs, lambda: api.get_run("3f2c9a52-7d41-4c1b-9b6e-0a1d2e3f4a5b")):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 503
        assert exc.value.detail == "Run history storage is unavailable."


def test_worker_failure_records_safe_error_without_traceback(store, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    run = store.create_run(2)

    api.execute_pipeline(run["run_id"], 2)

    saved = store.get_run(run["run_id"])
    assert saved["status"] == "failed"
    assert saved["error"].startswith("RuntimeError")
    assert "Traceback" not in saved["error"]
    assert "finished_at" in saved
    assert api.list_runs()["runs"][0]["run_id"] == run["run_id"]


def test_worker_records_stages_with_mocked_pipeline(store, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    run = store.create_run(2)

    with (
        patch("src.ingestion.eirgrid.fetch_demand_data", return_value={"Rows": []}),
        patch("src.ingestion.eirgrid.save_to_s3", return_value="bronze/a.json"),
        patch("src.transformation.process_bronze_object", return_value="silver/b.parquet"),
        patch("src.gold.process_silver_object", return_value="gold/c.parquet"),
        patch("src.pipeline.register_gold_partition", return_value=True),
    ):
        api.execute_pipeline(run["run_id"], 2)

    saved = store.get_run(run["run_id"])
    assert saved["status"] == "succeeded"
    assert saved["stage"] == "completed"
    assert (saved["bronze_key"], saved["silver_key"], saved["gold_key"]) == (
        "bronze/a.json",
        "silver/b.parquet",
        "gold/c.parquet",
    )


def test_worker_marks_partial_failure_after_gold(store, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    run = store.create_run(2)

    with (
        patch("src.ingestion.eirgrid.fetch_demand_data", return_value={"Rows": []}),
        patch("src.ingestion.eirgrid.save_to_s3", return_value="bronze/a.json"),
        patch("src.transformation.process_bronze_object", return_value="silver/b.parquet"),
        patch("src.gold.process_silver_object", return_value="gold/c.parquet"),
        patch(
            "src.pipeline.register_gold_partition",
            side_effect=PermissionError("denied"),
        ),
    ):
        api.execute_pipeline(run["run_id"], 2)

    saved = store.get_run(run["run_id"])
    assert saved["status"] == "partial_failure"
    assert saved["stage"] == "glue_registration"
    assert saved["gold_key"] == "gold/c.parquet"
