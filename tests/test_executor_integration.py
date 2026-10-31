"""StepFunctionsExecutor against a Step Functions client double (no AWS), and the disabled API."""

import fnmatch
import json
import logging
import re
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from src import api
from src.auth import JwksKeyProvider
from src.dynamodb_run_store import DynamoRunStore
from src.executor import ExecutionStartError
from src.executor_stepfunctions import StepFunctionsExecutor, reconcile_active_runs
from src.run_store import RunStore
from src.settings import RunHistorySettings, Settings
from tests import auth_helpers as h
from tests.pipeline_harness import (
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    STATE_MACHINE_EXECUTION_ARN_PATTERN,
    LocalStepFunctions,
    make_pipeline_fixture,
)

pipeline = make_pipeline_fixture()
CANARY = "AKIAIOSFODNN7EXAMPLE"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.canary-token-payload.signature"


def client_error(code, message="denied"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "StartExecution")


def test_start_execution_gets_one_named_execution_with_only_the_approved_input(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)

    started = client.executor().start(5, requested_by="operator-1", idempotency_key="idem-key-0001")

    (call,) = client.started
    assert started == {"run_id": call["name"], "status": "queued"}
    assert call["stateMachineArn"] == STATE_MACHINE_ARN
    assert call["input"] == {"run_id": started["run_id"], "days_back": 5}  # no user, key, token or hint
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", call["name"])  # a valid execution name, the run ID itself


def test_the_execution_name_is_deterministic_so_a_second_start_cannot_duplicate_it(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    pipeline.store.update_run(run_id, status="failed")  # let a new run begin
    other = client.executor().start(2)["run_id"]

    assert run_id != other and set(client.executions) == {run_id, other}
    # Starting the same run's execution again is what a lost response would do: it is accepted as started.
    pipeline.store.create_run = MagicMock(return_value={"run_id": run_id, "status": "queued"})
    assert client.executor().start(2)["run_id"] == run_id and len(client.started) == 2


def test_the_execution_arn_the_api_builds_matches_the_policy_and_the_arn_aws_returned(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]

    described = client.describe_execution(executionArn=client.executions[run_id]["arn"])

    assert fnmatch.fnmatchcase(described["executionArn"], STATE_MACHINE_EXECUTION_ARN_PATTERN)  # IAM allows it
    reconcile_active_runs(pipeline.store, client, STATE_MACHINE_ARN)
    assert client.describe_calls[-1] == described["executionArn"]  # reconciliation asks for exactly that ARN


@pytest.mark.parametrize("response", [{}, {"executionArn": "arn:aws:states:eu-west-1:851725545966:execution:x:y"}, None])
def test_the_start_response_is_not_needed_and_never_returned_to_callers(pipeline, response):
    client = MagicMock()
    client.start_execution.return_value = response

    result = StepFunctionsExecutor(pipeline.store, client, STATE_MACHINE_ARN).start(2)

    assert set(result) == {"run_id", "status"}


def test_an_execution_that_already_exists_counts_as_started_and_keeps_the_run_active(pipeline):
    client = LocalStepFunctions(pipeline, defer=True)
    client.start_error = client_error("ExecutionAlreadyExists")

    result = client.executor().start(2)

    assert result["status"] == "queued" and pipeline.store.get_run(result["run_id"])["status"] == "queued"
    assert pipeline.lock() is not None


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (client_error("AccessDeniedException", f"{CANARY} not authorised"), "ACCESS_DENIED"),
        (client_error("ThrottlingException"), "UPSTREAM_UNAVAILABLE"),
        (EndpointConnectionError(endpoint_url="https://states.eu-west-1.amazonaws.com"), "UPSTREAM_UNAVAILABLE"),
        (RuntimeError(f"failed with AWS_SECRET_ACCESS_KEY={CANARY} and token={TOKEN}"), "INTERNAL_ERROR"),
    ],
)
def test_start_failures_are_generic_for_callers_logged_safely_and_free_the_lock(pipeline, caplog, failure, code):
    client = LocalStepFunctions(pipeline)
    client.start_error = failure
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ExecutionStartError) as caught:
        client.executor().start(2)

    (run,) = pipeline.store.list_runs()
    assert str(caught.value) == "The pipeline execution could not be started."
    assert (run["status"], run["error_code"]) == ("failed", code) and pipeline.lock() is None
    leaked = json.dumps(run) + caplog.text + str(caught.value)
    assert CANARY not in leaked and TOKEN not in leaked and "AWS_SECRET" not in leaked.replace("[redacted]", "")


@pytest.mark.parametrize("days_back", [0, 31, -1, True, "2", None, 2.5])
def test_invalid_input_is_rejected_before_any_run_or_execution_exists(pipeline, days_back):
    client = LocalStepFunctions(pipeline)

    with pytest.raises(ValueError):
        client.executor().start(days_back)

    assert client.started == [] and pipeline.store.list_runs() == [] and pipeline.lock() is None


