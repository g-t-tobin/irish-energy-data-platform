"""Reconciliation and lock recovery against the in-memory DynamoDB and Step Functions doubles."""

import json
import threading
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src import run_history_schema as schema
from src.dynamodb_run_store import DynamoRunStore
from src.reconciliation import (
    COUNT_KEYS,
    INTERRUPTED_RECHECK_SECONDS,
    MISSING_EXECUTION_GRACE_SECONDS,
    RENEW_WHEN_LEASE_BELOW_SECONDS,
    STALE_AFTER_SECONDS,
    reconcile,
)
from src.run_store import PUBLIC_RUN_FIELDS, ActiveRunError, to_public_run
from tests.pipeline_harness import (
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    LocalStepFunctions,
    make_pipeline_fixture,
)

pipeline = make_pipeline_fixture()
LEASE = schema.LOCK_LEASE_SECONDS
IMMUTABLE = ("pk", "sk", "run_id", "days_back", "requested_by", "created_at", "gsi1pk", "gsi1sk")


def when(pipeline, after=0):
    return datetime.fromtimestamp(pipeline.clock.now + after, UTC)


def recon(pipeline, client, after=0, sink=None, store=None):
    return reconcile(store or pipeline.store, client, STATE_MACHINE_ARN, now=when(pipeline, after), on_event=sink)


def started(pipeline, **kwargs):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2, requested_by="operator-1", **kwargs)["run_id"]
    return client, run_id


def lock_owner(pipeline):
    lock = pipeline.lock()
    return lock["run_id"]["S"] if lock else None


def lease(pipeline):
    return int(pipeline.lock()["lease_expires_at"]["N"])


def run_item(pipeline, run_id):
    return pipeline.raw_items()[(f"{schema.RUN_PK_PREFIX}{run_id}", schema.RUN_SK)]


# -- every execution status ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("execution", "status", "lock_kept"),
    [
        ("RUNNING", "running", True),
        ("PENDING_REDRIVE", "running", True),
        ("SUCCEEDED", "succeeded", False),
        ("FAILED", "failed", False),
        ("TIMED_OUT", "failed", False),
        ("ABORTED", "interrupted", False),
        ("SOMETHING_NEW", "running", True),  # a status this code does not know changes nothing
    ],
)
def test_each_execution_status_settles_the_run_and_the_lock_correctly(pipeline, execution, status, lock_kept):
    client, run_id = started(pipeline)
    pipeline.store.update_run(run_id, status="running", stage="silver")
    client.executions[run_id]["status"] = execution

    recon(pipeline, client)

    run = pipeline.store.get_run(run_id)
    assert run["status"] == status
    assert (lock_owner(pipeline) == run_id) is lock_kept
    if execution == "SUCCEEDED":
        assert run["stage"] == "completed" and "error" not in run
    if execution in ("FAILED", "TIMED_OUT"):
        assert run["error_code"] == "INTERNAL_ERROR" and run["error"] == f"Execution {execution}"
    if execution == "ABORTED":
        assert run["error_code"] == "INTERRUPTED"


def test_a_failed_execution_after_gold_was_recorded_is_a_partial_failure(pipeline):
    client, run_id = started(pipeline)
    pipeline.store.update_run(run_id, status="running", stage="glue_registration", gold_key="gold/x/daily_demand.parquet")
    client.executions[run_id]["status"] = "FAILED"

    recon(pipeline, client)

    assert pipeline.store.get_run(run_id)["status"] == "partial_failure" and pipeline.lock() is None


# -- repeatability, restarts, concurrency -----------------------------------------------------------


def test_repeating_a_pass_changes_nothing_after_the_first(pipeline):
    client, run_id = started(pipeline)
    client.executions[run_id]["status"] = "FAILED"

    first = recon(pipeline, client)
    after_first = pipeline.raw_items()
    second = recon(pipeline, client)
    third = recon(pipeline, client)

    assert first["settled"] == 1 and second["checked"] == third["checked"] == 0
    assert pipeline.raw_items() == after_first


def test_a_fresh_process_after_an_api_restart_settles_the_same_runs(pipeline):
    client, run_id = started(pipeline)
    client.executions[run_id]["status"] = "SUCCEEDED"
    restarted = DynamoRunStore(RUN_HISTORY_TABLE_NAME, pipeline.ddb, clock=pipeline.clock)

    counts = recon(pipeline, client, store=restarted)

    assert counts["settled"] == 1 and restarted.get_run(run_id)["status"] == "succeeded" and pipeline.lock() is None


