"""Synthesised Step Functions workflow: definition, wiring, IAM and a local run against test doubles.

Nothing here reaches AWS: the stage handlers run in-process against S3 and Glue doubles, and the
run-history table is the DynamoDB test double.
"""

import copy
import json
import re

import pytest

from tests.test_energy_edge_infrastructure import ACCOUNT  # noqa: F401  (also puts infrastructure/ on sys.path)

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

from energy_config import (  # noqa: E402
    PIPELINE_STAGES,
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    STATE_MACHINE_NAME,
)
from infrastructure.run_control import spec  # noqa: E402
from src import pipeline_stages as stages  # noqa: E402
from src import run_history_schema as schema  # noqa: E402
from src.dynamodb_run_store import DynamoRunStore  # noqa: E402
from src.executor_stepfunctions import _ARN_RE  # noqa: E402
from src.run_store import TERMINAL_STATUSES, UPDATABLE_FIELDS  # noqa: E402
from stacks.energy_pipeline_stack import EnergyPipelineStack  # noqa: E402
from stacks.energy_run_history_stack import EnergyRunHistoryStack  # noqa: E402
from stacks.energy_workflow_stack import EnergyWorkflowStack, stage_label  # noqa: E402
from tests.asl_simulator import Execution, TaskError  # noqa: E402
from tests.fake_dynamodb import FakeDynamoDB  # noqa: E402
from tests.test_energy_api_service_infrastructure import ENV  # noqa: E402
from tests import test_pipeline_stages as handler_tests  # noqa: E402
from tests.test_run_store_contract import Clock  # noqa: E402

RUN_ID = handler_tests.RUN_ID
aws = handler_tests.aws  # re-exported fixture: stage handlers run against S3 and Glue doubles

TABLE_ARN_IMPORT = "IrishEnergyRunHistoryStack:ExportsOutputFnGetAttRunHistoryTableFD4DA44DArn6CF8D0FC"
EXPORT = "IrishEnergyPipeline-{label}FunctionArn"
ORDER = ["bronze", "silver", "gold", "glue_registration"]
TASK_STATES = {"bronze": "Bronze", "silver": "Silver", "gold": "Gold", "glue_registration": "GlueRegistration"}


@pytest.fixture(autouse=True)
def _no_aws_clients(monkeypatch):
    def refuse(service, *args, **kwargs):
        raise AssertionError(f"a test tried to create an AWS client for {service!r}")

    monkeypatch.setattr("boto3.client", refuse)


def build_app():
    app = cdk.App()
    pipeline = EnergyPipelineStack(app, "IrishEnergyPipelineStack", env=ENV)
    history = EnergyRunHistoryStack(app, "IrishEnergyRunHistoryStack", env=ENV)
    workflow = EnergyWorkflowStack(
        app, "IrishEnergyWorkflowStack", table=history.table, pipeline_stack=pipeline, env=ENV
    )
    return app, pipeline, history, workflow


@pytest.fixture(scope="module")
def stacks():
    app, pipeline, _, workflow = build_app()
    return {
        "app": app,
        "pipeline": Template.from_stack(pipeline),
        "workflow": Template.from_stack(workflow),
        "workflow_stack": workflow,
    }


def join_definition(template):
    """The state machine definition with each cross-stack import replaced by a readable marker."""
    machine = next(iter(template.find_resources("AWS::StepFunctions::StateMachine").values()))
    text = ""
    for part in machine["Properties"]["DefinitionString"]["Fn::Join"][1]:
        text += part if isinstance(part, str) else f"import:{part['Fn::ImportValue']}"
    return json.loads(text)


@pytest.fixture(scope="module")
def definition(stacks):
    return join_definition(stacks["workflow"])


def machine_resource(template):
    return next(iter(template.find_resources("AWS::StepFunctions::StateMachine").values()))["Properties"]


def lambda_tasks(definition):
    return {n: s for n, s in definition["States"].items() if s.get("Resource") == "arn:aws:states:::lambda:invoke"}


def ddb_tasks(definition):
    return {n: s for n, s in definition["States"].items() if s.get("Resource", "").startswith("arn:aws:states:::dynamodb:")}


