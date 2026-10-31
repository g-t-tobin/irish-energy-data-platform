import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from src import api
from src.auth import JwksKeyProvider
from src.dynamodb_run_store import DynamoRunStore
from src.executor import DisabledExecutor, ExecutionStartError
from src.executor_stepfunctions import StepFunctionsExecutor, reconcile_active_runs
from src.run_store import ActiveRunError
from src.settings import Settings
from tests import auth_helpers as h
from tests.fake_dynamodb import FakeDynamoDB
from tests.test_run_store_contract import Clock

ARN = "arn:aws:states:eu-west-1:123456789012:stateMachine:run-pipeline"


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:states:secret"}}, "StartExecution")


@pytest.fixture
def env():
    clock = Clock()
    store = DynamoRunStore("runs-table", FakeDynamoDB(), clock=clock)
    sfn = MagicMock()
    return store, sfn, StepFunctionsExecutor(store, sfn, ARN)


def test_start_records_the_run_then_starts_one_execution_named_after_it(env):
    store, sfn, executor = env

    result = executor.start(3, requested_by="alice")

    assert result["status"] == "queued"
    sfn.start_execution.assert_called_once()
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["stateMachineArn"] == ARN
    assert kwargs["name"] == result["run_id"]
    assert json.loads(kwargs["input"]) == {"run_id": result["run_id"], "days_back": 3}
    assert store.get_run(result["run_id"])["requested_by"] == "alice"


def test_execution_input_carries_no_identity_or_secrets(env):
    _, sfn, executor = env

    executor.start(2, requested_by="alice", idempotency_key="retry-key-0001")

    sent = sfn.start_execution.call_args.kwargs["input"]
    assert "alice" not in sent and "retry-key" not in sent


def test_repeated_request_returns_the_same_run_and_starts_nothing_more(env):
    _, sfn, executor = env

    first = executor.start(2, requested_by="alice", idempotency_key="retry-key-0001")
    again = executor.start(2, requested_by="alice", idempotency_key="retry-key-0001")

    assert again["run_id"] == first["run_id"]
    assert sfn.start_execution.call_count == 1


def test_a_second_run_while_one_is_active_is_refused_before_any_execution(env):
    _, sfn, executor = env
    executor.start(2, requested_by="alice")

    with pytest.raises(ActiveRunError):
        executor.start(2, requested_by="bob")

    assert sfn.start_execution.call_count == 1


def test_an_existing_execution_with_the_same_name_counts_as_started(env):
    store, sfn, executor = env
    sfn.start_execution.side_effect = client_error("ExecutionAlreadyExists")

    result = executor.start(2)

    assert store.get_run(result["run_id"])["status"] == "queued"


@pytest.mark.parametrize(
    "failure,code",
    [
        (client_error("AccessDeniedException"), "ACCESS_DENIED"),
        (client_error("ThrottlingException"), "UPSTREAM_UNAVAILABLE"),
        (EndpointConnectionError(endpoint_url="https://states.internal"), "UPSTREAM_UNAVAILABLE"),
        (RuntimeError("boom"), "INTERNAL_ERROR"),
    ],
)
def test_failed_start_marks_the_run_failed_and_frees_the_lock(env, failure, code):
    store, sfn, executor = env
    sfn.start_execution.side_effect = failure

    with pytest.raises(ExecutionStartError) as exc:
        executor.start(2, requested_by="alice")

    assert str(exc.value) == "The pipeline execution could not be started."
    failed = store.list_runs()[0]
    assert (failed["status"], failed["error_code"]) == ("failed", code)
    sfn.start_execution.side_effect = None
    assert executor.start(2)["status"] == "queued"  # lock was released


@pytest.mark.parametrize("arn", ["", "arn:aws:states:*:*:stateMachine:x", "arn:aws:lambda:eu-west-1:123456789012:function:f"])
def test_invalid_state_machine_arn_is_rejected(arn):
    with pytest.raises(ValueError):
        StepFunctionsExecutor(MagicMock(), MagicMock(), arn)


def test_constructing_the_executor_makes_no_aws_call():
    sfn = MagicMock()

    StepFunctionsExecutor(MagicMock(), sfn, ARN)

    assert sfn.mock_calls == []


# Reconciliation


def _execution_status(store, sfn, status):
    run = store.create_run(2)
    store.update_run(run["run_id"], status="running")
    sfn.describe_execution.return_value = {"status": status}
    return run["run_id"]


@pytest.mark.parametrize(
    "execution,expected",
    [("SUCCEEDED", "succeeded"), ("FAILED", "failed"), ("TIMED_OUT", "failed"), ("ABORTED", "interrupted")],
)
def test_reconcile_settles_runs_with_the_real_outcome(env, execution, expected):
    store, sfn, _ = env
    run_id = _execution_status(store, sfn, execution)

    counts = reconcile_active_runs(store, sfn, ARN)

    assert store.get_run(run_id)["status"] == expected
    assert counts["settled"] + counts["interrupted"] == 1
    described = sfn.describe_execution.call_args.kwargs["executionArn"]
    assert described == f"arn:aws:states:eu-west-1:123456789012:execution:run-pipeline:{run_id}"


