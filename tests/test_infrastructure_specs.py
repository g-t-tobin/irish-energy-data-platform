import json

import pytest

from infrastructure.run_control import spec
from src import run_history_schema as schema

ACCOUNT, REGION, BUCKET = "123456789012", "eu-west-1", "example-lake-bucket"
TABLE = spec.table_arn(REGION, ACCOUNT, "runs-table")
MACHINE = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:run-pipeline"
FUNCTIONS = {
    stage: f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{stage}"
    for stage in spec.STAGES
}


def policies():
    return {
        "api": spec.api_task_policy(
            bucket=BUCKET, table=TABLE, state_machine_arn=MACHINE
        ),
        "machine": spec.state_machine_role_policy(
            table=TABLE, stage_function_arns=list(FUNCTIONS.values())
        ),
        "stages": spec.pipeline_stage_role_policy(
            bucket=BUCKET,
            region=REGION,
            account=ACCOUNT,
            glue_database="db",
            glue_table="tbl",
        ),
    }


def statements(policy):
    return policy["Statement"]


def actions(policy):
    return {a for s in statements(policy) for a in s["Action"]}


@pytest.mark.parametrize("name", ["api", "machine", "stages"])
def test_policies_have_no_wildcard_actions_or_bare_resources(name):
    for statement in statements(policies()[name]):
        assert statement["Effect"] == "Allow"
        for action in statement["Action"]:
            assert "*" not in action
        for resource in statement["Resource"]:
            assert resource != "*"
            assert resource.count("*") <= 1 and (
                resource.endswith("*") or "*" not in resource
            )


def test_api_role_is_limited_to_gold_read_run_history_and_one_state_machine():
    api_policy = policies()["api"]

    assert actions(api_policy) == {
        "s3:ListBucket",
        "s3:GetObject",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "states:DescribeExecution",
    }
    flat = json.dumps(api_policy)
    for forbidden in (
        "s3:PutObject",
        "glue:",
        "iam:",
        "cognito",
        "bronze/",
        "silver/",
        "dynamodb:Scan",
        "dynamodb:CreateTable",
    ):
        assert forbidden not in flat
    by_sid = {s["Sid"]: s for s in statements(api_policy)}
    assert by_sid["ListGoldPrefixOnly"]["Condition"] == {
        "StringLike": {"s3:prefix": ["gold/eirgrid/daily_demand/*"]}
    }
    assert by_sid["ReadGoldObjectsOnly"]["Resource"] == [
        f"arn:aws:s3:::{BUCKET}/gold/eirgrid/daily_demand/*"
    ]
    assert by_sid["RunHistoryTable"]["Resource"] == [TABLE, f"{TABLE}/index/gsi1"]
    assert "StartAndInspectOneStateMachine" not in by_sid
    assert by_sid["ReconcileOwnExecutions"]["Resource"] == [
        f"arn:aws:states:{REGION}:{ACCOUNT}:execution:run-pipeline:*"
    ]


def test_api_policy_adds_start_execution_only_when_explicitly_enabled():
    api_policy = spec.api_task_policy(
        bucket=BUCKET,
        table=TABLE,
        state_machine_arn=MACHINE,
        pipeline_execution_enabled=True,
    )
    by_sid = {s["Sid"]: s for s in statements(api_policy)}
    assert by_sid["StartAndInspectOneStateMachine"]["Action"] == [
        "states:StartExecution"
    ]
    assert by_sid["StartAndInspectOneStateMachine"]["Resource"] == [MACHINE]


def test_state_machine_role_can_only_record_progress_and_invoke_its_stages():
    machine = policies()["machine"]

    assert actions(machine) == {
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "lambda:InvokeFunction",
    }
    invoke = next(s for s in statements(machine) if s["Sid"] == "InvokeStageFunctions")
    assert sorted(invoke["Resource"]) == sorted(FUNCTIONS.values())


def test_stage_role_has_lake_and_one_glue_table_but_no_run_history_or_iam():
    stages = policies()["stages"]

    assert actions(stages) == {
        "s3:GetObject",
        "s3:PutObject",
        "glue:GetTable",
        "glue:CreatePartition",
    }
    flat = json.dumps(stages)
    assert "dynamodb" not in flat and "iam:" not in flat and "states:" not in flat
    glue = next(s for s in statements(stages) if s["Sid"] == "RegisterGoldPartitions")
    assert "table/db/tbl" in glue["Resource"][-1]