def test_concurrent_passes_settle_a_run_once_and_never_corrupt_it(pipeline):
    client, run_id = started(pipeline)
    client.executions[run_id]["status"] = "FAILED"
    results, barrier = [], threading.Barrier(6)

    def work():
        barrier.wait()
        results.append(recon(pipeline, client))

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sum(r["settled"] for r in results) == 1 and sum(r["errors"] for r in results) == 0
    assert pipeline.store.get_run(run_id)["status"] == "failed" and pipeline.lock() is None


def test_a_pass_with_nothing_stale_reads_but_never_writes(pipeline):
    done = pipeline.new_run()
    pipeline.execute(done)
    client = LocalStepFunctions(pipeline, defer=True)
    before = len(pipeline.ddb.calls)

    counts = recon(pipeline, client)

    assert counts == dict.fromkeys(COUNT_KEYS, 0)
    assert set(pipeline.ddb.calls[before:]) <= {"get_item", "query"} and client.describe_calls == []


# -- States.Runtime and runs the workflow could not record --------------------------------------


def test_a_states_runtime_failure_leaves_an_active_run_that_reconciliation_settles_and_unlocks(pipeline):
    client, run_id = started(pipeline)
    pipeline.override_output("bronze", lambda out: {"records": 1})  # no key: States.Runtime, uncatchable
    client.finish(run_id)
    assert client.executions[run_id]["status"] == "FAILED"
    assert pipeline.store.get_run(run_id)["status"] == "running" and lock_owner(pipeline) == run_id

    counts = recon(pipeline, client)

    assert counts["settled"] == 1 and pipeline.store.get_run(run_id)["status"] == "failed"
    assert lock_owner(pipeline) is None and pipeline.new_run()


def test_an_execution_that_stopped_before_recording_anything_is_settled(pipeline):
    client, run_id = started(pipeline)
    restore = pipeline.fail_ddb("update_item", lambda kw: kw["Key"]["pk"]["S"].startswith(schema.RUN_PK_PREFIX))
    client.finish(run_id)
    restore()

    recon(pipeline, client)

    assert pipeline.store.get_run(run_id)["status"] == "failed" and pipeline.lock() is None


# -- missing executions and missing configuration ----------------------------------------------------


@pytest.mark.parametrize("arn", ["", None])
def test_without_a_state_machine_arn_nothing_is_touched(pipeline, arn):
    pipeline.new_run()
    before = pipeline.raw_items()

    with pytest.raises(ValueError, match="state_machine_arn"):
        reconcile(pipeline.store, LocalStepFunctions(pipeline), arn)

    assert pipeline.raw_items() == before


def test_a_run_whose_execution_never_started_is_interrupted_only_after_the_grace_period(pipeline):
    run_id = pipeline.new_run()  # recorded, but StartExecution was never reached
    client = LocalStepFunctions(pipeline, defer=True)

    early = recon(pipeline, client, after=MISSING_EXECUTION_GRACE_SECONDS - 30)
    assert early["unchanged"] == 1 and pipeline.store.get_run(run_id)["status"] == "queued"
    late = recon(pipeline, client, after=MISSING_EXECUTION_GRACE_SECONDS + 30)

    run = pipeline.store.get_run(run_id)
    assert late["interrupted"] == 1 and run["status"] == "interrupted" and run["error_code"] == "INTERRUPTED"
    assert pipeline.lock() is None


# -- expired leases ---------------------------------------------------------------------------


def test_an_expired_lease_with_a_running_execution_is_renewed_not_interrupted(pipeline):
    client, run_id = started(pipeline)
    pipeline.clock.advance(LEASE + 1)
    assert lease(pipeline) < pipeline.clock.now  # expired

    counts = recon(pipeline, client)

    assert counts["renewed"] == 1 and counts["stale"] == 1 and counts["interrupted"] == 0
    assert pipeline.store.get_run(run_id)["status"] == "queued" and lease(pipeline) > pipeline.clock.now + LEASE - 5
    with pytest.raises(ActiveRunError):
        pipeline.store.create_run(2)


def test_a_new_run_cannot_start_behind_a_running_execution_even_with_an_expired_lease(pipeline):
    client, run_id = started(pipeline)
    pipeline.clock.advance(LEASE + 1)

    with pytest.raises(ActiveRunError):
        client.executor().start(2, requested_by="someone-else")

    assert len(client.started) == 1 and pipeline.store.get_run(run_id)["status"] == "queued"
    assert lock_owner(pipeline) == run_id and len(pipeline.store.list_runs()) == 1


