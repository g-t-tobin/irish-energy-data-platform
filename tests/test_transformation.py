import pytest

from src.transformation import transform_demand_payload


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
