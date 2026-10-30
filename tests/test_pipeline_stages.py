"""Lambda stage handlers: input validation, contracts, and the S3/Glue calls they make.

All AWS access is replaced by in-memory doubles; nothing here reaches AWS or the network.
"""

import io
import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src import pipeline_stages as stages

RUN_ID = "0b6f4c1e-8a52-4d57-9a2f-3c1e5b7d9a10"
BUCKET = "test-lake-bucket"
PAYLOAD = {
    "Rows": [
        {"EffectiveTime": "28-Oct-2026 11:00:00", "FieldName": "SYSTEM_DEMAND", "Region": "ROI", "Value": 4100},
        {"EffectiveTime": "28-Oct-2026 12:00:00", "FieldName": "SYSTEM_DEMAND", "Region": "ROI", "Value": 4200},
    ]
}


class AlreadyExists(ClientError):
    def __init__(self):
        super().__init__({"Error": {"Code": "AlreadyExistsException"}}, "CreatePartition")


class FakeS3:
    def __init__(self, calls):
        self.objects, self.calls = {}, calls

    def put_object(self, Bucket, Key, Body, **_):
        self.calls.append(("s3:PutObject", Bucket, Key))
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        self.calls.append(("s3:GetObject", Bucket, Key))
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeGlue:
    exceptions = MagicMock(AlreadyExistsException=AlreadyExists)

    def __init__(self, calls):
        self.calls, self.partitions = calls, set()

    def get_table(self, DatabaseName, Name):
        self.calls.append(("glue:GetTable", DatabaseName, Name))
        descriptor = {"Columns": [{"Name": "a"}], "InputFormat": "i", "OutputFormat": "o", "SerdeInfo": {"x": 1}}
        return {"Table": {"StorageDescriptor": descriptor}}

    def create_partition(self, DatabaseName, TableName, PartitionInput):
        self.calls.append(("glue:CreatePartition", DatabaseName, TableName))
        value = PartitionInput["Values"][0]
        if value in self.partitions:
            raise AlreadyExists()
        self.partitions.add(value)


@pytest.fixture
def aws(monkeypatch):
    """Replace boto3 clients and the EirGrid call; records every S3/Glue operation by stage."""
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    calls: list[tuple] = []
    s3, glue = FakeS3(calls), FakeGlue(calls)

    def client(service, **_):
        return {"s3": s3, "glue": glue}[service]  # any other service raises KeyError

    with patch("boto3.client", side_effect=client), patch.object(stages, "fetch_demand_data", return_value=PAYLOAD):
        yield type("AWS", (), {"calls": calls, "s3": s3, "glue": glue})


def run_all_stages():
    state = {"run_id": RUN_ID, "days_back": 2}
    per_stage = {}
    for key, handler in (
        ("bronze", stages.ingest_handler),
        ("silver", stages.silver_handler),
        ("gold", stages.gold_handler),
        ("glue_registration", stages.glue_handler),
    ):
        state[key] = {"output": handler(dict(state))}
        per_stage[key] = state[key]["output"]
    return per_stage


def test_stages_chain_through_the_step_functions_state_shape(aws):
    results = run_all_stages()

    assert results["bronze"]["key"].startswith("bronze/eirgrid/demand_actual/ingestion_date=")
    assert results["bronze"]["records"] == 2
    assert results["silver"]["key"].startswith("silver/eirgrid/demand_actual/")
    assert results["gold"]["key"].startswith("gold/eirgrid/daily_demand/run_date=")
    assert results["glue_registration"] == {"key": results["gold"]["key"], "partition_created": True}


def test_glue_registration_is_idempotent(aws):
    gold_event = {"run_id": RUN_ID, "gold": {"output": {"key": "gold/eirgrid/daily_demand/run_date=2026-10-28/daily_demand.parquet"}}}

    assert stages.glue_handler(gold_event)["partition_created"] is True
    assert stages.glue_handler(gold_event)["partition_created"] is False


def test_only_the_expected_s3_and_glue_operations_are_used(aws):
    run_all_stages()

    assert {c[0] for c in aws.calls} == {"s3:PutObject", "s3:GetObject", "glue:GetTable", "glue:CreatePartition"}
    assert {c[1] for c in aws.calls if c[0].startswith("s3")} == {BUCKET}
    assert {(c[1], c[2]) for c in aws.calls if c[0].startswith("glue")} == {
        ("irish_energy_analytics", "daily_electricity_demand")
    }