@pytest.mark.parametrize("key", ["short", "has space!", "x" * 65, ""])
def test_an_invalid_idempotency_key_is_rejected_before_any_run_exists(pipeline, key):
    client = LocalStepFunctions(pipeline)

    with pytest.raises(ValueError):
        client.executor().start(2, idempotency_key=key)

    assert client.started == [] and pipeline.store.list_runs() == []


@pytest.mark.parametrize(
    ("status", "outcome"),
    [("SUCCEEDED", "succeeded"), ("FAILED", "failed"), ("TIMED_OUT", "failed"), ("ABORTED", "interrupted"),
     ("RUNNING", "queued")],
)
def test_reconciliation_after_an_api_restart_uses_a_fresh_store_and_executor(pipeline, status, outcome):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    client.finish(run_id, status)

    restarted = DynamoRunStore(RUN_HISTORY_TABLE_NAME, pipeline.ddb, clock=pipeline.clock)  # a new process
    counts = reconcile_active_runs(restarted, client, STATE_MACHINE_ARN)

    assert restarted.get_run(run_id)["status"] == outcome and sum(counts.values()) == 1


def test_reconciliation_logs_and_stores_no_credentials(pipeline, caplog):
    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    client.describe_error = client_error("AccessDeniedException", f"{CANARY} {TOKEN}")
    caplog.set_level(logging.DEBUG)

    reconcile_active_runs(pipeline.store, client, STATE_MACHINE_ARN)

    assert CANARY not in caplog.text and TOKEN not in caplog.text
    assert CANARY not in json.dumps(pipeline.store.get_run(run_id))


# -- the API never touches the executor while execution is disabled ---------------------------


def operator_post(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(api, "_store", RunStore(tmp_path / "runs.sqlite3"))
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    with TestClient(api.create_app(settings, key_provider=provider)) as client:  # runs start-up as well
        return client.post("/api/runs", json={"days_back": 2}, headers=h.auth_header(h.token(groups=["operator"])))


def test_the_api_neither_builds_nor_calls_an_executor_nor_creates_an_aws_client_when_disabled(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the executor or an AWS client was used while execution is disabled")

    settings = Settings(api_env="local", cognito=h.CONFIG, pipeline_state_machine_arn=STATE_MACHINE_ARN)
    with patch("src.executor_stepfunctions.StepFunctionsExecutor.__init__", refuse), \
            patch("src.executor_stepfunctions.StepFunctionsExecutor.start", refuse), \
            patch("src.executor_stepfunctions.reconcile_active_runs", refuse), \
            patch("boto3.client", refuse), patch("boto3.resource", refuse), patch("boto3.Session", refuse):
        response = operator_post(tmp_path, monkeypatch, settings)

    assert response.status_code == 503
    assert response.json() == {"detail": "Pipeline execution is disabled."}
    assert RunStore(tmp_path / "runs.sqlite3").list_runs() == []


def test_a_viewer_is_refused_before_the_disabled_check_and_an_anonymous_caller_is_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_store", RunStore(tmp_path / "runs.sqlite3"))
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    client = TestClient(api.create_app(Settings(api_env="local", cognito=h.CONFIG), key_provider=provider))

    viewer = client.post("/api/runs", json={"days_back": 2}, headers=h.auth_header(h.token(groups=["viewer"])))
    anonymous = client.post("/api/runs", json={"days_back": 2})

    assert (viewer.status_code, anonymous.status_code) == (403, 401)


def test_enabled_api_initialises_step_functions_with_the_shared_store(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "enabled-runs.sqlite3")
    monkeypatch.setattr(api, "_store", store)

    settings = Settings(
        api_env="local",
        cognito=h.CONFIG,
        pipeline_execution_enabled=True,
        pipeline_state_machine_arn=STATE_MACHINE_ARN,
        run_history=RunHistorySettings(
            backend="dynamodb",
            table=RUN_HISTORY_TABLE_NAME,
            region="eu-west-1",
        ),
    )
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    step_functions_client = MagicMock()
    reconciliation_counts = {
        "settled": 0,
        "interrupted": 0,
        "unchanged": 0,
    }

    with (
        patch("boto3.client", return_value=step_functions_client) as create_client,
        patch("src.executor_stepfunctions.StepFunctionsExecutor") as executor_class,
        patch(
            "src.executor_stepfunctions.reconcile_active_runs",
            return_value=reconciliation_counts,
        ) as reconcile,
    ):
        app = api.create_app(settings, key_provider=provider)

        with TestClient(app):
            create_client.assert_called_once_with(
                "stepfunctions", region_name="eu-west-1"
            )
            executor_class.assert_called_once_with(
                store, step_functions_client, STATE_MACHINE_ARN
            )
            reconcile.assert_called_once_with(
                store, step_functions_client, STATE_MACHINE_ARN
            )
            assert app.state.executor is executor_class.return_value
