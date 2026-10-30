"""Lock, lease, idempotency and reconciliation, through the store, the executor and the workflow."""

import threading
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

from src import run_history_schema as schema
from src.dynamodb_run_store import DynamoRunStore
from src.executor import ExecutionStartError
from src.executor_stepfunctions import (
    MISSING_EXECUTION_GRACE_SECONDS,
    reconcile_active_runs,
)
from src.pipeline import register_existing_gold_partition
from src.run_store import ActiveRunError
from tests.pipeline_harness import (
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    LocalStepFunctions,
    make_pipeline_fixture,
)

pipeline = make_pipeline_fixture()
LEASE = schema.LOCK_LEASE_SECONDS


def lease_of(pipeline) -> int:
    return int(pipeline.lock()["lease_expires_at"]["N"])


def reconcile(pipeline, client, *, after=0):
    when = datetime.fromtimestamp(pipeline.clock.now + after, UTC)
    return reconcile_active_runs(pipeline.store, client, STATE_MACHINE_ARN, now=when)


# -- store: creation, idempotency, lock, lease ---------------------------------------------


def test_creating_a_run_records_it_and_takes_the_lock_with_a_45_minute_lease(pipeline):
    run_id = pipeline.new_run(3, requested_by="operator-1", idempotency_key="idem-key-0001")

    run = pipeline.store.get_run(run_id)
    assert (run["status"], run["stage"], run["days_back"], run["requested_by"]) == ("queued", "queued", 3, "operator-1")
    assert pipeline.lock()["run_id"]["S"] == run_id
    assert lease_of(pipeline) == pytest.approx(pipeline.clock.now + LEASE, abs=2) and LEASE == 45 * 60


def test_the_lease_is_longer_than_the_workflow_limit_so_a_healthy_run_keeps_its_lock():
    assert schema.STATE_MACHINE_TIMEOUT_SECONDS == 30 * 60 < schema.LOCK_LEASE_SECONDS == 45 * 60


def test_a_duplicate_idempotency_key_returns_the_same_run_and_changes_nothing(pipeline):
    first = pipeline.store.create_run(2, requested_by="op", idempotency_key="idem-key-0001")
    before = pipeline.raw_items()

    again = pipeline.store.create_run(2, requested_by="op", idempotency_key="idem-key-0001")

    assert again["run_id"] == first["run_id"] and again["duplicate"] is True
    assert pipeline.raw_items() == before and len(pipeline.store.list_runs()) == 1


def test_the_same_user_cannot_start_a_second_run_while_one_is_active(pipeline):
    pipeline.new_run(requested_by="op", idempotency_key="idem-key-0001")

    with pytest.raises(ActiveRunError):
        pipeline.store.create_run(2, requested_by="op", idempotency_key="idem-key-0002")
    with pytest.raises(ActiveRunError):
        pipeline.store.create_run(2, requested_by="someone-else", idempotency_key="idem-key-0001")
    assert len(pipeline.store.list_runs()) == 1


def test_concurrent_requests_acquire_the_lock_exactly_once(pipeline):
    outcomes, barrier = [], threading.Barrier(12)

    def attempt(index):
        barrier.wait()
        try:
            outcomes.append(pipeline.store.create_run(2, requested_by=f"user-{index}")["run_id"])
        except ActiveRunError:
            outcomes.append("refused")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(o == "refused" for o in outcomes) == [False] + [True] * 11
    assert len(pipeline.store.list_runs()) == 1


def test_progress_renews_the_lease_and_only_the_owner_releases_it(pipeline):
    run_id = pipeline.new_run()
    first = lease_of(pipeline)
    pipeline.clock.advance(600)

    pipeline.store.update_run(run_id, status="running", stage="bronze")

    assert lease_of(pipeline) == pytest.approx(first + 600, abs=2)
    pipeline.store.update_run(run_id, status="succeeded", stage="completed")
    assert pipeline.lock() is None


