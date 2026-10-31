import io
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow.parquet as pq
import pytest

from src.gold import build_daily_metrics, main, process_silver_object


def test_build_daily_metrics():
    df = pd.DataFrame(
        {
            "effective_time_local": pd.to_datetime(
                [
                    "2026-10-27 00:00:00",
                    "2026-10-27 00:15:00",
                    "2026-10-28 00:00:00",
                ]
            ),
            "region": ["ROI", "ROI", "ROI"],
            "demand_mw": [100.0, 200.0, 300.0],
        }
    )

    result = build_daily_metrics(df)

    assert len(result) == 2

    first_day = result[result["date"] == "2026-10-27"].iloc[0]
    assert first_day["average_demand_mw"] == 150.0
    assert first_day["minimum_demand_mw"] == 100.0
    assert first_day["maximum_demand_mw"] == 200.0
    assert first_day["observations"] == 2


def test_build_daily_metrics_rejects_empty_data():
    df = pd.DataFrame(
        columns=["effective_time_local", "region", "demand_mw"]
    )

    with pytest.raises(ValueError, match="empty"):
        build_daily_metrics(df)


def test_build_daily_metrics_rejects_missing_demand():
    df = pd.DataFrame(
        {
            "effective_time_local": pd.to_datetime(["2026-10-27 00:00:00"]),
            "region": ["ROI"],
            "demand_mw": [None],
        }
    )

    with pytest.raises(ValueError, match="null"):
        build_daily_metrics(df)


def _silver_parquet_bytes():
    df = pd.DataFrame(
        {
            "effective_time_local": pd.to_datetime(
                ["2026-10-27 00:00:00", "2026-10-27 00:15:00"]
            ),
            "region": ["ROI", "ROI"],
            "demand_mw": [100.0, 200.0],
        }
    )
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    return buffer.getvalue()


def test_process_silver_object_reads_key_and_writes_gold():
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(_silver_parquet_bytes())}

    with patch("src.gold.boto3.client", return_value=s3):
        key = process_silver_object("bucket", "silver/x.parquet")

    s3.get_object.assert_called_once_with(Bucket="bucket", Key="silver/x.parquet")
    s3.put_object.assert_called_once()
    kwargs = s3.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "bucket"
    assert kwargs["Key"] == key
    assert key.startswith("gold/eirgrid/daily_demand/run_date=")
    assert key.endswith(".parquet")

    written = pd.read_parquet(io.BytesIO(kwargs["Body"]))
    assert written.iloc[0]["average_demand_mw"] == 150.0
    assert written.iloc[0]["observations"] == 2


@pytest.mark.parametrize("bucket,key", [("", "k"), ("b", "")])
def test_process_silver_object_requires_bucket_and_key(bucket, key):
    with patch("src.gold.boto3.client") as client, pytest.raises(ValueError):
        process_silver_object(bucket, key)
    client.assert_not_called()


def test_process_silver_object_does_not_write_on_invalid_data():
    s3 = MagicMock()
    bad = pd.DataFrame({"region": ["ROI"]})
    buffer = io.BytesIO()
    bad.to_parquet(buffer, index=False)
    s3.get_object.return_value = {"Body": io.BytesIO(buffer.getvalue())}

    with (
        patch("src.gold.boto3.client", return_value=s3),
        pytest.raises(ValueError, match="Missing"),
    ):
        process_silver_object("bucket", "silver/bad.parquet")

    s3.put_object.assert_not_called()


def test_main_processes_latest_silver_object(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "bucket")
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "silver/old.parquet", "LastModified": datetime(2026, 1, 1)},
            {"Key": "silver/new.parquet", "LastModified": datetime(2026, 2, 1)},
        ]
    }

    with patch("src.gold.boto3.client", return_value=s3), patch(
        "src.gold.process_silver_object", return_value="gold/out.parquet"
    ) as process:
        main()

    process.assert_called_once_with("bucket", "silver/new.parquet")


def _run_at(moment):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(_silver_parquet_bytes())}
    with patch("src.gold.boto3.client", return_value=s3), patch(
        "src.gold.datetime"
    ) as fake_datetime:
        fake_datetime.now.return_value = moment
        key = process_silver_object("bucket", "silver/x.parquet")
    return key, s3


def test_gold_key_is_canonical_and_uses_utc_date():
    key, s3 = _run_at(datetime(2026, 10, 29, 23, 59, 59, tzinfo=UTC))

    assert key == "gold/eirgrid/daily_demand/run_date=2026-10-29/daily_demand.parquet"
    assert s3.put_object.call_args.kwargs["Key"] == key


def test_gold_key_is_deterministic_and_overwrites_same_object():
    first, s3_first = _run_at(datetime(2026, 10, 29, 0, 0, 1, tzinfo=UTC))
    second, s3_second = _run_at(datetime(2026, 10, 29, 18, 30, 0, tzinfo=UTC))

    assert first == second
    s3_first.put_object.assert_called_once()
    s3_second.put_object.assert_called_once()
    s3_second.list_objects_v2.assert_not_called()


def test_gold_key_changes_with_utc_date():
    first, _ = _run_at(datetime(2026, 10, 29, 23, 59, tzinfo=UTC))
    second, _ = _run_at(datetime(2026, 10, 30, 0, 0, tzinfo=UTC))

    assert first != second
    assert second.endswith("run_date=2026-10-30/daily_demand.parquet")


def test_gold_output_keeps_parquet_settings():
    _, s3 = _run_at(datetime(2026, 10, 29, tzinfo=UTC))
    kwargs = s3.put_object.call_args.kwargs

    assert kwargs["ContentType"] == "application/vnd.apache.parquet"
    metadata = pq.ParquetFile(io.BytesIO(kwargs["Body"])).metadata
    assert metadata.row_group(0).column(0).compression == "SNAPPY"
    assert list(
        pd.read_parquet(io.BytesIO(kwargs["Body"])).columns
    ) == [
        "date",
        "region",
        "average_demand_mw",
        "minimum_demand_mw",
        "maximum_demand_mw",
        "observations",
    ]
