import json
from unittest.mock import Mock, patch

import pytest

from src.transformation import process_bronze_object, transform_demand_payload


def test_transform_demand_payload():
    payload = {
        "Rows": [
            {
                "EffectiveTime": "28-Oct-2026 12:00:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": "ROI",
                "Value": 4200,
            },
            {
                "EffectiveTime": "28-Oct-2026 11:00:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": "ROI",
                "Value": "4100",
            },
        ]
    }

    df = transform_demand_payload(payload)

    assert len(df) == 2
    assert list(df.columns) == [
        "effective_time_local",
        "source_timezone",
        "field_name",
        "region",
        "demand_mw",
    ]
    assert df.iloc[0]["demand_mw"] == 4100
    assert df.iloc[0]["region"] == "ROI"
    assert df.iloc[0]["source_timezone"] == "Europe/Dublin"


def test_transform_rejects_empty_rows():
    with pytest.raises(ValueError, match="no demand records"):
        transform_demand_payload({"Rows": []})


def test_transform_rejects_missing_columns():
    with pytest.raises(ValueError, match="Missing required columns"):
        transform_demand_payload({"Rows": [{"Value": 100}]})


def test_transform_rejects_invalid_timestamp():
    payload = {
        "Rows": [
            {
                "EffectiveTime": "not-a-date",
                "FieldName": "SYSTEM_DEMAND",
                "Region": "ROI",
                "Value": 100,
            }
        ]
    }

    with pytest.raises((ValueError, TypeError)):
        transform_demand_payload(payload)


def test_transform_rejects_non_numeric_demand():
    payload = {
        "Rows": [
            {
                "EffectiveTime": "28-Oct-2026 12:00:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": "ROI",
                "Value": "not-a-number",
            }
        ]
    }

    with pytest.raises((ValueError, TypeError)):
        transform_demand_payload(payload)


def test_transform_excludes_missing_demand_and_reports_count():
    payload = {
        "Rows": [
            {
                "EffectiveTime": "29-Oct-2026 19:45:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": "ROI",
                "Value": 4200,
            },
            {
                "EffectiveTime": "29-Oct-2026 20:00:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": "ROI",
                "Value": None,
            },
        ]
    }

    df = transform_demand_payload(payload)

    assert len(df) == 1
    assert df.iloc[0]["demand_mw"] == 4200
    assert df.attrs["excluded_missing_demand"] == 1


def test_process_bronze_object_uses_requested_key():
    payload = {
        "Rows": [
            {
                "EffectiveTime": "29-Oct-2026 12:00:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": "ROI",
                "Value": 4200,
            }
        ]
    }

    s3_client = Mock()
    s3_client.get_object.return_value = {
        "Body": Mock(read=Mock(return_value=json.dumps(payload).encode()))
    }

    with (
        patch("src.transformation.boto3.client", return_value=s3_client),
        patch(
            "src.transformation.save_silver_to_s3",
            return_value="silver/test.parquet",
        ) as save,
    ):
        result = process_bronze_object(
            "test-bucket",
            "bronze/test.json",
        )

    s3_client.get_object.assert_called_once_with(
        Bucket="test-bucket",
        Key="bronze/test.json",
    )
    assert save.call_args.args[1] == "test-bucket"
    assert result == "silver/test.parquet"