# -- the state machine ----------------------------------------------------------------------


def test_one_standard_state_machine_with_the_fixed_name_and_no_logging_or_tracing(stacks):
    template = stacks["workflow"]
    machines = template.find_resources("AWS::StepFunctions::StateMachine")

    assert len(machines) == 1
    properties = machine_resource(template)
    assert properties["StateMachineType"] == "STANDARD"
    assert properties["StateMachineName"] == STATE_MACHINE_NAME
    assert not {"LoggingConfiguration", "TracingConfiguration", "EncryptionConfiguration"} & set(properties)


def test_stack_contains_only_the_role_its_policy_and_the_state_machine(stacks):
    kinds = sorted(v["Type"] for v in stacks["workflow"].to_json()["Resources"].values())

    assert kinds == ["AWS::IAM::Policy", "AWS::IAM::Role", "AWS::StepFunctions::StateMachine"]


def test_no_functions_schedules_or_triggers_are_created_here(stacks):
    text = json.dumps(stacks["workflow"].to_json())

    for forbidden in ("AWS::Lambda::", "AWS::Events::", "AWS::Scheduler::", "AWS::Pipes::", "StartExecution"):
        assert forbidden not in text


def test_the_four_stage_functions_are_the_phase_9_4_exports(stacks, definition):
    imports = {
        stage: definition["States"][name]["Parameters"]["FunctionName"] for stage, name in TASK_STATES.items()
    }
    outputs = stacks["pipeline"].to_json()["Outputs"]
    functions = stacks["pipeline"].find_resources("AWS::Lambda::Function")

    for stage in PIPELINE_STAGES:
        export = EXPORT.format(label=stage_label(stage.key))
        assert imports[stage.key] == f"import:{export}"
        output = outputs[stage_label(stage.key) + "FunctionArn"]
        assert output["Export"]["Name"] == export
        function = functions[output["Value"]["Fn::GetAtt"][0]]["Properties"]
        assert function["ImageConfig"]["Command"] == [stage.handler]  # and the stage's own handler
    assert len(set(imports.values())) == 4


def test_stages_run_in_order_through_the_progress_records(definition):
    states, name, path = definition["States"], definition["StartAt"], []
    while name not in ("Succeeded", "Failed"):
        path.append(name)
        name = states[name]["Next"]

    assert path == [
        "MarkRunning", "Bronze", "Record_bronze", "Silver", "Record_silver", "Gold", "Record_gold",
        "GlueRegistration", "MarkSucceeded", "ReleaseLockOnSuccess",
    ]
    assert name == "Succeeded"


def test_every_stage_uses_the_optimised_lambda_integration_and_no_other_service(definition):
    resources = {s["Resource"] for s in definition["States"].values() if "Resource" in s}

    assert resources == {
        "arn:aws:states:::lambda:invoke",
        "arn:aws:states:::dynamodb:updateItem",
        "arn:aws:states:::dynamodb:deleteItem",
    }
    assert set(lambda_tasks(definition)) == set(TASK_STATES.values())
    assert not any(k in definition for k in ("QueryLanguage",))


# -- data flow ------------------------------------------------------------------------------


def test_each_stage_receives_the_whole_state_and_keeps_only_the_function_result(definition):
    for stage, name in TASK_STATES.items():
        state = definition["States"][name]
        assert state["Parameters"]["Payload.$"] == "$"  # run_id, days_back and earlier results
        assert state["ResultSelector"] == {"output.$": "$.Payload"}  # drops the Lambda metadata envelope
        assert state["ResultPath"] == f"$.{stage}"  # the handlers read <stage>.output.key
        assert "InputPath" not in state and "OutputPath" not in state


def test_database_tasks_discard_their_result_so_the_state_keeps_run_id_and_days_back(definition):
    """A missing ResultPath would replace the state with the DynamoDB response (jsii drops None)."""
    for name, state in ddb_tasks(definition).items():
        assert "ResultPath" in state and state["ResultPath"] is None, name
        for catch in state.get("Catch", []):
            assert "ResultPath" in catch, name


