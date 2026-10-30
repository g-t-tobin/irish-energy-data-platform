"""Scheduled run reconciler (AWS Lambda handler). Defined but not scheduled or deployed by default.

It inspects Step Functions executions and updates the run-history table through the shared
src/reconciliation.py logic. It cannot start an execution and uses no S3, Glue or Athena.
Logs are one JSON object per line with a fixed set of fields (run IDs, statuses and counts only;
no payloads, tokens or exception messages). The summary line is in CloudWatch Embedded Metric
Format, so its metrics need no extra permission and have no dimensions (bounded cardinality).
"""

import json
import os
import re
import time
from typing import Any

import boto3

from src.dynamodb_run_store import DynamoRunStore
from src.executor_stepfunctions import _ARN_RE
from src.reconciliation import reconcile
from src.run_store import RunStoreError

NAMESPACE = "IrishEnergy/Reconciler"
_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_COUNT_METRICS = {
    "checked": "RunsChecked",
    "stale": "StaleActiveRuns",
    "settled": "RunsSettled",
    "interrupted": "RunsInterrupted",
    "renewed": "LeasesRenewed",
    "released": "LocksReleased",
    "unchanged": "RunsUnchanged",
    "errors": "ReconcileErrors",
}
_LEVEL = {"error": "ERROR", "settled": "WARNING", "interrupted": "WARNING", "released": "WARNING"}


class ReconcilerConfigurationError(RuntimeError):
    """The function environment is missing or malformed."""


def _emit(record: dict[str, Any]) -> None:
    print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


def _configuration() -> tuple[str, str]:
    table = (os.environ.get("RUN_HISTORY_TABLE") or "").strip()
    arn = (os.environ.get("PIPELINE_STATE_MACHINE_ARN") or "").strip()
    if not _TABLE_RE.match(table):
        raise ReconcilerConfigurationError("RUN_HISTORY_TABLE is missing or invalid")
    if not _ARN_RE.match(arn):
        raise ReconcilerConfigurationError("PIPELINE_STATE_MACHINE_ARN is missing or invalid")
    return table, arn


def _run_event(event: dict[str, Any]) -> None:
    _emit({"event": "reconcile.run", "level": _LEVEL.get(event["action"], "INFO"), **event})


def _metrics_record(name: str, values: dict[str, float], units: dict[str, str]) -> dict[str, Any]:
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [[]],
                    "Metrics": [{"Name": metric, "Unit": units[metric]} for metric in values],
                }
            ],
        },
        "event": name,
        **values,
    }


def handler(event: Any = None, context: Any = None) -> dict[str, int]:
    table, state_machine_arn = _configuration()
    started = time.monotonic()
    try:
        store = DynamoRunStore(table, boto3.client("dynamodb"))
        counts = reconcile(store, boto3.client("stepfunctions"), state_machine_arn, on_event=_run_event)
    except RunStoreError as exc:
        record = _metrics_record("reconcile.failed", {"ReconcileFailures": 1}, {"ReconcileFailures": "Count"})
        _emit({**record, "level": "ERROR", "error_type": type(exc).__name__})
        raise

    metrics = {_COUNT_METRICS[key]: value for key, value in counts.items()}
    metrics["DurationMs"] = round((time.monotonic() - started) * 1000)
    units = {name: "Count" for name in metrics} | {"DurationMs": "Milliseconds"}
    _emit({**_metrics_record("reconcile.summary", metrics, units), "level": "ERROR" if counts["errors"] else "INFO"})
    return counts
