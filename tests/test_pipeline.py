import copy
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.pipeline import (
    main,
    register_existing_gold_partition,
    register_gold_partition,
    run_pipeline,
)

GOLD_KEY = "gold/eirgrid/daily_demand/run_date=2026-10-29/daily_demand.parquet"


TABLE_DESCRIPTOR = {
    "Location": "s3://test-bucket/gold/eirgrid/daily_demand/",
    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
    "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
    "SerdeInfo": {
        "SerializationLibrary": (
            "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
        )
    },
    "Columns": [{"Name": "date", "Type": "string"}],
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("EIRGRID_DAYS_BACK", "2")


class AlreadyExistsException(ClientError):
    pass


def _glue_client():
    glue = MagicMock()
    glue.exceptions.AlreadyExistsException = AlreadyExistsException
    glue.get_table.return_value = {
        "Table": {"StorageDescriptor": copy.deepcopy(TABLE_DESCRIPTOR)}
    }
    return glue


def test_run_pipeline_passes_exact_keys_in_order(env):
    calls = MagicMock()
    payload = {"Rows": []}
    calls.fetch.return_value = payload
    calls.save.return_value = "bronze/a.json"
    calls.silver.return_value = "silver/b.parquet"
    calls.gold.return_value = GOLD_KEY
    calls.register.return_value = True

    with (
        patch("src.pipeline.fetch_demand_data", calls.fetch),
        patch("src.pipeline.save_to_s3", calls.save),
        patch("src.pipeline.process_bronze_object", calls.silver),
        patch("src.pipeline.process_silver_object", calls.gold),
        patch("src.pipeline.register_gold_partition", calls.register),
    ):
        result = run_pipeline()

    assert [c[0] for c in calls.mock_calls] == [
        "fetch", "save", "silver", "gold", "register"
    ]
    calls.fetch.assert_called_once_with(days_back=2)
    calls.save.assert_called_once_with(payload, "test-bucket")
    calls.silver.assert_called_once_with("test-bucket", "bronze/a.json")
    calls.gold.assert_called_once_with("test-bucket", "silver/b.parquet")
    calls.register.assert_called_once_with("test-bucket", GOLD_KEY)
    assert result == {
        "bronze_key": "bronze/a.json",
        "silver_key": "silver/b.parquet",
        "gold_key": GOLD_KEY,
    }


def test_run_pipeline_requires_bucket(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)

    with pytest.raises(ValueError, match="S3_BUCKET"):
        run_pipeline()


@pytest.mark.parametrize("failing", ["fetch", "save", "silver", "gold"])
def test_run_pipeline_stops_after_stage_failure(env, failing):
    mocks = {
        "fetch": MagicMock(return_value={"Rows": []}),
        "save": MagicMock(return_value="bronze/a.json"),
        "silver": MagicMock(return_value="silver/b.parquet"),
        "gold": MagicMock(return_value=GOLD_KEY),
    }
    mocks[failing].side_effect = RuntimeError("boom")
    register = MagicMock()
    order = ["fetch", "save", "silver", "gold"]

    with (
        patch("src.pipeline.fetch_demand_data", mocks["fetch"]),
        patch("src.pipeline.save_to_s3", mocks["save"]),
        patch("src.pipeline.process_bronze_object", mocks["silver"]),
        patch("src.pipeline.process_silver_object", mocks["gold"]),
        patch("src.pipeline.register_gold_partition", register),
        pytest.raises(RuntimeError, match="boom"),
    ):
        run_pipeline()

    for later in order[order.index(failing) + 1:]:
        mocks[later].assert_not_called()
    register.assert_not_called()


def test_run_pipeline_propagates_registration_failure(env):
    with (
        patch("src.pipeline.fetch_demand_data", return_value={"Rows": []}),
        patch("src.pipeline.save_to_s3", return_value="bronze/a.json"),
        patch("src.pipeline.process_bronze_object", return_value="silver/b.parquet"),
        patch("src.pipeline.process_silver_object", return_value=GOLD_KEY),
        patch(
            "src.pipeline.register_gold_partition",
            side_effect=RuntimeError("glue down"),
        ),pytest.raises(RuntimeError, match="glue down")
    ):
        run_pipeline()


def test_register_gold_partition_creates_partition():
    glue = _glue_client()

    with patch("src.pipeline.boto3.client", return_value=glue) as client:
        assert register_gold_partition("test-bucket", GOLD_KEY) is True

    client.assert_called_once_with("glue", region_name="eu-west-1")
    glue.get_table.assert_called_once_with(
        DatabaseName="irish_energy_analytics", Name="daily_electricity_demand"
    )
    kwargs = glue.create_partition.call_args.kwargs
    assert kwargs["DatabaseName"] == "irish_energy_analytics"
    assert kwargs["TableName"] == "daily_electricity_demand"
    assert kwargs["PartitionInput"]["Values"] == ["2026-10-29"]
    assert kwargs["PartitionInput"]["StorageDescriptor"]["Location"] == (
        "s3://test-bucket/gold/eirgrid/daily_demand/run_date=2026-10-29/"
    )


def test_register_gold_partition_preserves_table_descriptor():
    glue = _glue_client()

    with patch("src.pipeline.boto3.client", return_value=glue):
        register_gold_partition("test-bucket", GOLD_KEY)

    used = glue.create_partition.call_args.kwargs["PartitionInput"][
        "StorageDescriptor"
    ]
    expected = copy.deepcopy(TABLE_DESCRIPTOR)
    expected["Location"] = (
        "s3://test-bucket/gold/eirgrid/daily_demand/run_date=2026-10-29/"
    )
    assert used == expected


@pytest.mark.parametrize(
    "run_date", ["2026-13-01", "2026-02-30", "2026-00-10", "2026-10-32"]
)
def test_register_gold_partition_rejects_invalid_calendar_date(run_date):
    key = f"gold/eirgrid/daily_demand/run_date={run_date}/f.parquet"

    with (
        patch("src.pipeline.boto3.client") as client,
        pytest.raises(ValueError, match="Invalid run_date"),
    ):
        register_gold_partition("test-bucket", key)

    client.assert_not_called()


@pytest.mark.parametrize(
    "field", ["Columns", "InputFormat", "OutputFormat", "SerdeInfo"]
)
def test_register_gold_partition_rejects_incomplete_descriptor(field):
    glue = _glue_client()
    del glue.get_table.return_value["Table"]["StorageDescriptor"][field]

    with (
        patch("src.pipeline.boto3.client", return_value=glue),
        pytest.raises(ValueError, match=field),
    ):
        register_gold_partition("test-bucket", GOLD_KEY)

    glue.create_partition.assert_not_called()


def test_register_gold_partition_rejects_missing_descriptor():
    glue = _glue_client()
    glue.get_table.return_value = {"Table": {}}

    with (
        patch("src.pipeline.boto3.client", return_value=glue),
        pytest.raises(ValueError, match="missing"),
    ):
        register_gold_partition("test-bucket", GOLD_KEY)

    glue.create_partition.assert_not_called()


def test_register_gold_partition_is_idempotent():
    glue = _glue_client()
    glue.create_partition.side_effect = AlreadyExistsException(
        {"Error": {"Code": "AlreadyExistsException", "Message": "x"}},
        "CreatePartition",
    )

    with patch("src.pipeline.boto3.client", return_value=glue):
        assert register_gold_partition("test-bucket", GOLD_KEY) is False


def test_register_gold_partition_does_not_hide_other_errors():
    glue = _glue_client()
    glue.create_partition.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "CreatePartition",
    )

    with (
        patch("src.pipeline.boto3.client", return_value=glue),
        pytest.raises(ClientError, match="AccessDenied"),
    ):
        register_gold_partition("test-bucket", GOLD_KEY)