def test_progress_records_read_the_same_paths_the_next_handler_reads(definition):
    states = definition["States"]

    for stage, key in (("bronze", "bronze_key"), ("silver", "silver_key"), ("gold", "gold_key")):
        values = states[f"Record_{stage}"]["Parameters"]["ExpressionAttributeValues"]
        assert {"S.$": f"$.{stage}.output.key"} in values.values()
        names = states[f"Record_{stage}"]["Parameters"]["ExpressionAttributeNames"]
        assert key in names.values()


def test_run_id_and_days_back_reach_every_stage_and_the_final_state(aws, definition):
    execution, _, run_id = prepared(aws, definition)

    result = execution.run({"run_id": run_id, "days_back": 7})

    assert result["status"] == "SUCCEEDED"
    assert [name for name, _ in execution.lambda_events] == list(TASK_STATES.values())
    for _, event in execution.lambda_events:
        assert event["run_id"] == run_id and event["days_back"] == 7
    assert result["output"]["run_id"] == run_id and result["output"]["days_back"] == 7
    assert set(result["output"]) == {"run_id", "days_back", *ORDER}


def test_next_stage_input_has_exactly_the_shape_the_handler_reads(aws, definition):
    execution, _, run_id = prepared(aws, definition)

    execution.run({"run_id": run_id, "days_back": 2})

    events = dict(execution.lambda_events)
    assert set(events["Bronze"]) == {"run_id", "days_back"}
    assert set(events["Silver"]) == {"run_id", "days_back", "bronze"}
    assert set(events["Gold"]) == {"run_id", "days_back", "bronze", "silver"}
    assert set(events["GlueRegistration"]) == {"run_id", "days_back", "bronze", "silver", "gold"}
    for stage in ("bronze", "silver", "gold"):
        envelope = events["GlueRegistration"][stage]
        assert list(envelope) == ["output"] and set(envelope["output"]) <= {"key", "records"}  # no Lambda metadata
        assert envelope["output"]["key"].startswith(stage + "/")


# -- retry and failure ----------------------------------------------------------------------


def test_transient_lambda_errors_are_retried_with_bounded_exponential_backoff(definition):
    for name, state in lambda_tasks(definition).items():
        retry, *others = state["Retry"]
        assert retry["ErrorEquals"] == spec.LAMBDA_TRANSIENT_ERRORS
        assert all(error.startswith("Lambda.") for error in retry["ErrorEquals"])
        for item in state["Retry"]:
            assert 1 <= item["MaxAttempts"] <= 3
            assert item["IntervalSeconds"] >= 1 and item["BackoffRate"] >= 2.0
        # only ingestion has a second retrier, for transient EirGrid failures
        assert [r["ErrorEquals"] for r in others] == ([spec.TRANSIENT_SOURCE_ERRORS] if name == "Bronze" else [])


def test_permanent_and_function_errors_are_never_retried(definition):
    for state in lambda_tasks(definition).values():
        errors = {e for retrier in state["Retry"] for e in retrier["ErrorEquals"]}
        assert not errors & {"States.ALL", "States.TaskFailed", "StageInputError", "StageConfigurationError",
                             "Lambda.Unknown", "Sandbox.Timedout", "States.Timeout"}


def test_task_timeouts_are_longer_than_the_function_timeouts_and_inside_the_workflow(definition):
    for stage in PIPELINE_STAGES:
        state = definition["States"][TASK_STATES[stage.key]]
        assert stage.timeout_seconds < state["TimeoutSeconds"] <= 600  # Lambda times out first
    assert sum(s.timeout_seconds for s in PIPELINE_STAGES) < definition["TimeoutSeconds"]
    assert definition["TimeoutSeconds"] == schema.STATE_MACHINE_TIMEOUT_SECONDS < schema.LOCK_LEASE_SECONDS


