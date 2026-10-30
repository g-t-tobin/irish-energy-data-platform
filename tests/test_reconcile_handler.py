"""The scheduled reconciler's Lambda handler: configuration, logs, metrics and what it can never do."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from src import reconcile_runs
from src.dynamodb_run_store import DynamoRunStore
from src.reconcile_runs import ReconcilerConfigurationError, handler
from src.run_store import RunStoreError
from tests.pipeline_harness import (
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    LocalStepFunctions,
    make_pipeline_fixture,
)

pipeline = make_pipeline_fixture()
ROOT = Path(__file__).resolve().parents[1]
CANARY = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture
def wired(pipeline, monkeypatch):
    """The handler wired to the DynamoDB and Step Functions doubles, on the real clock."""
    pipeline.store = DynamoRunStore(RUN_HISTORY_TABLE_NAME, pipeline.ddb)
    client = LocalStepFunctions(pipeline, defer=True)
    clients = {"dynamodb": pipeline.ddb, "stepfunctions": client}
    monkeypatch.setattr("src.reconcile_runs.boto3.client", lambda service, *a, **k: clients[service])
    monkeypatch.setenv("RUN_HISTORY_TABLE", RUN_HISTORY_TABLE_NAME)
    monkeypatch.setenv("PIPELINE_STATE_MACHINE_ARN", STATE_MACHINE_ARN)
    return pipeline, client


def lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_the_handler_settles_a_finished_execution_and_reports_counts(wired, capsys):
    pipeline, client = wired
    run_id = client.executor().start(2)["run_id"]
    client.executions[run_id]["status"] = "FAILED"

    counts = handler({}, None)

    assert counts["settled"] == 1 and counts["errors"] == 0
    assert pipeline.store.get_run(run_id)["status"] == "failed" and pipeline.lock() is None
    run_line, summary = lines(capsys)
    assert run_line["event"] == "reconcile.run" and run_line["action"] == "settled" and run_line["run_id"] == run_id
    assert summary["event"] == "reconcile.summary" and summary["RunsSettled"] == 1


def test_the_summary_is_a_valid_embedded_metric_with_no_dimensions(wired, capsys):
    handler({}, None)

    (summary,) = lines(capsys)
    (block,) = summary["_aws"]["CloudWatchMetrics"]
    assert block["Namespace"] == "IrishEnergy/Reconciler" and block["Dimensions"] == [[]]  # bounded cardinality
    names = {m["Name"] for m in block["Metrics"]}
    assert names == {"RunsChecked", "StaleActiveRuns", "RunsSettled", "RunsInterrupted", "LeasesRenewed",
                     "LocksReleased", "RunsUnchanged", "ReconcileErrors", "DurationMs"}
    assert all(isinstance(summary[name], int | float) for name in names) and isinstance(summary["_aws"]["Timestamp"], int)
    assert {m["Unit"] for m in block["Metrics"]} == {"Count", "Milliseconds"}


@pytest.mark.parametrize("missing", ["RUN_HISTORY_TABLE", "PIPELINE_STATE_MACHINE_ARN"])
def test_incomplete_configuration_fails_before_any_client_is_created(monkeypatch, missing):
    def refuse(*args, **kwargs):
        raise AssertionError("an AWS client was created")

    monkeypatch.setattr("src.reconcile_runs.boto3.client", refuse)
    monkeypatch.setenv("RUN_HISTORY_TABLE", "irish-energy-run-history")
    monkeypatch.setenv("PIPELINE_STATE_MACHINE_ARN", STATE_MACHINE_ARN)
    monkeypatch.delenv(missing)

    with pytest.raises(ReconcilerConfigurationError, match=missing):
        handler({}, None)


@pytest.mark.parametrize("arn", ["arn:aws:states:::stateMachine:x", "arn:aws:lambda:eu-west-1:851725545966:function:f",
                                 STATE_MACHINE_ARN + "/x"])
def test_a_malformed_state_machine_arn_is_rejected(monkeypatch, arn):
    monkeypatch.setenv("RUN_HISTORY_TABLE", "irish-energy-run-history")
    monkeypatch.setenv("PIPELINE_STATE_MACHINE_ARN", arn)

    with pytest.raises(ReconcilerConfigurationError):
        handler({}, None)


def test_a_storage_failure_is_logged_without_detail_and_fails_the_invocation(wired, capsys):
    pipeline, _ = wired
    pipeline.fail_ddb("query", lambda kw: True, code="ProvisionedThroughputExceededException")

    with pytest.raises(RunStoreError):
        handler({}, None)

    (record,) = lines(capsys)
    assert record["event"] == "reconcile.failed" and record["level"] == "ERROR"
    assert record["error_type"] == "RunStoreError" and record["ReconcileFailures"] == 1


def test_logs_never_contain_messages_payloads_or_credentials(wired, capsys, monkeypatch):
    _, client = wired
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canarySecretValue123")
    run_id = client.executor().start(2)["run_id"]
    client.describe_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"{CANARY} token=abc secret=hunter2"}}, "DescribeExecution")

    counts = handler({}, None)

    out = capsys.readouterr().out
    assert counts["errors"] == 1 and run_id in out
    assert not any(secret in out for secret in (CANARY, "hunter2", "token=abc", "canarySecretValue123"))
    assert "AccessDeniedException" in out  # the error class is logged, the message is not


def test_the_handler_cannot_start_executions_or_touch_other_services(wired):
    pipeline, client = wired
    client.start_execution = lambda **kwargs: (_ for _ in ()).throw(AssertionError("StartExecution called"))
    pipeline.new_run()
    before = len(pipeline.ddb.calls)

    handler({}, None)

    assert set(pipeline.ddb.calls[before:]) <= {"get_item", "query", "update_item", "delete_item"}
    assert client.describe_calls
    for module in (reconcile_runs, sys.modules["src.reconciliation"]):
        source = Path(module.__file__).read_text()
        for forbidden in ("start_execution", "StartExecution", "stop_execution", "put_item", "s3", "glue", "athena"):
            assert forbidden not in source.replace("s3:", ""), (module.__name__, forbidden)


def test_the_handler_imports_without_the_api_or_pipeline_dependencies():
    code = (
        "import sys\n"
        "for name in ('pandas', 'pyarrow', 'fastapi', 'uvicorn', 'requests', 'jwt'):\n"
        "    sys.modules[name] = None\n"
        "import src.reconcile_runs\n"
        "print('ok')\n"
    )

    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr
