"""Local integration harness for the whole pipeline: no AWS, no network, no real Lambda.

Real pieces: the four stage handlers, the Bronze/Silver/Gold/Glue processing code, the state
machine definition synthesised by CDK (run by tests/asl_simulator.py), the DynamoDB run store and
the Step Functions executor. Doubles: S3 (files under a temporary directory), Glue, DynamoDB,
the EirGrid HTTP call and the Step Functions client.
"""

import copy
import functools
import io
import json
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infrastructure"))

from aws_cdk.assertions import Template
from energy_config import (
    DATA_LAKE_BUCKET,
    PIPELINE_STAGES,
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    STATE_MACHINE_EXECUTION_ARN_PATTERN,
)
from stacks.energy_workflow_stack import stage_label

from src import pipeline_stages as stages
from src import run_history_schema as schema
from src.dynamodb_run_store import DynamoRunStore
from src.executor_stepfunctions import StepFunctionsExecutor
from src.ingestion.eirgrid import EIRGRID_URL
from tests.asl_simulator import Execution, TaskError
from tests.fake_dynamodb import FakeDynamoDB
from tests.test_energy_workflow_infrastructure import EXPORT, build_app, join_definition
from tests.test_pipeline_stages import FakeGlue
from tests.test_run_store_contract import Clock

__all__ = [
    "BUCKET", "DATA_LAKE_BUCKET", "RUN_HISTORY_TABLE_NAME", "STATE_MACHINE_ARN", "STATE_MACHINE_EXECUTION_ARN_PATTERN",
    "LocalDirS3", "LocalStepFunctions", "Pipeline", "demand_payload", "make_pipeline_fixture", "synthesised_definition",
]

BUCKET = "test-lake-bucket"
STAGE_FUNCTIONS = {
    "bronze": "fetch_demand_data",
    "silver": "process_bronze_object",
    "gold": "process_silver_object",
    "glue_registration": "register_gold_partition",
}
LOCK_KEY = {"pk": {"S": schema.LOCK_PK}, "sk": {"S": schema.LOCK_SK}}


def demand_payload(days=("28-Oct-2026", "29-Oct-2026"), per_day=2) -> dict:
    rows = [
        {"EffectiveTime": f"{day} {hour:02d}:00:00", "FieldName": "SYSTEM_DEMAND", "Region": "ROI",
         "Value": 4000 + 100 * index + hour}
        for index, day in enumerate(days)
        for hour in range(10, 10 + per_day)
    ]
    return {"Rows": rows}


def http_response(status: int, payload: Any = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = EIRGRID_URL
    response._content = json.dumps(payload if payload is not None else {"Rows": []}).encode()
    return response


class LocalDirS3:
    """The S3 client calls the pipeline makes, backed by files in a temporary directory."""

    def __init__(self, root: Path, calls: list) -> None:
        self.root, self.calls = root, calls

    def _path(self, bucket: str, key: str) -> Path:
        assert not key.startswith("/") and ".." not in key.split("/"), key
        return self.root / bucket / key

    def put_object(self, Bucket, Key, Body, **_):
        path = self._path(Bucket, Key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Body)
        self.calls.append(("s3:PutObject", Bucket, Key))

    def get_object(self, Bucket, Key):
        path = self._path(Bucket, Key)
        self.calls.append(("s3:GetObject", Bucket, Key))
        if not path.exists():
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(path.read_bytes())}

    def object_keys(self, prefix: str = "") -> list[str]:
        base = self.root / BUCKET
        found = (p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()) if base.exists() else []
        return sorted(k for k in found if k.startswith(prefix))

    def read(self, key: str) -> bytes:
        return self._path(BUCKET, key).read_bytes()


class RecordingGlue(FakeGlue):
    def __init__(self, calls):
        super().__init__(calls)
        self.registered: dict[str, dict] = {}

    def create_partition(self, DatabaseName, TableName, PartitionInput):
        super().create_partition(DatabaseName, TableName, PartitionInput)
        self.registered[PartitionInput["Values"][0]] = PartitionInput["StorageDescriptor"]["Location"]


