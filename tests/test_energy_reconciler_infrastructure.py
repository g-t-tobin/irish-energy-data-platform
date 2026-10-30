"""The optional scheduled reconciler as synthesised CloudFormation: absent by default, minimal when on."""

import json
import os
from datetime import UTC, datetime

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from tests.pipeline_harness import (
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    STATE_MACHINE_EXECUTION_ARN_PATTERN,
    LocalStepFunctions,
    make_pipeline_fixture,
)
from tests.test_energy_edge_infrastructure import (  # also puts infrastructure/ on the path
    API_CERT,
    FRONTEND_CERT,
    run_app,
)
from tests.test_energy_workflow_infrastructure import ENV, TABLE_ARN_IMPORT

from energy_config import (  # isort: skip
    ConfigError,
    load_reconciler_config,
)
from stacks.energy_reconciler_stack import EnergyReconcilerStack  # isort: skip
from stacks.energy_run_history_stack import EnergyRunHistoryStack  # isort: skip

pipeline = make_pipeline_fixture()
TABLE = {"Fn::ImportValue": TABLE_ARN_IMPORT}


def synth(state="DISABLED"):
    from energy_config import ReconcilerConfig

    app = cdk.App()
    history = EnergyRunHistoryStack(app, "IrishEnergyRunHistoryStack", env=ENV)
    stack = EnergyReconcilerStack(app, "IrishEnergyReconcilerStack", config=ReconcilerConfig(state), table=history.table, env=ENV)
    return app, Template.from_stack(stack)


@pytest.fixture(scope="module")
def template():
    return synth()[1]


def only(template, kind):
    (resource,) = template.find_resources(kind).values()
    return resource["Properties"]


def statements(template, role_prefix):
    for resource in template.find_resources("AWS::IAM::Policy").values():
        if any(r["Ref"].startswith(role_prefix) for r in resource["Properties"]["Roles"]):
            return {s["Sid"]: s for s in resource["Properties"]["PolicyDocument"]["Statement"]}
    raise AssertionError(role_prefix)


def as_list(value):
    return value if isinstance(value, list) else [value]


# -- configuration: off by default -------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "false", "FALSE", False])
def test_the_reconciler_is_absent_unless_enabled(value):
    assert load_reconciler_config({"reconcilerEnabled": value}.get) is None


def test_enabling_it_leaves_the_schedule_disabled_until_asked():
    assert load_reconciler_config({"reconcilerEnabled": "true"}.get).schedule_state == "DISABLED"
    assert load_reconciler_config({"reconcilerEnabled": True, "reconcilerScheduleState": "enabled"}.get).schedule_state == "ENABLED"


@pytest.mark.parametrize("context", [{"reconcilerEnabled": "yes"}, {"reconcilerEnabled": "true", "reconcilerScheduleState": "ON"}])
def test_invalid_reconciler_context_is_rejected(context):
    with pytest.raises(ConfigError):
        load_reconciler_config(context.get)


def test_default_synthesis_does_not_contain_the_reconciler(tmp_path):
    result, templates = run_app(tmp_path)

    assert result.returncode == 0 and "IrishEnergyReconcilerStack.template.json" not in templates


@pytest.mark.parametrize(("context", "state"), [({"reconcilerEnabled": "true"}, "DISABLED"),
                                                ({"reconcilerEnabled": "true", "reconcilerScheduleState": "ENABLED"}, "ENABLED")])
def test_the_app_adds_the_reconciler_stack_only_when_asked(tmp_path, context, state):
    ctx = {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, **context}

    result, templates = run_app(tmp_path, ctx)

    assert result.returncode == 0, result.stderr
    assert "IrishEnergyReconcilerStack.template.json" in templates
    schedule = only(Template.from_json(json.loads((tmp_path / "IrishEnergyReconcilerStack.template.json").read_text())),
                    "AWS::Scheduler::Schedule")
    assert schedule["State"] == state
    manifest = json.loads((tmp_path / "manifest.json").read_text())["artifacts"]
    assert "IrishEnergyRunHistoryStack" in manifest["IrishEnergyReconcilerStack"]["dependencies"]


def test_an_invalid_reconciler_setting_stops_synthesis(tmp_path):
    result, templates = run_app(tmp_path, {"reconcilerEnabled": "maybe"})

    assert result.returncode != 0 and "Configuration error" in result.stderr + result.stdout and templates == []


# -- the stack ----------------------------------------------------------------------------------------


def test_the_stack_has_one_function_two_roles_a_log_group_and_a_disabled_schedule(template):
    kinds = sorted(r["Type"] for r in template.to_json()["Resources"].values())

    assert kinds == sorted(["AWS::Lambda::Function", "AWS::IAM::Role", "AWS::IAM::Role", "AWS::IAM::Policy",
                            "AWS::IAM::Policy", "AWS::Logs::LogGroup", "AWS::Scheduler::Schedule"])
    schedule = only(template, "AWS::Scheduler::Schedule")
    assert schedule["State"] == "DISABLED" and schedule["ScheduleExpression"] == "rate(5 minutes)"
    assert schedule["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert schedule["Target"]["RetryPolicy"] == {"MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0}
    assert schedule["Target"]["Input"] == "{}"


