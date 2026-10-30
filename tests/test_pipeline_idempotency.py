"""Retries, repeated runs and overlapping run IDs: what S3 ends up holding."""

import io
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from src import pipeline_stages as stages
from src.ingestion.eirgrid import save_to_s3
from src.transformation import process_bronze_object, save_silver_to_s3
from tests.pipeline_harness import BUCKET, demand_payload, make_pipeline_fixture

pipeline = make_pipeline_fixture()
HANDLERS = {"bronze": stages.ingest_handler, "silver": stages.silver_handler, "gold": stages.gold_handler}


def event(run_id, **stage_keys):
    state = {"run_id": run_id, "days_back": 2}
    state.update({stage: {"output": {"key": key}} for stage, key in stage_keys.items()})
    return state


def rows_in(pipeline, key):
    return len(pd.read_parquet(io.BytesIO(pipeline.s3.read(key))))


def test_a_repeated_ingestion_of_the_same_run_rewrites_one_bronze_object(pipeline):
    run_id = str(uuid.uuid4())

    first = stages.ingest_handler(event(run_id))["key"]
    pipeline.eirgrid.default = demand_payload(per_day=3)  # fresher data on the retry
    second = stages.ingest_handler(event(run_id))["key"]

    assert first == second and pipeline.s3.object_keys("bronze/") == [first]
    assert len(json.loads(pipeline.s3.read(first))["Rows"]) == 6  # the latest attempt is the run's Bronze


def test_a_repeated_silver_stage_rewrites_one_silver_object_from_the_same_bronze(pipeline):
    run_id = str(uuid.uuid4())
    bronze = stages.ingest_handler(event(run_id))["key"]

    first = stages.silver_handler(event(run_id, bronze=bronze))["key"]
    second = stages.silver_handler(event(run_id, bronze=bronze))["key"]

    assert first == second and pipeline.s3.object_keys("silver/") == [first]


def test_a_repeated_gold_stage_rewrites_the_same_gold_object_with_identical_content(pipeline):
    run_id = str(uuid.uuid4())
    bronze = stages.ingest_handler(event(run_id))["key"]
    silver = stages.silver_handler(event(run_id, bronze=bronze))["key"]

    first = stages.gold_handler(event(run_id, silver=silver))["key"]
    body = pipeline.s3.read(first)
    second = stages.gold_handler(event(run_id, silver=silver))["key"]

    assert first == second and pipeline.s3.object_keys("gold/") == [first]
    assert pd.read_parquet(io.BytesIO(body)).equals(pd.read_parquet(io.BytesIO(pipeline.s3.read(first))))


def test_a_repeated_registration_is_a_no_op(pipeline):
    run_id = str(uuid.uuid4())
    gold = "gold/eirgrid/daily_demand/run_date=2026-10-28/daily_demand.parquet"

    assert stages.glue_handler(event(run_id, gold=gold))["partition_created"] is True
    assert stages.glue_handler(event(run_id, gold=gold))["partition_created"] is False
    assert list(pipeline.glue.registered) == ["2026-10-28"]


@pytest.mark.parametrize("stage", ["bronze", "silver", "gold", "glue_registration"])
def test_a_stage_whose_response_is_lost_is_retried_without_a_second_logical_output(pipeline, stage):
    run_id = pipeline.new_run()
    pipeline.lose_response(stage, times=1)

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "SUCCEEDED" and execution.invocations[{
        "bronze": "Bronze", "silver": "Silver", "gold": "Gold", "glue_registration": "GlueRegistration"}[stage]] == 2
    assert [len(pipeline.s3.object_keys(f"{layer}/")) for layer in ("bronze", "silver", "gold")] == [1, 1, 1]
    run = pipeline.store.get_run(run_id)
    assert pipeline.s3.object_keys("silver/") == [run["silver_key"]] and pipeline.s3.object_keys("gold/") == [run["gold_key"]]
    assert pipeline.lock() is None


def test_two_runs_never_overwrite_each_other_even_in_the_same_second(pipeline):
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    pipeline.eirgrid.default = demand_payload(per_day=1)
    bronze_a = stages.ingest_handler(event(run_a))["key"]
    pipeline.eirgrid.default = demand_payload(per_day=3)
    bronze_b = stages.ingest_handler(event(run_b))["key"]

    assert bronze_a != bronze_b and len(pipeline.s3.object_keys("bronze/")) == 2
    assert len(json.loads(pipeline.s3.read(bronze_a))["Rows"]) == 2
    assert len(json.loads(pipeline.s3.read(bronze_b))["Rows"]) == 6