class EirGridScript:
    """Replaces requests.get: plays a script of outcomes, then the default payload."""

    def __init__(self) -> None:
        self.default: Any = demand_payload()
        self.script: list[Any] = []
        self.responder = None  # optional callable(params) -> payload, for runs that differ
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, url, params=None, timeout=None, **_):
        with self._lock:
            self.requests.append({"url": url, "params": dict(params or {}), "timeout": timeout})
            step = self.script.pop(0) if self.script else self.default
            if self.responder and not isinstance(step, BaseException | int):
                step = self.responder(dict(params or {}))
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, int):
            return http_response(step)
        return http_response(200, step)


@functools.cache
def _synthesised_definition() -> str:
    _, _, _, workflow = build_app()
    return json.dumps(join_definition(Template.from_stack(workflow)))


def synthesised_definition() -> dict:
    return json.loads(_synthesised_definition())


class Pipeline:
    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[tuple] = []
        self.s3 = LocalDirS3(tmp_path / "s3", self.calls)
        self.glue = RecordingGlue(self.calls)
        self.ddb = FakeDynamoDB()
        self.clock = Clock()
        self.store = DynamoRunStore(RUN_HISTORY_TABLE_NAME, self.ddb, clock=self.clock)
        self.eirgrid = EirGridScript()
        self.definition = synthesised_definition()
        self.stage_faults: dict[str, BaseException] = {}
        self.service_faults: dict[str, list[str]] = {}
        self.lost_responses: dict[str, int] = {}
        self.output_overrides: dict[str, Any] = {}
        self.handler_events: list[tuple[str, dict]] = []
        self.handlers = {
            f"import:{EXPORT.format(label=stage_label(s.key))}": (s.key, getattr(stages, s.handler.rsplit(".", 1)[1]))
            for s in PIPELINE_STAGES
        }

    def client(self, service: str, *args, **kwargs):
        return {"s3": self.s3, "glue": self.glue}[service]  # any other service is a test error

    # -- run setup ----------------------------------------------------------------------

    def new_run(self, days_back: int = 2, **kwargs) -> str:
        return self.store.create_run(days_back, **kwargs)["run_id"]

    def fail(self, stage: str, error: BaseException | None = None) -> None:
        self.stage_faults[stage] = error or RuntimeError("boom")

    def service_error(self, stage: str, times: int, name: str = "Lambda.ServiceException") -> None:
        self.service_faults[stage] = [name] * times

    def override_output(self, stage: str, change) -> None:
        """Replace what a stage returns (after it ran), to model a handler that breaks its contract."""
        self.output_overrides[stage] = change

    def lose_response(self, stage: str, times: int = 1) -> None:
        """The function completes (its writes happen) but Step Functions sees a service error."""
        self.lost_responses[stage] = times

    def fail_ddb(self, operation: str, when, code: str = "ProvisionedThroughputExceededException"):
        """Make matching DynamoDB calls fail; returns a function that removes the fault."""
        original = getattr(self.ddb, operation)

        def wrapper(**kwargs):
            if when(kwargs):
                raise ClientError({"Error": {"Code": code, "Message": code}}, operation)
            return original(**kwargs)

        setattr(self.ddb, operation, wrapper)
        return lambda: setattr(self.ddb, operation, original)

    def fail_when_recording(self, attribute: str, code: str = "ProvisionedThroughputExceededException") -> None:
        self.fail_ddb(
            "update_item",
            lambda kw: attribute in kw.get("ExpressionAttributeNames", {}).values()
            and kw["Key"]["pk"]["S"].startswith(schema.RUN_PK_PREFIX),
            code,
        )

    def fail_lock_release(self) -> None:
        self.fail_ddb("delete_item", lambda kw: kw["Key"] == LOCK_KEY)

    # -- running ------------------------------------------------------------------------

    def _invoke(self, function: str, payload: dict) -> Any:
        stage, handler = self.handlers[function]
        self.handler_events.append((stage, copy.deepcopy(payload)))
        queued = self.service_faults.get(stage)
        if queued:
            raise TaskError(queued.pop(0), "simulated Lambda service failure")
        if stage in self.stage_faults:
            with patch.object(stages, STAGE_FUNCTIONS[stage], side_effect=self.stage_faults[stage]):
                return handler(payload, None)
        result = handler(payload, None)
        if stage in self.output_overrides:
            result = self.output_overrides[stage](result)
        if self.lost_responses.get(stage):
            self.lost_responses[stage] -= 1
            raise TaskError("Lambda.ServiceException", "the response was lost")
        return result

    def execution(self) -> Execution:
        return Execution(self.definition, invoke_lambda=self._invoke, dynamodb=self.ddb)

    def execute(self, run_id: str, days_back: int = 2, execution_input: dict | None = None):
        execution = self.execution()
        result = execution.run(execution_input if execution_input is not None else {"run_id": run_id, "days_back": days_back})
        return result, execution

    def lock(self) -> dict | None:
        return self.ddb.get_item(TableName=RUN_HISTORY_TABLE_NAME, Key=LOCK_KEY).get("Item")

    def raw_items(self) -> dict:
        return copy.deepcopy(self.ddb.items)