@pytest.mark.parametrize("status", ["succeeded", "failed", "partial_failure"])
def test_every_terminal_status_releases_the_lock_and_allows_a_new_run(pipeline, status):
    run_id = pipeline.new_run()

    assert pipeline.store.update_run(run_id, status=status, stage="completed") is True

    assert pipeline.lock() is None and pipeline.new_run()


def test_an_expired_lease_lets_a_new_run_take_over_and_recovery_spares_the_new_run(pipeline):
    old = pipeline.new_run()
    pipeline.clock.advance(LEASE + 1)
    new = pipeline.new_run()  # takes the expired lock

    assert pipeline.store.recover_interrupted() == 1
    assert pipeline.store.get_run(old)["status"] == "interrupted"
    assert pipeline.store.get_run(new)["status"] == "queued" and pipeline.lock()["run_id"]["S"] == new


def test_recovery_after_an_expired_lease_interrupts_the_run_and_frees_the_lock(pipeline):
    run_id = pipeline.new_run()
    pipeline.clock.advance(LEASE + 1)

    assert pipeline.store.recover_interrupted() == 1

    assert pipeline.store.get_run(run_id)["status"] == "interrupted" and pipeline.lock() is None


# -- executor and workflow: start, reconcile, restart ------------------------------------------


def test_a_failed_start_marks_the_run_failed_frees_the_lock_and_raises_a_generic_error(pipeline):
    client = LocalStepFunctions(pipeline)
    client.start_error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StartExecution")

    with pytest.raises(ExecutionStartError, match="could not be started"):
        client.executor().start(2)

    (run,) = pipeline.store.list_runs()
    assert (run["status"], run["error_code"]) == ("failed", "ACCESS_DENIED") and pipeline.lock() is None


@pytest.mark.parametrize(
    ("execution_status", "expected"),
    [
        ("RUNNING", ("running", "bronze")),
        ("SUCCEEDED", ("succeeded", "completed")),
        ("FAILED", ("failed", None)),
        ("TIMED_OUT", ("failed", None)),
        ("ABORTED", ("interrupted", None)),
    ],
)
def test_reconciliation_settles_active_runs_from_the_real_execution_status(pipeline, execution_status, expected):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    pipeline.store.update_run(run_id, status="running", stage="bronze")
    client.finish(run_id, execution_status)

    reconcile(pipeline, client)

    run = pipeline.store.get_run(run_id)
    assert run["status"] == expected[0] and (expected[1] is None or run["stage"] == expected[1])
    assert (pipeline.lock() is not None) == (execution_status == "RUNNING")  # only a live run keeps the lock