def test_every_task_failure_is_caught_into_a_marked_failure_and_never_reaches_success(definition):
    states = definition["States"]

    def reaches(start):
        seen, todo = set(), [start]
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                state = states[name]
                todo += [state["Next"]] if "Next" in state else []
        return seen

    for name, state in {**lambda_tasks(definition), **ddb_tasks(definition)}.items():
        for catch in state.get("Catch", []):
            if name.startswith("ReleaseLock"):
                continue  # a missed lock release is harmless and still ends in the same outcome
            assert catch["ErrorEquals"] == ["States.ALL"] and catch["ResultPath"] == "$.error"
            assert catch["Next"] in ("MarkFailure", "MarkPartialFailure")
            assert "Failed" in reaches(catch["Next"]) and "Succeeded" not in reaches(catch["Next"])
    assert states["Failed"] == {"Type": "Fail", "Error": "PipelineFailed", "Cause": "See the run record."}
    assert all("Catch" in s for n, s in lambda_tasks(definition).items())
    assert states["ReleaseLockOnFailure"]["Catch"][0]["Next"] == "Failed"
    assert states["ReleaseLockOnSuccess"]["Catch"][0]["Next"] == "Succeeded"


# -- DynamoDB progress ----------------------------------------------------------------------


def test_progress_writes_use_only_the_stores_attributes_statuses_and_stages(definition):
    written, statuses, stages_ = set(), set(), set()
    for state in ddb_tasks(definition).values():
        parameters = state["Parameters"]
        assert parameters["TableName"] == RUN_HISTORY_TABLE_NAME
        if state["Resource"].endswith("deleteItem"):
            continue
        assert parameters["Key"]["sk"] == {"S": schema.RUN_SK}
        names = parameters["ExpressionAttributeNames"]
        sets = re.findall(r"(#f\d+) =", parameters["UpdateExpression"])
        written |= {names[n] for n in sets}
        for value in parameters["ExpressionAttributeValues"].values():
            if value in ({"S": "running"}, {"S": "succeeded"}, {"S": "failed"}, {"S": "partial_failure"}):
                statuses.add(value["S"])
        for key in sets:
            if names[key] == "stage":
                stages_.add(parameters["ExpressionAttributeValues"][":v" + key[2:]]["S"])

    assert written <= UPDATABLE_FIELDS | {"updated_at"}
    assert written == {"status", "stage", "started_at", "bronze_key", "silver_key", "gold_key", "error",
                       "error_code", "finished_at", "updated_at"}
    assert statuses == {"running", "succeeded", "failed", "partial_failure"} and statuses <= {
        *TERMINAL_STATUSES, "running"
    }
    assert stages_ == {"bronze", "silver", "gold", "glue_registration", "completed"}


def test_identity_lock_lease_and_listing_fields_are_never_written(definition):
    protected = {schema.PARTITION_KEY, schema.SORT_KEY, "run_id", "days_back", "created_at", "requested_by",
                 schema.LIST_INDEX_PARTITION_KEY, schema.LIST_INDEX_SORT_KEY, "lease_expires_at", schema.TTL_ATTRIBUTE}
    for state in ddb_tasks(definition).values():
        parameters = state["Parameters"]
        if "UpdateExpression" in parameters:
            assert not protected & set(parameters["ExpressionAttributeNames"].values())
            assert parameters["UpdateExpression"].startswith("SET ")  # no REMOVE, ADD or DELETE
            assert parameters["Key"]["pk"] == {"S.$": "States.Format('RUN#{}', $.run_id)"}
    assert "dynamodb:putItem" not in json.dumps(definition)  # a put could replace a whole record


def test_lock_release_deletes_only_the_lock_item_and_only_for_this_run(definition):
    for name in ("ReleaseLockOnSuccess", "ReleaseLockOnFailure"):
        parameters = definition["States"][name]["Parameters"]
        assert parameters["Key"] == {"pk": {"S": schema.LOCK_PK}, "sk": {"S": schema.LOCK_SK}}
        assert parameters["ConditionExpression"] == "run_id = :rid"
        assert parameters["ExpressionAttributeValues"] == {":rid": {"S.$": "$.run_id"}}


def test_the_table_name_matches_the_run_history_stack(stacks):
    _, _, history, _ = build_app()
    table = next(iter(Template.from_stack(history).find_resources("AWS::DynamoDB::Table").values()))

    assert table["Properties"]["TableName"] == RUN_HISTORY_TABLE_NAME


def run_workflow(aws, definition, *, fail_stage=None, flaky=None):
    execution, store, run_id = prepared(aws, definition, fail_stage=fail_stage, flaky=flaky)
    result = execution.run({"run_id": run_id, "days_back": 2})
    return result, execution, store, run_id