def test_reconcile_leaves_running_executions_alone(env):
    store, sfn, _ = env
    run_id = _execution_status(store, sfn, "RUNNING")

    counts = reconcile_active_runs(store, sfn, ARN)

    assert store.get_run(run_id)["status"] == "running"
    assert counts == {"settled": 0, "interrupted": 0, "unchanged": 1}


def test_reconcile_waits_out_a_grace_period_for_a_missing_execution(env):
    store, sfn, _ = env
    run = store.create_run(2)
    sfn.describe_execution.side_effect = client_error("ExecutionDoesNotExist")
    created = datetime.fromisoformat(run["created_at"])

    early = reconcile_active_runs(store, sfn, ARN, now=created + timedelta(seconds=30))
    late = reconcile_active_runs(store, sfn, ARN, now=created + timedelta(minutes=10))

    assert early["unchanged"] == 1 and late["interrupted"] == 1
    assert store.get_run(run["run_id"])["status"] == "interrupted"


@pytest.mark.parametrize("failure", [client_error("AccessDeniedException"), EndpointConnectionError(endpoint_url="x")])
def test_reconcile_changes_nothing_when_describe_fails(env, failure):
    store, sfn, _ = env
    run_id = _execution_status(store, sfn, "SUCCEEDED")
    sfn.describe_execution.side_effect = failure

    assert reconcile_active_runs(store, sfn, ARN)["unchanged"] == 1
    assert store.get_run(run_id)["status"] == "running"


def test_reconcile_ignores_finished_runs(env):
    store, sfn, _ = env
    run = store.create_run(2)
    store.update_run(run["run_id"], status="succeeded")

    reconcile_active_runs(store, sfn, ARN)

    sfn.describe_execution.assert_not_called()


# API integration: the executor is reachable only by overriding the dependency


def _client(executor):
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    app = api.create_app(Settings(api_env="local", cognito=h.CONFIG), key_provider=provider)
    app.dependency_overrides[api.get_executor] = lambda: executor
    return TestClient(app)


VIEWER = h.auth_header(h.token(groups=["viewer"]))
OPERATOR = h.auth_header(h.token(sub="op-1", groups=["operator"]))


def test_roles_are_still_enforced_with_a_real_executor(env):
    store, sfn, executor = env
    client = _client(executor)

    assert client.post("/api/runs", json={}).status_code == 401
    assert client.post("/api/runs", json={}, headers=VIEWER).status_code == 403
    assert sfn.start_execution.call_count == 0

    response = client.post("/api/runs", json={"days_back": 4}, headers=OPERATOR)

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert store.get_run(run_id)["requested_by"] == "op-1"


def test_api_duplicate_conflict_and_failure_mapping(env):
    _store, sfn, executor = env
    client = _client(executor)
    headers = {**OPERATOR, "Idempotency-Key": "client-retry-0001"}

    first = client.post("/api/runs", json={}, headers=headers)
    again = client.post("/api/runs", json={}, headers=headers)
    other = client.post("/api/runs", json={}, headers=OPERATOR)

    assert first.json()["run_id"] == again.json()["run_id"]
    assert (first.status_code, again.status_code) == (202, 202)
    assert other.status_code == 409
    assert other.json() == {"detail": "A pipeline run is already active."}
    assert sfn.start_execution.call_count == 1


def test_api_hides_start_failure_detail(env):
    _, sfn, executor = env
    sfn.start_execution.side_effect = client_error("AccessDeniedException")

    response = _client(executor).post("/api/runs", json={}, headers=OPERATOR)

    assert response.status_code == 503
    assert response.json() == {"detail": "The pipeline could not be started."}
    assert "arn:aws" not in response.text


@pytest.mark.parametrize("key", ["short", "has space here", "x" * 65, "bad/chars!!!"])
def test_invalid_idempotency_header_is_rejected(env, key):
    _, sfn, executor = env

    response = _client(executor).post("/api/runs", json={}, headers={**OPERATOR, "Idempotency-Key": key})

    assert response.status_code == 422
    sfn.start_execution.assert_not_called()


def test_cors_preflight_allows_the_idempotency_header():
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    settings = Settings(api_env="local", cognito=h.CONFIG, cors_allowed_origins=("https://app.example.com",))
    client = TestClient(api.create_app(settings, key_provider=provider))

    response = client.options(
        "/api/runs",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key",
        },
    )

    assert "idempotency-key" in response.headers["access-control-allow-headers"].lower()


# Execution stays disabled


def test_execution_is_still_disabled_by_default_and_unwired():
    assert isinstance(api.get_executor(), DisabledExecutor)
    assert not hasattr(api, "StepFunctionsExecutor")
    assert not hasattr(api, "reconcile_active_runs")


def test_default_app_rejects_operator_posts_without_creating_runs(tmp_path, monkeypatch):
    from src.run_store import RunStore

    store = RunStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(api, "_store", store)
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    client = TestClient(api.create_app(Settings(api_env="local", cognito=h.CONFIG), key_provider=provider))

    response = client.post("/api/runs", json={}, headers={**OPERATOR, "Idempotency-Key": "client-retry-0001"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Pipeline execution is disabled."}
    assert store.list_runs() == []
