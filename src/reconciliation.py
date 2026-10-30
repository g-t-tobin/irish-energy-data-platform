"""Settle run history from the real Step Functions outcome.

Shared by the API (startup or a later activation phase) and by the scheduled reconciler function
(src/reconcile_runs.py). It only reads executions (DescribeExecution) and updates the run-history
table; it never starts an execution and never touches S3 or Glue.

Rules:
- The execution status decides. A run is marked interrupted only when its execution is aborted or
  does not exist (after a grace period), never while the execution is still running.
- A lock is released only by the run that owns it, and only once that run's execution is over.
- A run that is still running keeps its lock: the lease is renewed, so an expired lease alone can
  never let another run start behind a live execution.
- If the execution cannot be queried (throttling, permissions, network) nothing changes.
- Every step is a conditional write, so repeating the pass or running two at once is safe.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src.run_store import (
    ACTIVE_STATUSES,
    INTERRUPTED_MESSAGE,
    RunHistory,
    RunStoreError,
)

logger = logging.getLogger(__name__)

MISSING_EXECUTION_GRACE_SECONDS = 300
STALE_AFTER_SECONDS = 600  # an active run untouched this long is reported as stale (it is still checked)
RENEW_WHEN_LEASE_BELOW_SECONDS = 20 * 60
INTERRUPTED_RECHECK_SECONDS = 24 * 3600  # how long a run marked interrupted can still be corrected
RECONCILE_LIST_LIMIT = 50

_NOT_FINISHED = {"RUNNING", "PENDING_REDRIVE"}
_FAILED = {"FAILED", "TIMED_OUT"}
COUNT_KEYS = ("checked", "stale", "settled", "interrupted", "renewed", "released", "unchanged", "errors")

EventSink = Callable[[dict[str, Any]], None]


def execution_arn(state_machine_arn: str, run_id: str) -> str:
    """The execution is named after the run, so its ARN needs no storage."""
    return state_machine_arn.replace(":stateMachine:", ":execution:") + f":{run_id}"


def _age_seconds(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return None


def _is_stale(run: dict[str, Any], lock: dict[str, Any] | None, now: datetime) -> bool:
    if run.get("status") not in ACTIVE_STATUSES:
        return False
    if lock and lock["run_id"] == run["run_id"] and lock["lease_expires_at"] < now.timestamp():
        return True
    age = _age_seconds(run.get("updated_at"), now)
    return age is not None and age > STALE_AFTER_SECONDS


def _describe(client: Any, state_machine_arn: str, run_id: str) -> tuple[str, str | None]:
    """("status", value), ("missing", None) or ("error", exception name); never raises AWS errors."""
    try:
        described = client.describe_execution(executionArn=execution_arn(state_machine_arn, run_id))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ExecutionDoesNotExist":
            return "missing", None
        return "error", exc.response.get("Error", {}).get("Code") or type(exc).__name__
    except BotoCoreError as exc:
        return "error", type(exc).__name__
    return "status", described.get("status")


def _reconcile_run(
    store: RunHistory,
    client: Any,
    state_machine_arn: str,
    run: dict[str, Any],
    lock: dict[str, Any] | None,
    now: datetime,
) -> tuple[str, str | None, str | None]:
    """Returns (action, new status or None, execution status or None)."""
    run_id, status = run["run_id"], run["status"]
    holds_lock = bool(lock and lock["run_id"] == run_id)
    active = status in ACTIVE_STATUSES

    kind, value = _describe(client, state_machine_arn, run_id)
    if kind == "error":
        return "error", None, value

    if kind == "missing":
        if active:
            age = _age_seconds(run.get("created_at"), now)
            if age is None or age < MISSING_EXECUTION_GRACE_SECONDS:
                return "unchanged", None, None
            applied = store.update_run(
                run_id, status="interrupted", error=INTERRUPTED_MESSAGE, error_code="INTERRUPTED"
            )
            return ("interrupted" if applied else "unchanged"), "interrupted", None
        # The run is already finished or interrupted and no execution exists, so nothing can still be using its lock.
        if holds_lock:
            return ("released" if store.release_lock(run_id) else "unchanged"), None, None
        return "unchanged", None, None

    execution_status = value
    if execution_status in _NOT_FINISHED:
        if active and holds_lock and lock["lease_expires_at"] - now.timestamp() < RENEW_WHEN_LEASE_BELOW_SECONDS:
            return ("renewed" if store.renew_lock(run_id) else "unchanged"), None, execution_status
        return "unchanged", None, execution_status  # still running: leave it alone

    if execution_status == "SUCCEEDED":
        target, fields = "succeeded", {"stage": "completed"}
    elif execution_status in _FAILED:
        # Gold already exists once its key is recorded, matching the workflow's own partial_failure rule.
        target = "partial_failure" if run.get("gold_key") else "failed"
        fields = {"error": f"Execution {execution_status}", "error_code": "INTERNAL_ERROR"}
    elif execution_status == "ABORTED":
        target, fields = "interrupted", {"error": INTERRUPTED_MESSAGE, "error_code": "INTERRUPTED"}
    else:
        return "unchanged", None, execution_status  # a status this code does not know: change nothing

    already_settled = status == target == "interrupted"
    if (active or status == "interrupted") and not already_settled:
        applied = store.update_run(run_id, status=target, **fields)  # a terminal update also releases the lock
        action = "unchanged" if not applied else ("interrupted" if target == "interrupted" else "settled")
        return action, target, execution_status
    # The record is already final, so a lock this run still holds is left over from a failed release.
    if holds_lock:
        return ("released" if store.release_lock(run_id) else "unchanged"), None, execution_status
    return "unchanged", None, execution_status


def _emit(sink: EventSink | None, run: dict[str, Any], action: str, new: str | None, execution: str | None, stale: bool,
          error: str | None = None) -> None:
    if sink is None:
        return
    event = {"run_id": run["run_id"], "action": action, "from_status": run["status"], "to_status": new,
             "execution_status": execution, "stale": stale}
    if error:
        event["error_type"] = error  # a class or AWS error code, never a message
    sink(event)


def reconcile(
    store: RunHistory,
    client: Any,
    state_machine_arn: str,
    *,
    now: datetime | None = None,
    on_event: EventSink | None = None,
) -> dict[str, int]:
    """One reconciliation pass over recent runs and the lock holder; returns counts (see COUNT_KEYS)."""
    if not state_machine_arn:
        raise ValueError("state_machine_arn is required")
    now = now or datetime.now(UTC)
    counts = dict.fromkeys(COUNT_KEYS, 0)

    lock = store.get_lock()
    runs = store.list_runs(RECONCILE_LIST_LIMIT)
    holder = lock["run_id"] if lock else None

    candidates = []
    for run in runs:
        recheck = run["status"] == "interrupted" and (_age_seconds(run.get("updated_at"), now) or 0) < INTERRUPTED_RECHECK_SECONDS
        if run["status"] in ACTIVE_STATUSES or recheck or run["run_id"] == holder:
            candidates.append(run)
    if holder and holder not in {r["run_id"] for r in candidates}:
        holder_run = store.get_run(holder)  # an old run whose lock was never released
        if holder_run:
            candidates.append(holder_run)

    for run in candidates:
        stale = _is_stale(run, lock, now)
        counts["checked"] += 1
        counts["stale"] += int(stale)
        try:
            action, new_status, execution_status = _reconcile_run(store, client, state_machine_arn, run, lock, now)
            error = execution_status if action == "error" else None
        except RunStoreError as exc:
            action, new_status, execution_status, error = "error", None, None, type(exc).__name__
        counts["errors" if action == "error" else action] += 1
        if action != "unchanged" or stale:
            _emit(on_event, run, action, new_status, None if action == "error" else execution_status, stale, error)
    return counts


def reconcile_run(
    store: RunHistory, client: Any, state_machine_arn: str, run_id: str, *, now: datetime | None = None
) -> str:
    """Reconcile one run (used before a new run may take over an expired lock); returns the action."""
    now = now or datetime.now(UTC)
    run = store.get_run(run_id)
    if run is None:
        return "unchanged"
    action, _, _ = _reconcile_run(store, client, state_machine_arn, run, store.get_lock(), now)
    return action
