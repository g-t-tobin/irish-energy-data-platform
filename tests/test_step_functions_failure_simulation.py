"""The synthesised workflow run against real handlers and test doubles, failure by failure.

Limits of the simulation (see tests/asl_simulator.py): no wall-clock time or TimeoutSeconds, retry
delays are recorded and not slept, and only the state types this definition uses are modelled.
"""

import json

import pytest

from src import run_history_schema as schema
from tests.pipeline_harness import make_pipeline_fixture

pipeline = make_pipeline_fixture()

STAGES = ["bronze", "silver", "gold", "glue_registration"]
TASK = {"bronze": "Bronze", "silver": "Silver", "gold": "Gold", "glue_registration": "GlueRegistration"}
FAILED_STATUS = {"bronze": "failed", "silver": "failed", "gold": "failed", "glue_registration": "partial_failure"}
MARK = {"failed": "MarkFailure", "partial_failure": "MarkPartialFailure"}


def assert_failed_cleanly(pipeline, result, execution, run_id, status, stage):
    assert result["status"] == "FAILED" and result["error"] == "PipelineFailed"
    assert "Succeeded" not in execution.visited and execution.visited[-1] == "Failed"
    assert MARK[status] in execution.visited and "ReleaseLockOnFailure" in execution.visited
    assert result["output"]["run_id"] == run_id and result["output"]["days_back"] == 2  # context survives
    run = pipeline.store.get_run(run_id)
    assert (run["status"], run["stage"]) == (status, stage)
    assert run["error"] == "A pipeline stage failed." and run["finished_at"]
    assert pipeline.lock() is None and "delete_item" in pipeline.ddb.calls


@pytest.mark.parametrize("stage", STAGES)
def test_a_stage_failure_is_recorded_releases_the_lock_and_fails_the_execution(pipeline, stage):
    run_id = pipeline.new_run()
    pipeline.fail(stage)

    result, execution = pipeline.execute(run_id)

    assert_failed_cleanly(pipeline, result, execution, run_id, FAILED_STATUS[stage], stage)
    assert result["output"]["error"]["Error"] == "RuntimeError"  # the cause stays in the execution history


@pytest.mark.parametrize(
    ("progress", "status", "stage"),
    [("bronze_key", "failed", "bronze"), ("silver_key", "failed", "silver"), ("gold_key", "partial_failure", "gold")],
)
def test_failing_to_record_progress_fails_the_run_and_never_reports_success(pipeline, progress, status, stage):
    run_id = pipeline.new_run()
    pipeline.fail_when_recording(progress)

    result, execution = pipeline.execute(run_id)

    assert_failed_cleanly(pipeline, result, execution, run_id, status, stage)
    assert result["output"]["error"]["Error"].startswith("DynamoDb.")
    assert pipeline.store.get_run(run_id).get(progress) is None  # the update really did not apply


def test_a_gold_object_whose_key_could_not_be_recorded_is_a_partial_failure_without_a_gold_key(pipeline):
    """Known limitation: the object exists, but the run record does not name it."""
    run_id = pipeline.new_run()
    pipeline.fail_when_recording("gold_key")

    pipeline.execute(run_id)

    assert len(pipeline.s3.object_keys("gold/")) == 1 and "gold_key" not in pipeline.store.get_run(run_id)


def test_a_failed_lock_release_after_success_keeps_the_success_and_the_lock(pipeline):
    run_id = pipeline.new_run()
    pipeline.fail_lock_release()

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "SUCCEEDED" and execution.visited[-2:] == ["ReleaseLockOnSuccess", "Succeeded"]
    assert pipeline.store.get_run(run_id)["status"] == "succeeded"
    assert pipeline.lock()["run_id"]["S"] == run_id  # held until the lease expires


def test_a_failed_lock_release_after_failure_still_fails_the_execution(pipeline):
    run_id = pipeline.new_run()
    pipeline.fail("gold")
    pipeline.fail_lock_release()

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "FAILED" and execution.visited[-2:] == ["ReleaseLockOnFailure", "Failed"]
    assert pipeline.store.get_run(run_id)["status"] == "failed" and pipeline.lock() is not None


@pytest.mark.parametrize("stage", STAGES)
def test_one_transient_lambda_error_is_retried_and_the_run_succeeds(pipeline, stage):
    run_id = pipeline.new_run()
    pipeline.service_error(stage, times=1, name="Lambda.TooManyRequestsException")

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "SUCCEEDED" and execution.invocations[TASK[stage]] == 2
    assert execution.waits[TASK[stage]] == [5.0]
    assert pipeline.store.get_run(run_id)["status"] == "succeeded"


@pytest.mark.parametrize("stage", STAGES)
def test_exhausted_lambda_retries_fail_the_run_after_exactly_three_attempts(pipeline, stage):
    run_id = pipeline.new_run()
    pipeline.service_error(stage, times=9, name="Lambda.ServiceException")

    result, execution = pipeline.execute(run_id)

    assert execution.invocations[TASK[stage]] == 3 and execution.waits[TASK[stage]] == [5.0, 10.0]
    assert_failed_cleanly(pipeline, result, execution, run_id, FAILED_STATUS[stage], stage)
    assert result["output"]["error"]["Error"] == "Lambda.ServiceException"