def test_the_function_is_a_small_zip_with_only_the_two_settings_it_needs(template):
    function = only(template, "AWS::Lambda::Function")

    assert function["Handler"] == "src.reconcile_runs.handler" and function["Runtime"] == "python3.13"
    assert function["Architectures"] == ["x86_64"] and function["Timeout"] == 60 and function["MemorySize"] == 256
    assert function["Environment"]["Variables"] == {
        "RUN_HISTORY_TABLE": RUN_HISTORY_TABLE_NAME, "PIPELINE_STATE_MACHINE_ARN": STATE_MACHINE_ARN}
    assert "PackageType" not in function and "ReservedConcurrentExecutions" not in function


def test_the_package_contains_only_source(tmp_path):
    app, _ = synth()
    directory = app.synth().directory
    zips = [d for d in os.listdir(directory) if d.startswith("asset.") and os.path.isdir(f"{directory}/{d}")]

    assert zips and all(sorted(os.listdir(f"{directory}/{d}")) == ["src"] for d in zips)
    files = [os.path.join(root, f) for d in zips for root, _, names in os.walk(f"{directory}/{d}") for f in names]
    assert any(f.endswith("src/reconcile_runs.py") for f in files)
    assert not [f for f in files if "__pycache__" in f or ".env" in os.path.basename(f) or f.endswith(".sqlite3")]


def test_the_reconciler_role_is_exactly_read_executions_and_settle_run_history(template):
    found = statements(template, "Role")

    assert {sid: (s["Action"], s["Resource"]) for sid, s in found.items() if sid != "WriteOwnLogs"} == {
        "InspectOwnExecutions": ("states:DescribeExecution", STATE_MACHINE_EXECUTION_ARN_PATTERN),
        "SettleRunHistoryItems": (["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], TABLE),
        "ListRunsByIndex": ("dynamodb:Query", {"Fn::Join": ["", [TABLE, "/index/gsi1"]]}),
    }
    assert found["WriteOwnLogs"]["Action"] == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert all(s["Effect"] == "Allow" and not {"Condition", "NotAction", "NotResource"} & set(s) for s in found.values())


def test_no_start_write_wildcard_or_other_service_permission_exists(template):
    text = json.dumps(statements(template, "Role"))

    for forbidden in ("StartExecution", "StartSyncExecution", "StopExecution", "states:*", "dynamodb:PutItem", "dynamodb:Scan",
                      "dynamodb:Batch", "dynamodb:Transact", "dynamodb:Create", "dynamodb:Delete" + "Table", "s3:", "glue:",
                      "athena:", "cognito", "iam:", "ecs:", "ec2:", "kms:", "secretsmanager:", "lambda:", "sts:", "ecr:"):
        assert forbidden not in text
    assert '"*"' not in text and "AWS::IAM::ManagedPolicy" not in json.dumps(template.to_json())
    assert "ManagedPolicyArns" not in json.dumps(only(template, "AWS::Lambda::Function"))


def test_both_roles_are_account_conditioned_and_the_scheduler_can_only_invoke_this_function(template):
    roles = template.find_resources("AWS::IAM::Role")
    trusts = {r["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["Service"]: r["Properties"][
        "AssumeRolePolicyDocument"]["Statement"][0]["Condition"] for r in roles.values()}

    assert trusts == {"lambda.amazonaws.com": {"StringEquals": {"aws:SourceAccount": "851725545966"}},
                      "scheduler.amazonaws.com": {"StringEquals": {"aws:SourceAccount": "851725545966"}}}
    (invoke,) = statements(template, "SchedulerRole").values()
    assert invoke["Action"] == "lambda:InvokeFunction" and "Fn::GetAtt" in invoke["Resource"]


def test_the_permissions_cover_exactly_the_calls_the_reconciler_makes(pipeline):
    """Run every reconciler path against the doubles and match the operations used to the role."""
    from src.reconciliation import reconcile

    client = LocalStepFunctions(pipeline, defer=True)
    run_id = client.executor().start(2)["run_id"]
    when = datetime.fromtimestamp(pipeline.clock.now, UTC)
    pipeline.ddb.calls.clear()
    pipeline.clock.advance(46 * 60)
    reconcile(pipeline.store, client, STATE_MACHINE_ARN, now=when)  # renew
    client.executions[run_id]["status"] = "FAILED"
    reconcile(pipeline.store, client, STATE_MACHINE_ARN, now=when)  # settle and release
    orphan = pipeline.new_run()
    reconcile(pipeline.store, client, STATE_MACHINE_ARN, now=datetime.fromtimestamp(pipeline.clock.now + 900, UTC))  # interrupt
    assert pipeline.store.get_run(orphan)["status"] == "interrupted"

    iam = {"get_item": "dynamodb:GetItem", "update_item": "dynamodb:UpdateItem",
           "delete_item": "dynamodb:DeleteItem", "query": "dynamodb:Query"}
    used = {iam[call] for call in set(pipeline.ddb.calls) - {"transact_write_items"}}
    template = synth()[1]
    granted = {a for s in statements(template, "Role").values() for a in as_list(s["Action"]) if a.startswith("dynamodb:")}
    assert used == granted and set(client.describe_calls) and not hasattr(client, "stop_execution")