@pytest.mark.parametrize(
    "handler",
    [stages.ingest_handler, stages.silver_handler, stages.gold_handler, stages.glue_handler],
)
@pytest.mark.parametrize("event", [None, [], "x", {}, {"run_id": "not-a-uuid"}, {"run_id": RUN_ID.upper()}, {"run_id": 5}])
def test_invalid_events_fail_before_any_aws_call(aws, handler, event):
    with pytest.raises(stages.StageInputError):
        handler(event)

    assert aws.calls == []


@pytest.mark.parametrize("days_back", [0, -1, 31, 2.5, "2", None, True])
def test_ingestion_validates_days_back(aws, days_back):
    with pytest.raises(stages.StageInputError):
        stages.ingest_handler({"run_id": RUN_ID, "days_back": days_back})

    assert aws.calls == []


@pytest.mark.parametrize(
    ("handler", "stage", "bad_key"),
    [
        (stages.silver_handler, "bronze", "silver/eirgrid/demand_actual/ingestion_date=2026-10-28/x.parquet"),
        (stages.silver_handler, "bronze", "../bronze/eirgrid/demand_actual/ingestion_date=2026-10-28/x.json"),
        (stages.gold_handler, "silver", "bronze/eirgrid/demand_actual/ingestion_date=2026-10-28/x.json"),
        (stages.gold_handler, "silver", "silver/other/ingestion_date=2026-10-28/x.parquet"),
        (stages.glue_handler, "gold", "gold/eirgrid/daily_demand/run_date=not-a-date/x.parquet"),
        (stages.glue_handler, "gold", "silver/eirgrid/demand_actual/ingestion_date=2026-10-28/x.parquet"),
        (stages.glue_handler, "gold", 7),
    ],
)
def test_downstream_stages_reject_missing_or_foreign_keys(aws, handler, stage, bad_key):
    for event in (
        {"run_id": RUN_ID},
        {"run_id": RUN_ID, stage: {}},
        {"run_id": RUN_ID, stage: {"output": {}}},
        {"run_id": RUN_ID, stage: {"output": {"key": bad_key}}},
    ):
        with pytest.raises(stages.StageInputError):
            handler(event)

    assert aws.calls == []


def test_missing_bucket_configuration_is_an_explicit_error(aws, monkeypatch):
    monkeypatch.delenv("S3_BUCKET")

    with pytest.raises(stages.StageConfigurationError):
        stages.ingest_handler({"run_id": RUN_ID, "days_back": 2})


def test_upstream_and_aws_failures_are_not_swallowed(aws):
    with patch.object(stages, "fetch_demand_data", side_effect=RuntimeError("EirGrid down")), pytest.raises(
        RuntimeError, match="EirGrid down"
    ):
        stages.ingest_handler({"run_id": RUN_ID, "days_back": 2})

    event = {"run_id": RUN_ID, "bronze": {"output": {"key": "bronze/eirgrid/demand_actual/ingestion_date=2026-10-28/x.json"}}}
    with pytest.raises(KeyError):  # the object does not exist in the fake bucket
        stages.silver_handler(event)


def test_handlers_log_without_secrets_or_payloads(aws, caplog):
    caplog.set_level(logging.INFO)

    run_all_stages()

    text = " ".join(record.getMessage() for record in caplog.records)
    assert RUN_ID in text and "stage=bronze" in text
    assert "4100" not in text and "SYSTEM_DEMAND" not in text
    assert "AWS_SECRET" not in text and "token" not in text.lower()


def test_importing_the_handlers_needs_no_credentials_or_clients():
    with patch("boto3.client", side_effect=AssertionError("client created at import")):
        import importlib

        importlib.reload(stages)

    assert json.dumps(sorted(n for n in dir(stages) if n.endswith("_handler"))) == json.dumps(
        ["glue_handler", "gold_handler", "ingest_handler", "silver_handler"]
    )


def test_the_api_does_not_import_or_use_the_stage_handlers():
    from pathlib import Path

    source = (Path(stages.__file__).parent / "api.py").read_text()

    assert "pipeline_stages" not in source
    assert "stepfunctions" not in source and "boto3.client(\"lambda\"" not in source