def test_table_definition_matches_the_store_layout():
    table = spec.table_definition("runs-table")

    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["DeletionProtectionEnabled"] and table["PointInTimeRecoveryEnabled"]
    assert table["RemovalPolicy"] == "RETAIN"
    assert table["TimeToLiveSpecification"] == {
        "AttributeName": schema.TTL_ATTRIBUTE,
        "Enabled": True,
    }
    assert table["GlobalSecondaryIndexes"][0]["IndexName"] == schema.LIST_INDEX
    declared = {a["AttributeName"] for a in table["AttributeDefinitions"]}
    assert declared == {"pk", "sk", "gsi1pk", "gsi1sk"}


def definition():
    return spec.build_state_machine_definition(
        table="runs-table", stage_function_arns=FUNCTIONS
    )


def reachable(machine):
    seen, todo = set(), [machine["StartAt"]]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        state = machine["States"][name]
        todo += [state["Next"]] if "Next" in state else []
        todo += [c["Next"] for c in state.get("Catch", [])]
    return seen


def test_state_machine_is_closed_and_fully_reachable():
    machine = definition()
    states = machine["States"]

    for state in states.values():
        for target in [state.get("Next")] + [c["Next"] for c in state.get("Catch", [])]:
            assert target is None or target in states
    assert reachable(machine) == set(states)


def test_every_stage_has_timeout_retry_and_catch_and_the_whole_run_is_bounded():
    machine = definition()

    assert machine["TimeoutSeconds"] == schema.STATE_MACHINE_TIMEOUT_SECONDS
    assert schema.LOCK_LEASE_SECONDS > machine["TimeoutSeconds"]
    tasks = {
        n: s
        for n, s in machine["States"].items()
        if s.get("Resource", "").endswith("lambda:invoke")
    }
    assert len(tasks) == len(spec.STAGES)
    for state in tasks.values():
        assert state["TimeoutSeconds"] > 0 and state["Retry"]
        assert state["Catch"][0]["ErrorEquals"] == ["States.ALL"]


def test_failures_end_in_failed_and_release_the_lock_with_the_right_status():
    states = definition()["States"]

    assert states["Bronze"]["Catch"][0]["Next"] == "MarkFailure"
    assert states["GlueRegistration"]["Catch"][0]["Next"] == "MarkPartialFailure"
    assert states["Record_gold"]["Catch"][0]["Next"] == "MarkPartialFailure"
    assert (
        states["MarkFailure"]["Next"]
        == states["MarkPartialFailure"]["Next"]
        == "ReleaseLockOnFailure"
    )
    assert states["ReleaseLockOnFailure"]["Next"] == "Failed"
    assert states["MarkSucceeded"]["Next"] == "ReleaseLockOnSuccess"
    assert states["ReleaseLockOnSuccess"]["Next"] == "Succeeded"
    assert (
        states["Failed"]["Type"] == "Fail" and states["Succeeded"]["Type"] == "Succeed"
    )


def test_progress_writes_use_the_same_guards_as_the_store_and_lock_release_is_owner_only():
    states = definition()["States"]

    assert (
        states["MarkRunning"]["Parameters"]["ConditionExpression"]
        == spec.PROGRESS_CONDITION
    )
    assert (
        states["MarkSucceeded"]["Parameters"]["ConditionExpression"]
        == spec.TERMINAL_CONDITION
    )
    release = states["ReleaseLockOnSuccess"]["Parameters"]
    assert release["ConditionExpression"] == "run_id = :rid"
    assert release["Key"]["pk"] == {"S": schema.LOCK_PK}
    assert states["ReleaseLockOnSuccess"]["Catch"][0]["Next"] == "Succeeded"


def test_workflow_writes_only_fixed_generic_error_text_and_no_secrets():
    states = definition()["States"]
    failure = states["MarkFailure"]["Parameters"]["ExpressionAttributeValues"]

    assert {"S": "A pipeline stage failed."} in failure.values()
    assert {"S": "INTERNAL_ERROR"} in failure.values()
    flat = json.dumps(definition())
    assert (
        "AKIA" not in flat and "secret" not in flat.lower() and "arn:aws:s3" not in flat
    )


def test_definition_requires_every_stage_function():
    with pytest.raises(ValueError):
        spec.build_state_machine_definition(
            table="t", stage_function_arns={"bronze": "arn"}
        )
