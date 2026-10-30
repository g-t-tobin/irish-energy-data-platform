import io
import threading
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from botocore.exceptions import ClientError

from src import analytics
from src.analytics import (
    AnalyticsUnavailable,
    assess_quality,
    clear_analytics_cache,
    expected_observations,
    get_daily_demand,
    read_gold_daily_demand,
)

PREFIX = "gold/eirgrid/daily_demand/"


def _key(run_date):
    return f"{PREFIX}run_date={run_date}/daily_demand.parquet"


def _parquet(rows):
    buffer = io.BytesIO()
    pd.DataFrame(
        rows,
        columns=[
            "date",
            "region",
            "average_demand_mw",
            "minimum_demand_mw",
            "maximum_demand_mw",
            "observations",
        ],
    ).to_parquet(buffer, index=False)
    return buffer.getvalue()


def _s3(objects):
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": key} for key in objects]}
    ]
    s3.get_object.side_effect = lambda Bucket, Key: {
        "Body": io.BytesIO(objects[Key])
    }
    return s3


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv("EXPECTED_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("ANALYTICS_CACHE_TTL_SECONDS", raising=False)
    clear_analytics_cache()
    yield
    clear_analytics_cache()


def test_returns_structured_rows_sorted_with_later_run_overriding(env):
    s3 = _s3(
        {
            _key("2026-10-28"): _parquet(
                [
                    ("2026-10-27", "ROI", 100.0, 80.0, 120.0, 10),
                    ("2026-10-28", "ROI", 200.0, 150.0, 250.0, 20),
                ]
            ),
            _key("2026-10-29"): _parquet(
                [("2026-10-28", "ROI", 210.0, 150.0, 260.0, 96)]
            ),
            f"{PREFIX}run_date=2026-10-29/old_ts.parquet": b"ignored",
        }
    )

    with patch("src.analytics.boto3.client", return_value=s3):
        result = read_gold_daily_demand()

    assert result["source"] == "gold/eirgrid/daily_demand"
    assert "test-bucket" not in str(result)
    assert result["expected_interval_minutes"] == 15
    legacy = ["date", "region", "average_demand_mw", "minimum_demand_mw",
              "maximum_demand_mw", "observations"]
    assert [{k: r[k] for k in legacy} for r in result["days"]] == [
        {
            "date": "2026-10-27",
            "region": "ROI",
            "average_demand_mw": 100.0,
            "minimum_demand_mw": 80.0,
            "maximum_demand_mw": 120.0,
            "observations": 10,
        },
        {
            "date": "2026-10-28",
            "region": "ROI",
            "average_demand_mw": 210.0,
            "minimum_demand_mw": 150.0,
            "maximum_demand_mw": 260.0,
            "observations": 96,
        },
    ]
    assert [r["quality_status"] for r in result["days"]] == ["partial", "complete"]
    assert result["days"][1]["expected_observations"] == 96
    assert result["days"][1]["is_complete"] is True
    s3.put_object.assert_not_called()


def test_reads_only_most_recent_partitions(env):
    objects = {
        _key(f"2026-10-2{n}"): _parquet(
            [(f"2026-10-2{n}", "ROI", 1.0, 1.0, 1.0, 1)]
        )
        for n in range(1, 6)
    }
    s3 = _s3(objects)

    with patch("src.analytics.boto3.client", return_value=s3):
        result = read_gold_daily_demand(days=2)

    assert [row["date"] for row in result["days"]] == [
        "2026-10-24",
        "2026-10-25",
    ]
    assert s3.get_object.call_count == 2


def test_empty_gold_layer_returns_empty_list(env):
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{}]

    with patch("src.analytics.boto3.client", return_value=s3):
        assert read_gold_daily_demand()["days"] == []


def test_requires_bucket(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)

    with patch("src.analytics.boto3.client") as client:
        with pytest.raises(AnalyticsUnavailable, match="S3_BUCKET"):
            read_gold_daily_demand()

    client.assert_not_called()


def test_access_denied_is_reported_without_details(env):
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret detail"}},
        "ListObjectsV2",
    )

    with patch("src.analytics.boto3.client", return_value=s3):
        with pytest.raises(AnalyticsUnavailable) as exc:
            read_gold_daily_demand()

    assert "AccessDenied" in str(exc.value)
    assert "secret detail" not in str(exc.value)


def test_missing_columns_are_rejected(env):
    buffer = io.BytesIO()
    pd.DataFrame({"date": ["2026-10-29"]}).to_parquet(buffer, index=False)
    s3 = _s3({_key("2026-10-29"): buffer.getvalue()})

    with patch("src.analytics.boto3.client", return_value=s3):
        with pytest.raises(AnalyticsUnavailable, match="missing columns"):
            read_gold_daily_demand()


def test_endpoint_returns_503_when_unavailable():
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from src.api import daily_demand

    with patch(
        "src.analytics.read_gold_daily_demand",
        side_effect=AnalyticsUnavailable("nope"),
    ):
        with pytest.raises(HTTPException) as exc:
            daily_demand(days=30)

    assert exc.value.status_code == 503
    assert exc.value.detail == "Gold analytics are temporarily unavailable."
    assert "nope" not in exc.value.detail