def test_register_gold_partition_rejects_unexpected_key():
    with (
        patch("src.pipeline.boto3.client") as client,
        pytest.raises(ValueError, match="Unexpected Gold key"),
    ):
        register_gold_partition("test-bucket", "gold/other/file.parquet")

    client.assert_not_called()


def test_register_gold_partition_accepts_canonical_key():
    glue = _glue_client()

    with patch("src.pipeline.boto3.client", return_value=glue):
        assert register_gold_partition("test-bucket", GOLD_KEY) is True

    assert glue.create_partition.call_args.kwargs["PartitionInput"]["Values"] == [
        "2026-10-29"
    ]


STAGE_TARGETS = [
    "src.pipeline.fetch_demand_data",
    "src.pipeline.save_to_s3",
    "src.pipeline.process_bronze_object",
    "src.pipeline.process_silver_object",
]


def test_register_partition_command_registers_given_key_only(env):
    glue = _glue_client()
    stages = [patch(t) for t in STAGE_TARGETS]
    mocks = [p.start() for p in stages]
    try:
        with patch("src.pipeline.boto3.client", return_value=glue):
            main(["register-partition", "--gold-key", GOLD_KEY])
    finally:
        for p in stages:
            p.stop()

    for mock in mocks:
        mock.assert_not_called()
    kwargs = glue.create_partition.call_args.kwargs
    assert kwargs["PartitionInput"]["Values"] == ["2026-10-29"]
    assert kwargs["PartitionInput"]["StorageDescriptor"]["Location"] == (
        "s3://test-bucket/gold/eirgrid/daily_demand/run_date=2026-10-29/"
    )


