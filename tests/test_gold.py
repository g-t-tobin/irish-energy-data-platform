import pandas as pd
import pytest

from src.gold import build_daily_metrics


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