def test_a_duplicate_request_for_the_running_run_still_returns_it_after_the_lease_expired(pipeline):
    client, run_id = started(pipeline, idempotency_key="idem-key-0001")
    pipeline.clock.advance(LEASE + 1)

    again = client.executor().start(2, requested_by="operator-1", idempotency_key="idem-key-0001")

    assert again["run_id"] == run_id and len(client.started) == 1


@pytest.mark.parametrize("execution", ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"])
def test_an_expired_lease_with_a_finished_execution_is_settled_and_a_new_run_may_start(pipeline, execution):
    client, run_id = started(pipeline)
    pipeline.clock.advance(LEASE + 1)
    client.executions[run_id]["status"] = execution

    new = client.executor().start(2)["run_id"]

    assert new != run_id and len(client.started) == 2
    assert pipeline.store.get_run(run_id)["status"] in {"succeeded", "failed", "interrupted"}
    assert lock_owner(pipeline) == new


def test_an_expired_lease_with_a_missing_execution_waits_for_the_grace_period(pipeline):
    run_id = pipeline.new_run()
    client = LocalStepFunctions(pipeline, defer=True)
    pipeline.clock.advance(LEASE + 1)  # long past the grace period: the run is old

    new = client.executor().start(2)["run_id"]

    assert pipeline.store.get_run(run_id)["status"] == "interrupted" and lock_owner(pipeline) == new


@pytest.mark.parametrize(
    "failure",
    [ClientError({"Error": {"Code": "ThrottlingException"}}, "DescribeExecution"),
     ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeExecution"),
     EndpointConnectionError(endpoint_url="https://states.eu-west-1.amazonaws.com")],
)
def test_a_temporary_describe_failure_changes_nothing_and_blocks_a_takeover(pipeline, failure):
    client, _ = started(pipeline)
    pipeline.clock.advance(LEASE + 1)
    client.describe_error = failure
    before = pipeline.raw_items()

    counts = recon(pipeline, client)
    with pytest.raises(ActiveRunError):
        client.executor().start(2)

    assert counts["errors"] == 1 and pipeline.raw_items() == before and len(client.started) == 1
    client.describe_error = None  # AWS recovers: the next pass renews the lease of the running execution
    assert recon(pipeline, client)["renewed"] == 1


def test_a_live_lease_is_not_rewritten_on_every_pass(pipeline):
    client, _ = started(pipeline)
    first = lease(pipeline)
    pipeline.clock.advance(60)

    counts = recon(pipeline, client)

    assert counts["renewed"] == 0 and lease(pipeline) == first
    pipeline.clock.advance(LEASE - RENEW_WHEN_LEASE_BELOW_SECONDS)
    assert recon(pipeline, client)["renewed"] == 1


# -- failed lock release -----------------------------------------------------------------------


@pytest.mark.parametrize("fault", [None, "gold"], ids=["success", "failure"])
def test_a_lock_left_behind_by_a_failed_release_is_released_by_reconciliation(pipeline, fault):
    client, run_id = started(pipeline)
    if fault:
        pipeline.fail(fault)
    restore = pipeline.fail_lock_release()
    client.finish(run_id)
    restore()  # DynamoDB is healthy again when reconciliation runs
    assert lock_owner(pipeline) == run_id and pipeline.store.get_run(run_id)["status"] != "running"

    first = recon(pipeline, client)
    second = recon(pipeline, client)

    assert first["released"] == 1 and second["released"] == 0
    assert lock_owner(pipeline) is None and pipeline.new_run()


def test_a_finished_run_keeps_its_lock_while_the_workflow_is_still_releasing_it(pipeline):
    client, run_id = started(pipeline)
    pipeline.store.update_run(run_id, status="running")
    pipeline.store.update_run(run_id, status="succeeded", stage="completed")  # MarkSucceeded done, then release...
    pipeline.ddb.items[(schema.LOCK_PK, schema.LOCK_SK)] = {  # ...modelled as the release not having happened yet
        "pk": {"S": schema.LOCK_PK}, "sk": {"S": schema.LOCK_SK}, "run_id": {"S": run_id},
        "lease_expires_at": {"N": str(int(pipeline.clock.now + LEASE))}}
    client.executions[run_id]["status"] = "RUNNING"

    counts = recon(pipeline, client)

    assert counts["released"] == 0 and lock_owner(pipeline) == run_id


# -- lock ownership -------------------------------------------------------------------------------


def test_one_run_can_never_release_renew_or_overwrite_another_runs_lock(pipeline):
    _, owner = started(pipeline)
    other = "11111111-2222-4333-8444-555555555555"
    before = pipeline.lock()

    assert pipeline.store.release_lock(other) is False
    assert pipeline.store.renew_lock(other) is False
    assert pipeline.lock() == before

    assert pipeline.store.release_lock(owner) is True and pipeline.store.release_lock(owner) is False


def test_reconciling_an_old_finished_run_leaves_the_current_runs_lock_alone(pipeline):
    old = pipeline.new_run()
    pipeline.store.update_run(old, status="running")
    old_client = LocalStepFunctions(pipeline, defer=True)
    pipeline.clock.advance(LEASE + 1)
    pipeline.store.recover_interrupted()  # the old run was guessed interrupted; its lock expired and was freed
    client, current = started(pipeline)
    client.executions[current]["status"] = "RUNNING"
    old_client.executions[old] = {"arn": f"{STATE_MACHINE_ARN.replace(':stateMachine:', ':execution:')}:{old}",
                                  "status": "SUCCEEDED", "input": {}}
    client.executions[old] = old_client.executions[old]

    recon(pipeline, client)

    assert pipeline.store.get_run(old)["status"] == "succeeded"  # the real outcome wins over the guess
    assert lock_owner(pipeline) == current and pipeline.store.get_run(current)["status"] == "queued"


# -- what must never change -----------------------------------------------------------------------


def test_reconciliation_never_changes_identity_listing_idempotency_or_user_fields(pipeline):
    client, run_id = started(pipeline, idempotency_key="idem-key-0001")
    before = pipeline.raw_items()
    client.executions[run_id]["status"] = "FAILED"

    recon(pipeline, client)

    after = pipeline.raw_items()
    key = (f"{schema.RUN_PK_PREFIX}{run_id}", schema.RUN_SK)
    assert all(after[key][field] == before[key][field] for field in IMMUTABLE)
    assert {k: v for k, v in after.items() if k[0].startswith(schema.IDEMPOTENCY_PK_PREFIX)} == {
        k: v for k, v in before.items() if k[0].startswith(schema.IDEMPOTENCY_PK_PREFIX)}
    assert set(after) - set(before) == set()


def test_the_public_run_format_is_unchanged_for_reconciled_runs(pipeline):
    client, run_id = started(pipeline)
    client.executions[run_id]["status"] = "FAILED"
    recon(pipeline, client)

    public = to_public_run(pipeline.store.get_run(run_id))

    assert set(public) <= {*PUBLIC_RUN_FIELDS, "error", "error_code"}
    assert public["status"] == "failed" and public["error_code"] == "INTERNAL_ERROR"
    assert "requested_by" not in public and "gold_key" not in public


# -- staleness, the recheck window and events ----------------------------------------------------------


def test_active_runs_untouched_for_ten_minutes_are_reported_stale_but_still_checked(pipeline):
    client, _ = started(pipeline)
    events = []

    fresh = recon(pipeline, client, after=60, sink=events.append)
    stale = recon(pipeline, client, after=STALE_AFTER_SECONDS + 60, sink=events.append)

    assert fresh["stale"] == 0 and stale["stale"] == 1 and stale["checked"] == 1
    assert [e["stale"] for e in events] == [True] and events[0]["action"] == "unchanged"


def test_a_run_interrupted_long_ago_is_no_longer_rechecked(pipeline):
    run_id = pipeline.new_run()
    pipeline.store.update_run(run_id, status="interrupted")
    client = LocalStepFunctions(pipeline, defer=True)

    recon(pipeline, client, after=60)
    assert len(client.describe_calls) == 1
    recon(pipeline, client, after=INTERRUPTED_RECHECK_SECONDS + 120)

    assert len(client.describe_calls) == 1


def test_events_carry_only_run_id_statuses_and_error_class_never_messages(pipeline):
    client, _ = started(pipeline)
    client.describe_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "AKIAIOSFODNN7EXAMPLE secret=hunter2"}}, "DescribeExecution")
    events = []

    recon(pipeline, client, after=STALE_AFTER_SECONDS + 60, sink=events.append)

    (event,) = events
    assert set(event) == {"run_id", "action", "from_status", "to_status", "execution_status", "stale", "error_type"}
    assert event["error_type"] == "AccessDeniedException" and "hunter2" not in json.dumps(event)