def test_a_stage_consumes_the_object_of_its_own_run_not_the_newest_one(pipeline):
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    pipeline.eirgrid.default = demand_payload(per_day=1)
    bronze_a = stages.ingest_handler(event(run_a))["key"]
    pipeline.eirgrid.default = demand_payload(per_day=3)
    bronze_b = stages.ingest_handler(event(run_b))["key"]  # newer, larger, same prefix

    silver_a = stages.silver_handler(event(run_a, bronze=bronze_a))["key"]
    silver_b = stages.silver_handler(event(run_b, bronze=bronze_b))["key"]

    assert silver_a != silver_b
    assert (rows_in(pipeline, silver_a), rows_in(pipeline, silver_b)) == (2, 6)
    assert pipeline.s3.object_keys("silver/") == sorted([silver_a, silver_b])


def test_concurrent_runs_write_distinct_objects_and_each_silver_matches_its_own_input(pipeline):
    run_ids = [str(uuid.uuid4()) for _ in range(8)]
    pipeline.eirgrid.responder = lambda params: demand_payload(per_day=int(params["datefrom"][:2]) % 5 + 1)

    def work(run_id):
        bronze = stages.ingest_handler(event(run_id))["key"]
        silver = stages.silver_handler(event(run_id, bronze=bronze))["key"]
        return run_id, (bronze, silver, len(json.loads(pipeline.s3.read(bronze))["Rows"]))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(work, run_ids))  # a failure in any worker is raised here

    assert len(results) == 8
    assert len({r[0] for r in results.values()}) == 8 and len({r[1] for r in results.values()}) == 8
    for bronze, silver, rows in results.values():
        assert rows_in(pipeline, silver) == rows


def test_a_second_run_on_the_same_day_replaces_the_days_gold_object(pipeline):
    """Characterisation: Gold keeps one object per run date (the analytics contract), so the latest wins."""
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    keys = []
    for run_id, per_day in ((run_a, 1), (run_b, 3)):
        pipeline.eirgrid.default = demand_payload(per_day=per_day)
        bronze = stages.ingest_handler(event(run_id))["key"]
        silver = stages.silver_handler(event(run_id, bronze=bronze))["key"]
        keys.append(stages.gold_handler(event(run_id, silver=silver))["key"])

    assert keys[0] == keys[1] and pipeline.s3.object_keys("gold/") == [keys[0]]
    assert pd.read_parquet(io.BytesIO(pipeline.s3.read(keys[0])))["observations"].sum() == 6


def test_a_failed_run_does_not_touch_another_runs_output(pipeline):
    good = pipeline.new_run()
    pipeline.execute(good)
    before = {key: pipeline.s3.read(key) for key in pipeline.s3.object_keys()}
    failing = pipeline.new_run()
    pipeline.fail("silver")

    pipeline.execute(failing)

    assert all(pipeline.s3.read(key) == body for key, body in before.items())
    assert len(pipeline.s3.object_keys("bronze/")) == 2 and len(pipeline.s3.object_keys("silver/")) == 1


@pytest.mark.parametrize("bad", ["../x", "x", "0B6F4C1E-8A52-4D57-9A2F-3C1E5B7D9A10", "a" * 36, "", "x/y"])
def test_a_malformed_run_id_cannot_shape_an_object_key(pipeline, bad):
    with pytest.raises(ValueError, match="run_id"):
        save_to_s3(demand_payload(), BUCKET, bad)
    with pytest.raises(ValueError, match="run_id"):
        save_silver_to_s3(pd.DataFrame({"a": [1]}), BUCKET, bad)
    with pytest.raises(ValueError, match="run_id"):
        process_bronze_object(BUCKET, "bronze/x.json", bad)
    assert pipeline.s3.object_keys() == []


def test_without_a_run_id_the_original_timestamped_names_are_unchanged(pipeline):
    bronze = save_to_s3(demand_payload(), BUCKET)

    assert re.fullmatch(
        r"bronze/eirgrid/demand_actual/ingestion_date=\d{4}-\d{2}-\d{2}/demand_actual_\d{8}T\d{6}Z\.json", bronze
    )
    silver = process_bronze_object(BUCKET, bronze)
    assert re.fullmatch(
        r"silver/eirgrid/demand_actual/ingestion_date=\d{4}-\d{2}-\d{2}/demand_actual_\d{8}T\d{6}Z\.parquet", silver
    )