class LocalStepFunctions:
    """A Step Functions client double that runs the synthesised workflow in-process.

    By default an execution runs to completion inside start_execution; with defer=True it stays
    RUNNING until finish() is called, which models an API that returns before the workflow ends.
    """

    def __init__(self, pipeline: Pipeline, *, defer: bool = False) -> None:
        self.pipeline, self.defer = pipeline, defer
        self.started: list[dict] = []
        self.executions: dict[str, dict] = {}
        self.start_error: ClientError | BaseException | None = None
        self.describe_error: ClientError | BaseException | None = None
        self.describe_calls: list[str] = []

    @staticmethod
    def _arn(state_machine_arn: str, name: str) -> str:
        return state_machine_arn.replace(":stateMachine:", ":execution:") + f":{name}"

    def start_execution(self, *, stateMachineArn, name, input):
        if self.start_error:
            raise self.start_error
        arn = self._arn(stateMachineArn, name)
        if name in self.executions:
            raise ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}}, "StartExecution")
        payload = json.loads(input)
        self.started.append({"stateMachineArn": stateMachineArn, "name": name, "input": payload})
        self.executions[name] = {"arn": arn, "status": "RUNNING", "input": payload}
        if not self.defer:
            self.finish(name)
        return {"executionArn": arn, "startDate": "2026-10-30T12:00:00Z"}

    def finish(self, name: str, status: str | None = None) -> None:
        record = self.executions[name]
        if status is None:
            result, _ = self.pipeline.execute("", execution_input=record["input"])
            status = result["status"]
            record["result"] = result
        record["status"] = status

    def describe_execution(self, *, executionArn):
        self.describe_calls.append(executionArn)
        if self.describe_error:
            raise self.describe_error
        for record in self.executions.values():
            if record["arn"] == executionArn:
                return {"executionArn": executionArn, "status": record["status"]}
        raise ClientError({"Error": {"Code": "ExecutionDoesNotExist", "Message": "missing"}}, "DescribeExecution")

    def executor(self) -> StepFunctionsExecutor:
        return StepFunctionsExecutor(self.pipeline.store, self, STATE_MACHINE_ARN)


def make_pipeline_fixture():
    """A `pipeline` fixture for a test module: `pipeline = make_pipeline_fixture()`."""

    @pytest.fixture
    def pipeline(tmp_path, monkeypatch):
        harness = Pipeline(tmp_path)
        monkeypatch.setenv("S3_BUCKET", BUCKET)
        monkeypatch.setattr("boto3.client", harness.client)
        monkeypatch.setattr("requests.get", harness.eirgrid)
        return harness

    return pipeline
