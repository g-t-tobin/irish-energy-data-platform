"""Step Functions execution and reconciliation. Designed and tested, not wired in.

Nothing in the API constructs `StepFunctionsExecutor`; settings still refuse to
enable execution. Enabling it is a separate, reviewed change (plan Phase 8).
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src.executor import ExecutionStartError
from src.run_store import (
    ACTIVE_STATUSES,
    INTERRUPTED_MESSAGE,
    RunHistory,
    classify_error,
    safe_error_message,
)

logger = logging.getLogger(__name__)

_ARN_RE = re.compile(r"^arn:aws:states:[a-z]{2}(?:-[a-z]+)+-\d:\d{12}:stateMachine:[A-Za-z0-9_-]{1,80}$")
MISSING_EXECUTION_GRACE_SECONDS = 300
_TERMINAL_EXECUTION_STATUSES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}


class StepFunctionsExecutor:
    """Records a run, then starts one execution named after the run ID.

    The state machine receives only the run ID and `days_back`. Reusing the
    run ID as the execution name makes a repeated start a no-op.
    """

    def __init__(self, store: RunHistory, client: Any, state_machine_arn: str) -> None:
        if not _ARN_RE.match(state_machine_arn):
            raise ValueError("state_machine_arn must be a Step Functions state machine ARN")
        self._store = store
        self._client = client
        self._arn = state_machine_arn

    def start(
        self,
        days_back: int,
        *,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
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
        except Exception as exc:
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
    return state_machine_arn.replace(":stateMachine:", ":execution:") + f":{run_id}"


def reconcile_active_runs(
    store: RunHistory,
    client: Any,
    state_machine_arn: str,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Settle runs still marked active using the real execution outcome.

    Needs `states:DescribeExecution` on this state machine's executions only.
    Running executions are left alone; a run whose execution never started is
    marked interrupted after a short grace period.
    """
    now = now or datetime.now(timezone.utc)
    counts = {"settled": 0, "interrupted": 0, "unchanged": 0}
    for run in store.list_runs(50):
        if run.get("status") not in ACTIVE_STATUSES:
            continue
        run_id = run["run_id"]
        try:
            described = client.describe_execution(
                executionArn=_execution_arn(state_machine_arn, run_id)
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ExecutionDoesNotExist":
                counts["unchanged"] += 1
                continue
            created = datetime.fromisoformat(run["created_at"])
            if (now - created).total_seconds() >= MISSING_EXECUTION_GRACE_SECONDS:
                store.update_run(
                    run_id, status="interrupted", error=INTERRUPTED_MESSAGE, error_code="INTERRUPTED"
                )
                counts["interrupted"] += 1
            else:
                counts["unchanged"] += 1
            continue
        except BotoCoreError:
            counts["unchanged"] += 1
            continue

        status = described.get("status")
        if status == "SUCCEEDED":
            store.update_run(run_id, status="succeeded", stage="completed")
            counts["settled"] += 1
        elif status in {"FAILED", "TIMED_OUT"}:
            store.update_run(
                run_id,
                status="failed",
                error=f"Execution {status}",
                error_code="INTERNAL_ERROR",
            )
            counts["settled"] += 1
        elif status == "ABORTED":
            store.update_run(
                run_id, status="interrupted", error=INTERRUPTED_MESSAGE, error_code="INTERRUPTED"
            )
            counts["interrupted"] += 1
        else:
            counts["unchanged"] += 1
    return counts
