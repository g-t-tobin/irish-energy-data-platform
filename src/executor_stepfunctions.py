"""Step Functions execution and reconciliation. Designed and tested, not wired in.

Nothing in the API constructs `StepFunctionsExecutor`; settings still refuse to
enable execution. Enabling it is a separate, reviewed change (plan Phase 8).
"""

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from src.executor import ExecutionStartError
from src.reconciliation import (  # noqa: F401  (re-exported for existing callers)
    MISSING_EXECUTION_GRACE_SECONDS,
    execution_arn,
    reconcile,
    reconcile_run,
)
from src.run_store import (
    ActiveRunError,
    RunHistory,
    classify_error,
    safe_error_message,
    validate_create_args,
)

logger = logging.getLogger(__name__)

_ARN_RE = re.compile(r"^arn:aws:states:[a-z]{2}(?:-[a-z]+)+-\d:\d{12}:stateMachine:[A-Za-z0-9_-]{1,80}$")


class StepFunctionsExecutor:
    """Records a run, then starts one execution named after the run ID.

    The state machine receives only the run ID and `days_back`. Reusing the
    run ID as the execution name makes a repeated start a no-op.
    """

    def __init__(
        self,
        store: RunHistory,
        client: Any,
        state_machine_arn: str,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not _ARN_RE.match(state_machine_arn):
            raise ValueError("state_machine_arn must be a Step Functions state machine ARN")
        self._store = store
        self._client = client
        self._arn = state_machine_arn
        self._clock = clock or getattr(store, "_clock", time.time)

    def _refuse_if_expired_lock_is_still_in_use(self) -> None:
        """An expired lease alone must not let a new run start behind a live execution.

        Takes over only when the lock's run is provably over; a running execution keeps its lock
        (the lease is renewed); anything unknown (AWS error, missing execution inside the grace
        period) refuses.
        """
        lock = self._store.get_lock()
        if lock is None or lock["lease_expires_at"] >= self._clock():
            return
        now = datetime.fromtimestamp(self._clock(), UTC)
        action = reconcile_run(self._store, self._client, self._arn, lock["run_id"], now=now)
        if action in ("settled", "interrupted", "released"):
            return
        lock = self._store.get_lock()  # "renewed" leaves a live lock, which create_run then enforces itself
        if lock is not None and lock["lease_expires_at"] < self._clock():
            raise ActiveRunError("A pipeline run is already active.")

    def start(
        self,
        days_back: int,
        *,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        validate_create_args(days_back, idempotency_key)
        self._refuse_if_expired_lock_is_still_in_use()
        run = self._store.create_run(
            days_back, requested_by=requested_by, idempotency_key=idempotency_key
        )
        if run.get("duplicate"):
            return {"run_id": run["run_id"], "status": run.get("status", "queued")}

        run_id = run["run_id"]
        try:
            self._client.start_execution(
                stateMachineArn=self._arn,
                name=run_id,
                input=json.dumps({"run_id": run_id, "days_back": days_back}),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                self._fail_start(run_id, exc)
        except Exception as exc:  # noqa: BLE001 - mark run failed on unexpected startup errors
            self._fail_start(run_id, exc)
        return {"run_id": run_id, "status": "queued"}

    def _fail_start(self, run_id: str, exc: BaseException) -> None:
        # Mark the run failed so the lock is released instead of waiting for the lease.
        logger.error("Could not start execution for run %s: %s", run_id, safe_error_message(exc))
        self._store.update_run(
            run_id,
            status="failed",
            error=safe_error_message(exc),
            error_code=classify_error(exc),
        )
        raise ExecutionStartError("The pipeline execution could not be started.") from exc


def _execution_arn(state_machine_arn: str, run_id: str) -> str:
    return execution_arn(state_machine_arn, run_id)


def reconcile_active_runs(
    store: RunHistory,
    client: Any,
    state_machine_arn: str,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """One reconciliation pass with the original three counts; `reconcile` returns the full set.

    Needs states:DescribeExecution only. Query errors, renewed leases and no-ops all count as unchanged.
    """
    full = reconcile(store, client, state_machine_arn, now=now)
    return {
        "settled": full["settled"] + full["released"],
        "interrupted": full["interrupted"],
        "unchanged": full["unchanged"] + full["errors"] + full["renewed"],
    }