def test_endpoint_passes_through_data():
    pytest.importorskip("fastapi")
    from src.api import daily_demand

    payload = {"source": "s3://b/p/", "days": []}
    clear_analytics_cache()
    with patch(
        "src.analytics.read_gold_daily_demand", return_value=payload
    ) as read:
        assert daily_demand(days=7) == payload

    read.assert_called_once_with(7)


def test_expected_observations_handles_dst_days():
    assert expected_observations("2026-10-27", 15) == 96
    assert expected_observations("2026-10-25", 15) == 100  # clocks go back
    assert expected_observations("2026-03-29", 15) == 92  # clocks go forward
    assert expected_observations("not-a-date", 15) is None
    assert expected_observations("2026-10-27", None) is None


def test_quality_states():
    assert assess_quality("2026-10-28", 96)["quality_status"] == "complete"
    partial = assess_quality("2026-10-29", 84)
    assert partial["quality_status"] == "partial"
    assert partial["is_complete"] is False
    assert partial["expected_observations"] == 96
    assert "12 interval" in partial["quality_note"]
    over = assess_quality("2026-10-28", 97)
    assert over["quality_status"] == "unknown"
    assert over["is_complete"] is None
    bad = assess_quality("garbage", 96)
    assert bad["quality_status"] == "unknown"
    assert bad["expected_observations"] is None


def test_quality_unknown_when_interval_disabled(monkeypatch):
    monkeypatch.setenv("EXPECTED_INTERVAL_MINUTES", "0")

    assert assess_quality("2026-10-28", 96)["quality_status"] == "unknown"


def _payload(marker="a"):
    return {"source": marker, "days": [{"date": "2026-10-28"}]}


def test_cache_hit_within_ttl(monkeypatch):
    monkeypatch.setattr(analytics, "_now", lambda: 100.0)
    with patch("src.analytics.read_gold_daily_demand", return_value=_payload()) as read:
        first = get_daily_demand(30)
        second = get_daily_demand(30)

    read.assert_called_once_with(30)
    assert first == second


def test_cache_refreshes_after_ttl(monkeypatch):
    monkeypatch.setenv("ANALYTICS_CACHE_TTL_SECONDS", "60")
    clock = {"t": 0.0}
    monkeypatch.setattr(analytics, "_now", lambda: clock["t"])
    with patch(
        "src.analytics.read_gold_daily_demand",
        side_effect=[_payload("a"), _payload("b")],
    ) as read:
        assert get_daily_demand(30)["source"] == "a"
        clock["t"] = 59.0
        assert get_daily_demand(30)["source"] == "a"
        clock["t"] = 61.0
        assert get_daily_demand(30)["source"] == "b"

    assert read.call_count == 2


def test_cache_entries_are_per_days_value():
    with patch(
        "src.analytics.read_gold_daily_demand",
        side_effect=lambda days: _payload(str(days)),
    ) as read:
        assert get_daily_demand(7)["source"] == "7"
        assert get_daily_demand(30)["source"] == "30"
        assert get_daily_demand(7)["source"] == "7"

    assert read.call_count == 2


def test_cached_result_cannot_be_mutated_by_callers():
    with patch("src.analytics.read_gold_daily_demand", return_value=_payload()):
        first = get_daily_demand(30)
        first["days"].append({"date": "tampered"})
        first["source"] = "tampered"
        second = get_daily_demand(30)

    assert second == _payload()


def test_failures_are_not_cached():
    with patch(
        "src.analytics.read_gold_daily_demand",
        side_effect=[AnalyticsUnavailable("down"), _payload()],
    ) as read:
        with pytest.raises(AnalyticsUnavailable):
            get_daily_demand(30)
        assert get_daily_demand(30) == _payload()

    assert read.call_count == 2


def test_empty_dataset_is_cached_only_as_success():
    empty = {"source": "s", "days": []}
    with patch("src.analytics.read_gold_daily_demand", return_value=empty) as read:
        assert get_daily_demand(30)["days"] == []
        assert get_daily_demand(30)["days"] == []

    read.assert_called_once()


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setenv("ANALYTICS_CACHE_TTL_SECONDS", "0")
    with patch("src.analytics.read_gold_daily_demand", return_value=_payload()) as read:
        get_daily_demand(30)
        get_daily_demand(30)

    assert read.call_count == 2


def test_clear_cache_forces_reload():
    with patch("src.analytics.read_gold_daily_demand", return_value=_payload()) as read:
        get_daily_demand(30)
        clear_analytics_cache()
        get_daily_demand(30)

    assert read.call_count == 2


def test_concurrent_requests_share_one_read():
    started = threading.Event()

    def slow_read(days):
        started.set()
        time.sleep(0.2)
        return _payload()

    results = []
    with patch("src.analytics.read_gold_daily_demand", side_effect=slow_read) as read:
        threads = [
            threading.Thread(target=lambda: results.append(get_daily_demand(30)))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    read.assert_called_once()
    assert len(results) == 8
    assert all(result == _payload() for result in results)