@pytest.mark.parametrize("error", ["Lambda.Unknown", "Sandbox.Timedout", "States.Timeout", "States.TaskFailed"])
def test_timeouts_and_unknown_function_failures_are_not_retried(pipeline, error):
    run_id = pipeline.new_run()
    pipeline.service_error("silver", times=1, name=error)

    result, execution = pipeline.execute(run_id)

    assert execution.invocations["Silver"] == 1
    assert_failed_cleanly(pipeline, result, execution, run_id, "failed", "silver")


@pytest.mark.parametrize("days_back", [0, 31, "2", None, True])
def test_invalid_days_back_is_a_permanent_failure_without_a_retry_or_any_write(pipeline, days_back):
    run_id = pipeline.new_run()

    result, execution = pipeline.execute(run_id, execution_input={"run_id": run_id, "days_back": days_back})

    assert execution.invocations == {"Bronze": 1} and pipeline.s3.object_keys() == [] and pipeline.eirgrid.requests == []
    assert result["output"]["error"]["Error"] == "StageInputError"
    assert pipeline.store.get_run(run_id)["status"] == "failed"


def test_a_missing_run_id_stops_the_execution_before_any_stage_or_write(pipeline):
    result, execution = pipeline.execute("", execution_input={"days_back": 2})

    assert result["status"] == "FAILED" and result["error"] == "States.Runtime"  # States.ALL cannot catch it
    assert execution.visited == ["MarkRunning"] and execution.invocations == {}
    assert pipeline.s3.object_keys() == [] and pipeline.eirgrid.requests == []


@pytest.mark.parametrize(
    ("stage", "broken"),
    [
        ("bronze", lambda out: {"records": out["records"]}),  # no key
        ("bronze", lambda out: None),  # no output at all
        ("silver", lambda out: {}),
        ("gold", lambda out: {"key": None}),
    ],
)
def test_a_stage_that_breaks_its_output_contract_fails_the_execution_and_reconciliation_settles_the_run(
    pipeline, stage, broken
):
    run_id = pipeline.new_run()
    pipeline.override_output(stage, broken)

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "FAILED" and "Succeeded" not in execution.visited
    # An unresolvable path (States.Runtime) cannot be caught, so the run stays active for the API
    # to reconcile; a null key resolves and is rejected by the next stage's own validation.
    assert result["error"] in {"States.Runtime", "PipelineFailed"}
    assert pipeline.store.get_run(run_id)["status"] in {"running", "failed", "partial_failure"}


def test_an_uncatchable_workflow_error_leaves_an_active_run_that_reconciliation_settles(pipeline):
    from src.executor_stepfunctions import reconcile_active_runs
    from tests.pipeline_harness import LocalStepFunctions

    run_id = pipeline.new_run()
    pipeline.override_output("bronze", lambda out: {"records": 1})
    client = LocalStepFunctions(pipeline)
    client.start_execution(
        stateMachineArn="arn:aws:states:eu-west-1:851725545966:stateMachine:irish-energy-pipeline",
        name=run_id, input=json.dumps({"run_id": run_id, "days_back": 2}),
    )

    assert pipeline.store.get_run(run_id)["status"] == "running" and pipeline.lock() is not None
    counts = reconcile_active_runs(
        pipeline.store, client, "arn:aws:states:eu-west-1:851725545966:stateMachine:irish-energy-pipeline"
    )

    assert counts["settled"] == 1
    assert pipeline.store.get_run(run_id)["status"] == "failed" and pipeline.lock() is None


def test_progress_updates_never_touch_identity_listing_or_lease_fields(pipeline):
    run_id = pipeline.new_run(requested_by="operator-1", idempotency_key="idem-key-0001")
    before = pipeline.raw_items()
    pipeline.fail_lock_release()  # keep the lock item so its lease can be compared

    pipeline.execute(run_id)

    after = pipeline.raw_items()
    run_key = (f"{schema.RUN_PK_PREFIX}{run_id}", schema.RUN_SK)
    for field in ("pk", "sk", "run_id", "days_back", "requested_by", "created_at", "gsi1pk", "gsi1sk"):
        assert after[run_key][field] == before[run_key][field], field
    assert set(after[run_key]) - set(before[run_key]) <= {
        "started_at", "finished_at", "bronze_key", "silver_key", "gold_key"}
    assert [v for k, v in after.items() if k[0].startswith(schema.IDEMPOTENCY_PK_PREFIX)] == [
        v for k, v in before.items() if k[0].startswith(schema.IDEMPOTENCY_PK_PREFIX)
    ]  # idempotency record (run_id, expires_at) untouched
    lock_key = (schema.LOCK_PK, schema.LOCK_SK)
    assert after[lock_key] == before[lock_key]  # the workflow never renews or rewrites the lease
    assert set(after) == set(before)  # no items created or deleted besides what the run itself owns