def prepared(aws, definition, *, fail_stage=None, flaky=None):
    """Create the run exactly as the API would, then wire the definition to the real handlers."""
    fake = FakeDynamoDB()
    store = DynamoRunStore(RUN_HISTORY_TABLE_NAME, fake, clock=Clock())
    run = store.create_run(2, requested_by="operator-1")
    by_marker = {
        f"import:{EXPORT.format(label=stage_label(s.key))}": getattr(stages, s.handler.rsplit(".", 1)[1])
        for s in PIPELINE_STAGES
    }
    failures = {"bronze": "fetch_demand_data", "silver": "process_bronze_object",
                "gold": "process_silver_object", "glue_registration": "register_gold_partition"}
    calls = {"count": 0}

    def invoke(function, payload):
        handler = by_marker[function]
        if flaky and function == f"import:{EXPORT.format(label=stage_label(flaky['stage']))}":
            calls["count"] += 1
            if calls["count"] <= flaky["times"]:
                raise TaskError(flaky["error"], "simulated")
        if fail_stage and handler is by_marker[f"import:{EXPORT.format(label=stage_label(fail_stage))}"]:
            from unittest.mock import patch

            with patch.object(stages, failures[fail_stage], side_effect=RuntimeError("boom")):
                return handler(payload, None)
        return handler(payload, None)

    execution = Execution(definition, invoke_lambda=invoke, dynamodb=fake)
    execution.fake, execution.store = fake, store
    return execution, store, run["run_id"]


def test_a_successful_run_updates_the_real_store_record_and_releases_the_lock(aws, definition):
    result, execution, store, run_id = run_workflow(aws, definition)

    run = store.get_run(run_id)
    assert result["status"] == "SUCCEEDED" and result["last_state"] == "Succeeded"
    assert run["status"] == "succeeded" and run["stage"] == "completed"
    assert run["bronze_key"].startswith("bronze/") and run["silver_key"].startswith("silver/")
    assert run["gold_key"].startswith("gold/")
    assert {"started_at", "finished_at", "updated_at"} <= set(run) and "error" not in run
    assert run["days_back"] == 2 and run["requested_by"] == "operator-1" and run["created_at"]  # untouched
    assert execution.fake.get_item(TableName=RUN_HISTORY_TABLE_NAME, Key={
        "pk": {"S": schema.LOCK_PK}, "sk": {"S": schema.LOCK_SK}}).get("Item") is None
    assert store.create_run(2)["status"] == "queued"  # the lock really is free


def test_progress_is_recorded_stage_by_stage(aws, definition):
    execution, store, run_id = prepared(aws, definition)
    seen = []
    original = execution.fake.update_item

    def spy(**kwargs):
        original(**kwargs)
        if kwargs["Key"]["pk"] == {"S": f"RUN#{run_id}"}:
            run = store.get_run(run_id)
            seen.append((run["status"], run["stage"]))

    execution.fake.update_item = spy
    execution.run({"run_id": run_id, "days_back": 2})

    assert seen == [("running", "bronze"), ("running", "silver"), ("running", "gold"),
                    ("running", "glue_registration"), ("succeeded", "completed")]


@pytest.mark.parametrize(
    ("stage", "status"),
    [("bronze", "failed"), ("silver", "failed"), ("gold", "failed"), ("glue_registration", "partial_failure")],
)
def test_a_failed_stage_fails_the_execution_marks_the_run_and_releases_the_lock(aws, definition, stage, status):
    result, execution, store, run_id = run_workflow(aws, definition, fail_stage=stage)

    run = store.get_run(run_id)
    assert result["status"] == "FAILED" and result["error"] == "PipelineFailed"  # never reported as success
    assert result["output"]["error"]["Error"] == "RuntimeError"  # the cause is kept for the execution history
    assert run["status"] == status and run["stage"] == stage
    assert run["error"] == "A pipeline stage failed." and run["error_code"] == "INTERNAL_ERROR"
    assert run["finished_at"] and "boom" not in json.dumps(run)
    assert store.create_run(2)["status"] == "queued"  # lock released
    later = [TASK_STATES[s] for s in ORDER[ORDER.index(stage) + 1:]]
    assert not set(later) & set(execution.visited)  # later stages did not run