def test_register_partition_command_does_not_run_pipeline(env):
    with (
        patch("src.pipeline.run_pipeline") as run,
        patch("src.pipeline.register_gold_partition", return_value=True) as reg,
    ):
        main(["register-partition", "--gold-key", GOLD_KEY])

    run.assert_not_called()
    reg.assert_called_once_with("test-bucket", GOLD_KEY)


def test_register_partition_command_requires_gold_key(env):
    with (
        patch("src.pipeline.register_gold_partition") as reg,
        pytest.raises(SystemExit) as exc,
    ):
        main(["register-partition"])

    assert exc.value.code == 2
    reg.assert_not_called()


@pytest.mark.parametrize(
    "key",
    [
        "",
        "gold/other/file.parquet",
        "gold/eirgrid/daily_demand/run_date=2026-02-30/daily_demand.parquet",
    ],
)
def test_register_existing_rejects_malformed_key_without_aws(env, key):
    with patch("src.pipeline.boto3.client") as client, pytest.raises(ValueError):
        register_existing_gold_partition(key)

    client.assert_not_called()


def test_register_existing_requires_bucket(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)

    with (
        patch("src.pipeline.register_gold_partition") as reg,
        pytest.raises(ValueError, match="S3_BUCKET"),
    ):
        register_existing_gold_partition(GOLD_KEY)

    reg.assert_not_called()


def test_register_existing_handles_existing_partition(env):
    glue = _glue_client()
    glue.create_partition.side_effect = AlreadyExistsException(
        {"Error": {"Code": "AlreadyExistsException", "Message": "x"}},
        "CreatePartition",
    )

    with patch("src.pipeline.boto3.client", return_value=glue):
        assert register_existing_gold_partition(GOLD_KEY) is False


def test_register_existing_propagates_aws_errors(env):
    glue = _glue_client()
    glue.create_partition.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "CreatePartition",
    )

    with (
        patch("src.pipeline.boto3.client", return_value=glue),
        pytest.raises(ClientError, match="AccessDenied"),
    ):
        register_existing_gold_partition(GOLD_KEY)


def test_default_main_runs_full_pipeline():
    with patch("src.pipeline.run_pipeline", return_value={}) as run:
        main([])

    run.assert_called_once_with()
