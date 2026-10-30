"""The whole pipeline, locally: real handlers and processing code, files for S3, no AWS."""

import io
import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src import analytics
from src import pipeline as pipeline_module
from src import run_history_schema as schema
from tests.pipeline_harness import BUCKET, demand_payload, make_pipeline_fixture

pipeline = make_pipeline_fixture()


def run_once(pipeline, days_back=2):
    run_id = pipeline.new_run(days_back, requested_by="operator-1")
    result, execution = pipeline.execute(run_id, days_back)
    return run_id, result, execution


def test_a_complete_run_writes_each_layer_registers_the_partition_and_records_success(pipeline):
    pipeline.eirgrid.default = payload = demand_payload()

    run_id, result, _ = run_once(pipeline)

    assert result["status"] == "SUCCEEDED"
    bronze, silver, gold = (pipeline.s3.object_keys(f"{layer}/") for layer in ("bronze", "silver", "gold"))
    assert len(bronze) == len(silver) == len(gold) == 1
    assert json.loads(pipeline.s3.read(bronze[0])) == payload  # Bronze is the untouched source response
    silver_frame = pd.read_parquet(io.BytesIO(pipeline.s3.read(silver[0])))
    assert len(silver_frame) == len(payload["Rows"]) and set(silver_frame["region"]) == {"ROI"}
    gold_frame = pd.read_parquet(io.BytesIO(pipeline.s3.read(gold[0])))
    assert sorted(gold_frame["date"].astype(str)) == ["2026-10-28", "2026-10-29"]
    assert gold_frame["observations"].sum() == len(payload["Rows"])
    assert (gold_frame["minimum_demand_mw"] <= gold_frame["average_demand_mw"]).all()
    assert (gold_frame["average_demand_mw"] <= gold_frame["maximum_demand_mw"]).all()

    run_date = gold[0].split("run_date=")[1].split("/")[0]
    assert pipeline.glue.registered == {run_date: f"s3://{BUCKET}/gold/eirgrid/daily_demand/run_date={run_date}/"}

    run = pipeline.store.get_run(run_id)
    assert (run["status"], run["stage"]) == ("succeeded", "completed")
    assert (run["bronze_key"], run["silver_key"], run["gold_key"]) == (bronze[0], silver[0], gold[0])
    assert run["days_back"] == 2 and run["requested_by"] == "operator-1" and "error" not in run
    assert pipeline.lock() is None


def test_each_stage_receives_exactly_the_keys_the_previous_stage_produced(pipeline):
    _, result, execution = run_once(pipeline)

    events = dict(execution.lambda_events)
    written = {call[2] for call in pipeline.calls if call[0] == "s3:PutObject"}
    bronze_key = events["Silver"]["bronze"]["output"]["key"]
    silver_key = events["Gold"]["silver"]["output"]["key"]
    gold_key = events["GlueRegistration"]["gold"]["output"]["key"]

    assert {bronze_key, silver_key, gold_key} == written  # nothing else was written, nothing is guessed
    assert result["output"]["bronze"]["output"]["key"] == bronze_key
    assert result["output"]["silver"]["output"]["key"] == silver_key
    assert result["output"]["glue_registration"]["output"] == {"key": gold_key, "partition_created": True}
    reads = [call[2] for call in pipeline.calls if call[0] == "s3:GetObject"]
    assert reads == [bronze_key, silver_key]  # each stage read only its input


def test_run_id_and_days_back_survive_every_stage_and_the_final_state(pipeline):
    run_id, result, execution = run_once(pipeline, days_back=7)

    assert [name for name, _ in execution.lambda_events] == ["Bronze", "Silver", "Gold", "GlueRegistration"]
    for _, event in execution.lambda_events:
        assert event["run_id"] == run_id and event["days_back"] == 7
    assert result["output"]["run_id"] == run_id and result["output"]["days_back"] == 7
    assert pipeline.store.get_run(run_id)["days_back"] == 7


def test_days_back_reaches_the_source_request(pipeline):
    run_once(pipeline, days_back=7)

    (request,) = pipeline.eirgrid.requests
    today = datetime.now(UTC).date()
    assert request["params"]["datefrom"] == (today - timedelta(days=7)).strftime("%d-%b-%Y")
    assert request["params"]["area"] == "demandActual" and request["params"]["region"] == "ROI"
    assert request["timeout"] == 30


def test_bronze_and_silver_names_belong_to_the_run_and_gold_keeps_the_daily_name(pipeline):
    run_id, _, _ = run_once(pipeline)

    (bronze,), (silver,), (gold,) = (pipeline.s3.object_keys(f"{layer}/") for layer in ("bronze", "silver", "gold"))
    assert bronze.startswith("bronze/eirgrid/demand_actual/ingestion_date=") and bronze.endswith(f"_{run_id}.json")
    assert silver.startswith("silver/eirgrid/demand_actual/ingestion_date=") and silver.endswith(f"_{run_id}.parquet")
    assert analytics.GOLD_KEY_PATTERN.match(gold) and pipeline_module.GOLD_KEY_PATTERN.match(gold)


def test_no_stage_triggers_another_and_only_the_expected_services_are_used(pipeline):
    run_once(pipeline)

    assert [stage for stage, _ in pipeline.handler_events] == ["bronze", "silver", "gold", "glue_registration"]
    assert {call[0] for call in pipeline.calls} == {"s3:PutObject", "s3:GetObject", "glue:GetTable", "glue:CreatePartition"}


@pytest.mark.parametrize(
    ("stage", "layers", "status"),
    [
        ("bronze", (), "failed"),
        ("silver", ("bronze",), "failed"),
        ("gold", ("bronze", "silver"), "failed"),
        ("glue_registration", ("bronze", "silver", "gold"), "partial_failure"),
    ],
)
def test_a_failure_leaves_only_the_completed_layers_and_a_matching_run_record(pipeline, stage, layers, status):
    run_id = pipeline.new_run()
    pipeline.fail(stage)

    result, _ = pipeline.execute(run_id)

    assert result["status"] == "FAILED"
    present = tuple(layer for layer in ("bronze", "silver", "gold") if pipeline.s3.object_keys(f"{layer}/"))
    assert present == layers  # S3 writes are atomic per object: no partial files, no later layers
    run = pipeline.store.get_run(run_id)
    assert (run["status"], run["stage"]) == (status, stage)
    assert pipeline.lock() is None and pipeline.glue.registered == {}


def test_a_partial_failure_can_be_repaired_with_the_registration_command(pipeline):
    run_id = pipeline.new_run()
    pipeline.fail("glue_registration")
    pipeline.execute(run_id)
    gold_key = pipeline.store.get_run(run_id)["gold_key"]

    created = pipeline_module.register_existing_gold_partition(gold_key)

    assert created is True and list(pipeline.glue.registered.values()) == [
        f"s3://{BUCKET}/{gold_key.rsplit('/', 1)[0]}/"
    ]


def test_the_store_and_the_state_machine_agree_on_the_run_record_schema(pipeline):
    run_id, _, _ = run_once(pipeline)

    item = pipeline.raw_items()[(f"{schema.RUN_PK_PREFIX}{run_id}", schema.RUN_SK)]
    assert set(item) == {
        "pk", "sk", "run_id", "status", "stage", "days_back", "created_at", "updated_at", "started_at",
        "finished_at", "bronze_key", "silver_key", "gold_key", "requested_by", "gsi1pk", "gsi1sk",
    }