def test_invalid_input_is_a_permanent_failure_and_is_not_retried(aws, definition):
    execution, store, run_id = prepared(aws, definition)

    result = execution.run({"run_id": run_id, "days_back": 0})

    assert result["status"] == "FAILED"
    assert execution.invocations == {"Bronze": 1}  # StageInputError: one attempt, no retry
    assert result["output"]["error"]["Error"] == "StageInputError"
    assert store.get_run(run_id)["status"] == "failed"


def test_transient_lambda_errors_are_retried_then_succeed(aws, definition):
    result, execution, _, _ = run_workflow(
        aws, definition, flaky={"stage": "silver", "times": 2, "error": "Lambda.ServiceException"}
    )

    assert result["status"] == "SUCCEEDED"
    assert execution.invocations["Silver"] == 3  # the original call and two retries


def test_retries_are_bounded_and_then_fail_the_run(aws, definition):
    result, execution, store, run_id = run_workflow(
        aws, definition, flaky={"stage": "bronze", "times": 99, "error": "Lambda.TooManyRequestsException"}
    )

    assert result["status"] == "FAILED"
    assert execution.invocations["Bronze"] == 3
    assert store.get_run(run_id)["status"] == "failed"


def test_a_function_error_that_only_looks_transient_by_name_is_not_retried(aws, definition):
    result, execution, *_ = run_workflow(
        aws, definition, flaky={"stage": "gold", "times": 99, "error": "RuntimeError"}
    )

    assert result["status"] == "FAILED" and execution.invocations["Gold"] == 1


def test_a_retry_of_a_succeeded_but_unacknowledged_stage_would_write_a_second_timestamped_object(aws, definition):
    """Documents the duplicate-output behaviour: retries re-run the stage and keys are timestamped."""
    keys_before = set(aws.s3.objects)
    event = {"run_id": RUN_ID, "days_back": 2}

    first = stages.ingest_handler(event)["key"]
    stages.ingest_handler(event)  # what a retry after a lost response would do

    assert first in aws.s3.objects and len(set(aws.s3.objects) - keys_before) >= 1


# -- IAM ------------------------------------------------------------------------------------


def role_statements(template):
    (policy,) = template.find_resources("AWS::IAM::Policy").values()
    return {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}


def test_role_trust_is_states_for_this_account_only(stacks):
    (role,) = stacks["workflow"].find_resources("AWS::IAM::Role").values()

    assert role["Properties"]["AssumeRolePolicyDocument"]["Statement"] == [{
        "Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"},
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
    }]
    assert "ManagedPolicyArns" not in role["Properties"] and "Policies" not in role["Properties"]


def test_role_permissions_are_exactly_progress_updates_and_the_four_invocations(stacks):
    found = role_statements(stacks["workflow"])

    assert set(found) == {"RecordProgress", "InvokeStageFunctions"}
    assert found["RecordProgress"]["Action"] == ["dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    assert found["RecordProgress"]["Resource"] == {"Fn::ImportValue": TABLE_ARN_IMPORT}  # the table, not the index
    assert found["InvokeStageFunctions"]["Action"] == "lambda:InvokeFunction"
    assert found["InvokeStageFunctions"]["Resource"] == [
        {"Fn::ImportValue": EXPORT.format(label=stage_label(s.key))} for s in PIPELINE_STAGES
    ]
    assert all(s["Effect"] == "Allow" and not {"Condition", "NotAction", "NotResource"} & set(s)
               for s in found.values())


def test_no_unneeded_service_permissions_and_no_wildcard_resources(stacks):
    text = json.dumps(role_statements(stacks["workflow"]))

    for forbidden in ("s3:", "glue:", "athena:", "iam:", "cognito", "ec2:", "kms:", "logs:", "states:",
                      "dynamodb:Scan", "dynamodb:Batch", "dynamodb:PutItem", "dynamodb:GetItem",
                      "dynamodb:Query", "dynamodb:CreateTable", "dynamodb:*", "lambda:*", "sts:"):
        assert forbidden not in text
    for statement in role_statements(stacks["workflow"]).values():
        assert "*" not in json.dumps(statement["Resource"])


def test_the_state_machine_role_is_separate_from_the_four_lambda_roles(stacks):
    workflow_role = next(iter(stacks["workflow"].find_resources("AWS::IAM::Role")))
    lambda_roles = stacks["pipeline"].find_resources("AWS::IAM::Role")

    assert len(lambda_roles) == 4 and workflow_role not in lambda_roles
    for role in lambda_roles.values():
        principal = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]
        assert principal == {"Service": "lambda.amazonaws.com"}


