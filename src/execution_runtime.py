"""Conditional runtime setup for pipeline execution."""

import logging

from src.executor import DisabledExecutor, PipelineExecutor
from src.run_store import RunHistory
from src.settings import Settings

logger = logging.getLogger(__name__)


def configure_executor(settings: Settings, store: RunHistory) -> PipelineExecutor:
    """Build the executor only when explicitly enabled and reconcile existing runs."""
    if not settings.pipeline_execution_enabled:
        return DisabledExecutor()

    arn = settings.pipeline_state_machine_arn
    if not arn:
        raise RuntimeError(
            "Pipeline execution is enabled but the state machine ARN is missing."
        )

    # Import AWS and Step Functions dependencies only for enabled execution.
    import boto3

    from src.executor_stepfunctions import StepFunctionsExecutor, reconcile_active_runs

    region = arn.split(":")[3]
    client = boto3.client("stepfunctions", region_name=region)
    executor = StepFunctionsExecutor(store, client, arn)

    counts = reconcile_active_runs(store, client, arn)
    logger.info("Step Functions startup reconciliation: %s", counts)
    return executor
