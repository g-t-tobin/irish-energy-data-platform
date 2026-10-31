"""Read-only access to Gold daily-demand Parquet objects in S3."""

import copy
import io
import math
import os
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import pandas as pd

GOLD_PREFIX = "gold/eirgrid/daily_demand/"
GOLD_KEY_PATTERN = re.compile(
    r"^gold/eirgrid/daily_demand/run_date=(\d{4}-\d{2}-\d{2})/daily_demand\.parquet$"
)
METRIC_COLUMNS = [
    "date",
    "region",
    "average_demand_mw",
    "minimum_demand_mw",
    "maximum_demand_mw",
    "observations",
]


SOURCE_TIMEZONE = ZoneInfo("Europe/Dublin")
# Inferred from verified Gold data (96 observations on full days), not stated
# in the source code or API docs; override with EXPECTED_INTERVAL_MINUTES.
DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_CACHE_TTL_SECONDS = 300.0


PUBLIC_UNAVAILABLE_MESSAGE = "Gold analytics are temporarily unavailable."
PUBLIC_SOURCE_LABEL = GOLD_PREFIX.rstrip("/")
MAX_CACHE_TTL_SECONDS = 3600.0


class AnalyticsUnavailable(Exception):
    """Gold data cannot be read. The message is internal: log it, never return it."""


def _interval_minutes() -> int | None:
    raw = os.environ.get("EXPECTED_INTERVAL_MINUTES")
    if raw is None:
        return DEFAULT_INTERVAL_MINUTES
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def expected_observations(day: str, interval: int | None) -> int | None:
    """Intervals in the Europe/Dublin calendar day (92/100 on DST changes)."""
    if interval is None:
        return None
    try:
        start = datetime.combine(
            date.fromisoformat(day), datetime.min.time(), tzinfo=SOURCE_TIMEZONE
        )
    except ValueError:
        return None
    end = datetime.combine(
        start.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=SOURCE_TIMEZONE,
    )
    minutes = (end.astimezone(UTC) - start.astimezone(UTC)) / (
        timedelta(minutes=1)
    )
    return int(minutes // interval)


def assess_quality(day: str, observations: int) -> dict[str, Any]:
    """Compare observations with the expected interval count for the day."""
    expected = expected_observations(day, _interval_minutes())
    if expected is None:
        return {
            "expected_observations": None,
            "is_complete": None,
            "quality_status": "unknown",
            "quality_note": "Expected interval count could not be determined.",
        }
    if observations == expected:
        return {
            "expected_observations": expected,
            "is_complete": True,
            "quality_status": "complete",
            "quality_note": "All expected intervals are present.",
        }
    if observations < expected:
        return {
            "expected_observations": expected,
            "is_complete": False,
            "quality_status": "partial",
            "quality_note": (
                f"{expected - observations} interval(s) missing: the day may "
                "be in progress or have excluded missing measurements."
            ),
        }
    return {
        "expected_observations": expected,
        "is_complete": None,
        "quality_status": "unknown",
        "quality_note": "More observations than expected intervals.",
    }


def _list_gold_keys(s3: Any, bucket: str) -> list[tuple[str, str]]:
    keys: dict[str, str] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=GOLD_PREFIX):
        for obj in page.get("Contents", []):
            match = GOLD_KEY_PATTERN.match(obj["Key"])
            if match:
                keys[match.group(1)] = obj["Key"]
    return sorted((run_date, key) for run_date, key in keys.items())


def read_gold_daily_demand(days: int = 30) -> dict[str, Any]:
    """Return daily demand metrics from the most recent Gold partitions."""
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise AnalyticsUnavailable("S3_BUCKET is not configured on the API.")

    try:
        s3 = boto3.client("s3")
        partitions = _list_gold_keys(s3, bucket)[-days:]

        # Later run_dates override earlier ones for the same date and region.
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for _, key in partitions:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            frame = pd.read_parquet(io.BytesIO(body))
            missing = set(METRIC_COLUMNS) - set(frame.columns)
            if missing:
                raise AnalyticsUnavailable(
                    f"Gold object {key} is missing columns: {sorted(missing)}"
                )
            for row in frame[METRIC_COLUMNS].itertuples(index=False):
                observations = int(row.observations)
                merged[(str(row.date), str(row.region))] = {
                    "date": str(row.date),
                    "region": str(row.region),
                    "average_demand_mw": float(row.average_demand_mw),
                    "minimum_demand_mw": float(row.minimum_demand_mw),
                    "maximum_demand_mw": float(row.maximum_demand_mw),
                    "observations": observations,
                    **assess_quality(str(row.date), observations),
                }
    except AnalyticsUnavailable:
        raise
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        raise AnalyticsUnavailable(
            f"Unable to read Gold data from S3 ({code or type(exc).__name__})."
        ) from exc

    rows = sorted(merged.values(), key=lambda r: (r["date"], r["region"]))
    return {
        "source": PUBLIC_SOURCE_LABEL,
        "expected_interval_minutes": _interval_minutes(),
        "days": rows,
    }


_now = time.monotonic
_cache_lock = threading.Lock()
_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_key_locks: dict[int, threading.Lock] = {}


def _cache_ttl() -> float:
    try:
        ttl = float(
            os.environ.get("ANALYTICS_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS)
        )
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS
    if not math.isfinite(ttl):
        return DEFAULT_CACHE_TTL_SECONDS
    return min(ttl, MAX_CACHE_TTL_SECONDS)


def _fresh(days: int) -> dict[str, Any] | None:
    entry = _cache.get(days)
    if entry and entry[0] > _now():
        return copy.deepcopy(entry[1])
    return None


def get_daily_demand(days: int = 30) -> dict[str, Any]:
    """Cached read_gold_daily_demand; failures are never cached."""
    ttl = _cache_ttl()
    if ttl <= 0:
        return read_gold_daily_demand(days)

    with _cache_lock:
        cached = _fresh(days)
        if cached is not None:
            return cached
        key_lock = _key_locks.setdefault(days, threading.Lock())

    # One S3 read per key; waiting callers reuse the result.
    with key_lock:
        with _cache_lock:
            cached = _fresh(days)
        if cached is not None:
            return cached

        result = read_gold_daily_demand(days)
        with _cache_lock:
            _cache[days] = (_now() + ttl, copy.deepcopy(result))
        return result


def clear_analytics_cache() -> None:
    """Internal helper for tests; deliberately not exposed over HTTP."""
    with _cache_lock:
        _cache.clear()