def test_a_missing_execution_is_given_a_grace_period_then_interrupted(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = pipeline.new_run()  # the API recorded the run but never reached StartExecution

    assert reconcile(pipeline, client, after=MISSING_EXECUTION_GRACE_SECONDS - 60)["unchanged"] == 1
    assert pipeline.store.get_run(run_id)["status"] == "queued"
    assert reconcile(pipeline, client, after=MISSING_EXECUTION_GRACE_SECONDS + 60)["interrupted"] == 1
    assert pipeline.store.get_run(run_id)["status"] == "interrupted" and pipeline.lock() is None


@pytest.mark.parametrize("failure", [ClientError({"Error": {"Code": "ThrottlingException"}}, "DescribeExecution"),
                                     ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeExecution")])
def test_when_describe_fails_nothing_changes_and_the_run_stays_active(pipeline, failure):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    client.describe_error = failure

    assert reconcile(pipeline, client)["unchanged"] == 1

    assert pipeline.store.get_run(run_id)["status"] == "queued" and pipeline.lock() is not None


# -- scenarios -------------------------------------------------------------------------------


def test_two_api_requests_racing_for_the_lock_start_one_execution(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    results, barrier = [], threading.Barrier(2)

    def request(index):
        barrier.wait()
        try:
            results.append(client.executor().start(2, requested_by=f"user-{index}")["run_id"])
        except ActiveRunError:
            results.append("refused")

    threads = [threading.Thread(target=request, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(r == "refused" for r in results) == [False, True] and len(client.started) == 1


def test_a_duplicate_request_during_an_active_run_starts_nothing_new(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    first = client.executor().start(2, requested_by="op", idempotency_key="idem-key-0001")

    repeat = client.executor().start(2, requested_by="op", idempotency_key="idem-key-0001")
    with pytest.raises(ActiveRunError):
        client.executor().start(2, requested_by="op", idempotency_key="idem-key-0002")

    assert repeat["run_id"] == first["run_id"] and len(client.started) == 1


def test_an_api_process_dying_after_start_execution_does_not_disturb_the_running_workflow(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2, requested_by="op")["run_id"]
    restarted = DynamoRunStore(RUN_HISTORY_TABLE_NAME, pipeline.ddb, clock=pipeline.clock)  # a new API process

    assert restarted.recover_interrupted() == 0  # the lease is live, so the run is left alone
    client.finish(run_id)

    assert restarted.get_run(run_id)["status"] == "succeeded" and restarted.get_run(run_id)["stage"] == "completed"
    assert pipeline.lock() is None


def test_a_restart_after_the_lease_expired_cannot_overwrite_the_real_outcome(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    pipeline.clock.advance(LEASE + 1)
    restarted = DynamoRunStore(RUN_HISTORY_TABLE_NAME, pipeline.ddb, clock=pipeline.clock)

    assert restarted.recover_interrupted() == 1  # a guess: the outcome is unknown
    assert restarted.get_run(run_id)["status"] == "interrupted"
    client.executions[run_id]["status"] = "SUCCEEDED"
    reconcile_active_runs(restarted, client, STATE_MACHINE_ARN)  # interrupted is not active: left alone

    assert restarted.get_run(run_id)["status"] == "interrupted"  # known limitation, see the handover document


def test_an_execution_that_failed_before_it_could_record_anything_is_settled_by_reconciliation(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    restore = pipeline.fail_ddb("update_item", lambda kw: kw["Key"]["pk"]["S"].startswith(schema.RUN_PK_PREFIX))
    client.finish(run_id)
    restore()  # DynamoDB is healthy again by the time the API reconciles

    assert client.executions[run_id]["status"] == "FAILED"
    assert pipeline.store.get_run(run_id)["status"] == "queued" and pipeline.lock() is not None  # nothing recorded

    reconcile(pipeline, client)

    assert pipeline.store.get_run(run_id)["status"] == "failed" and pipeline.lock() is None


def test_a_lock_that_expired_before_reconciliation_is_not_released_from_under_the_new_run(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    old = client.executor().start(2)["run_id"]
    client.executions[old]["status"] = "FAILED"
    pipeline.clock.advance(LEASE + 1)
    new = pipeline.new_run()  # takes over the expired lock

    reconcile(pipeline, client)

    assert pipeline.store.get_run(old)["status"] == "failed"
    assert pipeline.store.get_run(new)["status"] == "queued" and pipeline.lock()["run_id"]["S"] == new


def test_a_successful_workflow_whose_lock_release_failed_blocks_new_runs_only_until_the_lease_expires(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    pipeline.fail_lock_release()
    client.finish(run_id)

    assert pipeline.store.get_run(run_id)["status"] == "succeeded" and client.executions[run_id]["status"] == "SUCCEEDED"
    with pytest.raises(ActiveRunError):  # known limitation: the finished run still holds the lock
        pipeline.store.create_run(2)
    assert pipeline.store.recover_interrupted() == 0 and pipeline.lock() is not None

    pipeline.clock.advance(LEASE + 1)
    assert pipeline.new_run()  # the lease is the backstop


def test_a_glue_registration_failure_is_a_partial_failure_that_frees_the_lock_and_can_be_repaired(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    pipeline.fail("glue_registration")
    client.finish(run_id)

    run = pipeline.store.get_run(run_id)
    assert (run["status"], run["stage"]) == ("partial_failure", "glue_registration") and run["gold_key"]
    assert pipeline.lock() is None and pipeline.new_run()  # the next run may start
    assert register_existing_gold_partition(run["gold_key"]) is True