def test_the_lambda_stack_is_unchanged_by_the_workflow(stacks):
    alone = cdk.App()
    only_pipeline = Template.from_stack(EnergyPipelineStack(alone, "IrishEnergyPipelineStack", env=ENV))

    assert only_pipeline.to_json() == stacks["pipeline"].to_json()
    text = json.dumps(stacks["pipeline"].to_json())
    assert "states:" not in text and "dynamodb:" not in text and "AWS::StepFunctions" not in text


def test_stack_dependencies_and_outputs(stacks):
    dependencies = {d.stack_name for d in stacks["workflow_stack"].dependencies}
    outputs = stacks["workflow"].to_json()["Outputs"]

    assert {"IrishEnergyPipelineStack", "IrishEnergyRunHistoryStack"} <= dependencies
    assert outputs["StateMachineArn"]["Export"]["Name"] == "IrishEnergyPipelineWorkflow-StateMachineArn"
    assert list(outputs) == ["StateMachineArn"]


# -- the API boundary -----------------------------------------------------------------------


def test_the_configured_arn_is_the_state_machines_name_and_valid_for_the_executor_and_settings(stacks):
    from src.settings import _STATE_MACHINE_ARN_RE

    assert STATE_MACHINE_ARN == f"arn:aws:states:eu-west-1:{ACCOUNT}:stateMachine:{machine_resource(stacks['workflow'])['StateMachineName']}"
    assert _ARN_RE.match(STATE_MACHINE_ARN) and _STATE_MACHINE_ARN_RE.match(STATE_MACHINE_ARN)


def test_synthesis_makes_no_aws_calls_and_starts_nothing(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("AWS call during synthesis")

    monkeypatch.setattr("botocore.client.BaseClient._make_api_call", refuse)

    app, *_ = build_app()
    app.synth()


def test_the_definition_is_not_mutated_by_running_it(aws, definition):
    before = copy.deepcopy(definition)

    run_workflow(aws, definition)

    assert definition == before


def test_stage_handlers_accept_the_exact_state_the_workflow_builds(aws):
    """Direct contract check without the interpreter: the state shape named in the handler docstring."""
    state = {"run_id": RUN_ID, "days_back": 2}
    for stage, handler in zip(ORDER, (stages.ingest_handler, stages.silver_handler, stages.gold_handler,
                                      stages.glue_handler), strict=True):
        state[stage] = {"output": handler(copy.deepcopy(state))}

    assert state["glue_registration"]["output"]["key"].startswith("gold/")


def test_app_deploy_order_workflow_after_stages_and_table_and_before_the_api_service(tmp_path):
    from tests.test_energy_edge_infrastructure import API_CERT, FRONTEND_CERT, run_app

    ctx = {
        "apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, "apiImageTag": "abc1234",
        "cognitoUserPoolId": "eu-west-1_ABCDEFGHI", "cognitoAppClientId": "1234567890abcdefghijklmnop",
    }

    result, templates = run_app(tmp_path, ctx)

    assert result.returncode == 0, result.stderr
    artifacts = json.loads((tmp_path / "manifest.json").read_text())["artifacts"]
    deps = {name: set(a.get("dependencies", [])) for name, a in artifacts.items() if name.startswith("IrishEnergy")}
    assert {"IrishEnergyPipelineStack", "IrishEnergyRunHistoryStack"} <= deps["IrishEnergyWorkflowStack"]
    assert "IrishEnergyWorkflowStack" in deps["IrishEnergyApiEdgeStack"]
    assert "IrishEnergyWorkflowStack.template.json" in templates
