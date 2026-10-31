"""IAM, configuration and logging boundaries of the whole synthesised app, as one regression check."""

import json
import re

import pytest

from src.settings import SettingsError, load_settings
from tests.pipeline_harness import (
    DATA_LAKE_BUCKET,
    STATE_MACHINE_ARN,
    make_pipeline_fixture,
)
from tests.test_energy_edge_infrastructure import API_CERT, FRONTEND_CERT, run_app
from tests.test_energy_pipeline_infrastructure import EXPECTED

pipeline = make_pipeline_fixture()
CTX = {
    "apiCertificateArn": API_CERT,
    "frontendCertificateArn": FRONTEND_CERT,
    "apiImageTag": "abc1234",
    "cognitoUserPoolId": "eu-west-1_ABCDEFGHI",
    "cognitoAppClientId": "1234567890abcdefghijklmnop",
    "reconcilerEnabled": "true",
}
ACCOUNT_BUCKET = f"arn:aws:s3:::{DATA_LAKE_BUCKET}"
LABELS = {
    "bronze": "Bronze",
    "silver": "Silver",
    "gold": "Gold",
    "glue_registration": "GlueRegistration",
}
ALLOWED_DYNAMODB = {
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Query",
}
CHECKED = ("ApiRegistry", "RunHistory", "Pipeline", "Workflow", "ApiEdge", "Reconciler")


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    out = tmp_path_factory.mktemp("synth")
    result, names = run_app(out, CTX)
    assert result.returncode == 0, result.stderr
    return {
        n.split(".")[0].removeprefix("IrishEnergy").removesuffix("Stack"): json.loads(
            (out / n).read_text()
        )
        for n in names
    }


def as_list(value):
    return value if isinstance(value, list) else [value]


def policies(template):
    """(role logical ids, statements) for every identity policy in a template."""
    found = []
    for resource in template["Resources"].values():
        props = resource["Properties"]
        if resource["Type"] == "AWS::IAM::Policy":
            found.append(
                (
                    [r["Ref"] for r in props["Roles"]],
                    props["PolicyDocument"]["Statement"],
                )
            )
        elif resource["Type"] == "AWS::IAM::ManagedPolicy":
            found.append(([], props["PolicyDocument"]["Statement"]))
        elif resource["Type"] == "AWS::IAM::Role":
            found += [
                ([], p["PolicyDocument"]["Statement"])
                for p in props.get("Policies", [])
            ]
    return found


def all_statements(templates, stacks=CHECKED):
    return [
        s
        for stack in stacks
        for _, statements in policies(templates[stack])
        for s in statements
    ]


def role_statements(template, prefix):
    (statements,) = (
        s for roles, s in policies(template) if any(r.startswith(prefix) for r in roles)
    )
    return statements


def pairs(statements):
    return {
        (a, json.dumps(r, sort_keys=True))
        for s in statements
        for a in as_list(s["Action"])
        for r in as_list(s["Resource"])
    }


# -- roles stay separate and minimal ---------------------------------------------------------


def test_the_four_lambda_roles_are_separate_and_unchanged(templates):
    pipeline_template = templates["Pipeline"]
    roles = {
        n
        for n, r in pipeline_template["Resources"].items()
        if r["Type"] == "AWS::IAM::Role"
    }

    assert (
        len(roles) == 4 and len({tuple(r) for r, _ in policies(pipeline_template)}) == 4
    )
    for stage, label in LABELS.items():
        statements = [
            s
            for s in role_statements(pipeline_template, label + "Role")
            if s["Sid"] != "WriteOwnLogs"
        ]
        expected = {(a, json.dumps(r, sort_keys=True)) for a, r in EXPECTED[stage]}
        found = {
            (a, json.dumps(as_list(s["Resource"]), sort_keys=True))
            for s in statements
            for a in as_list(s["Action"])
        }
        assert found == expected, stage


def test_the_state_machine_role_can_only_invoke_the_four_stages_and_record_progress(
    templates,
):
    (statements,) = (s for roles, s in policies(templates["Workflow"]))

    assert {a for s in statements for a in as_list(s["Action"])} == {
        "lambda:InvokeFunction",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    }
    invoke = next(s for s in statements if s["Sid"] == "InvokeStageFunctions")
    assert len(as_list(invoke["Resource"])) == 4
    assert [s["Sid"] for s in statements] == ["RecordProgress", "InvokeStageFunctions"]


def test_the_api_task_role_can_start_and_describe_only_this_state_machine(templates):
    statements = role_statements(templates["ApiEdge"], "ApiServiceTaskRole")
    states = {
        s["Sid"]: s
        for s in statements
        if any(a.startswith("states:") for a in as_list(s["Action"]))
    }

    assert {sid: (s["Action"], s["Resource"]) for sid, s in states.items()} == {
        "ReconcileOwnExecutions": (
            "states:DescribeExecution",
            STATE_MACHINE_ARN.replace(":stateMachine:", ":execution:") + ":*",
        ),
    }
    assert [s["Sid"] for s in statements] == [
        "ListGoldPrefixOnly",
        "ReadGoldObjectsOnly",
        "RunHistoryItems",
        "RunHistoryListIndex",
        "ReconcileOwnExecutions",
    ]


def test_only_the_workflow_and_the_api_can_start_executions_and_the_reconciler_can_only_describe(
    templates,
):
    starters = {
        stack
        for stack in CHECKED
        for _, ss in policies(templates[stack])
        for s in ss
        if any("StartExecution" in a for a in as_list(s["Action"]))
    }
    reconciler = [s for _, ss in policies(templates["Reconciler"]) for s in ss]

    assert (
        starters == set()
    )  # API execution is disabled by default  # the workflow stack's role only invokes Lambda and writes DynamoDB
    assert {
        a for s in reconciler for a in as_list(s["Action"]) if a.startswith("states:")
    } == {"states:DescribeExecution"}


def test_no_role_other_than_the_workflow_and_the_api_can_use_step_functions_or_lambda(
    templates,
):
    for stack in ("Pipeline", "RunHistory", "ApiRegistry"):
        text = json.dumps([s for _, ss in policies(templates[stack]) for s in ss])
        assert "states:" not in text and "lambda:" not in text


# -- data-plane boundaries across every stack -------------------------------------------------


def test_dynamodb_actions_are_item_operations_on_the_run_history_table_and_its_index_only(
    templates,
):
    table = "IrishEnergyRunHistoryStack:ExportsOutputFnGetAttRunHistoryTableFD4DA44DArn6CF8D0FC"
    seen = set()
    for statement in all_statements(templates):
        dynamo = [a for a in as_list(statement["Action"]) if a.startswith("dynamodb:")]
        if not dynamo:
            continue
        assert set(dynamo) <= ALLOWED_DYNAMODB
        text = json.dumps(statement["Resource"])
        assert (
            table in text and "*" not in text
        )  # the table or its gsi1 index, never anything else
        seen |= set(dynamo)
    assert seen == ALLOWED_DYNAMODB


def test_s3_access_is_limited_to_the_existing_layer_prefixes(templates):
    allowed = {
        "gold/eirgrid/daily_demand/",
        "bronze/eirgrid/demand_actual/",
        "silver/eirgrid/demand_actual/",
    }
    for statement in all_statements(templates):
        for action in (a for a in as_list(statement["Action"]) if a.startswith("s3:")):
            for resource in as_list(statement["Resource"]):
                if resource == ACCOUNT_BUCKET:
                    assert action == "s3:ListBucket" and "s3:prefix" in json.dumps(
                        statement["Condition"]
                    )
                else:
                    assert any(
                        resource == f"{ACCOUNT_BUCKET}/{prefix}*" for prefix in allowed
                    ), resource
                    assert action in {"s3:GetObject", "s3:PutObject"}


def test_glue_access_exists_only_for_registration_and_only_for_one_table(templates):
    holders = [
        (roles, s)
        for stack in CHECKED
        for roles, ss in policies(templates[stack])
        for s in ss
        if any(a.startswith("glue:") for a in as_list(s["Action"]))
    ]

    assert len(holders) == 1 and holders[0][0][0].startswith("GlueRegistrationRole")
    assert sorted(as_list(holders[0][1]["Action"])) == [
        "glue:CreatePartition",
        "glue:GetTable",
    ]
    assert [r.rsplit(":", 1)[-1] for r in holders[0][1]["Resource"]] == [
        "catalog",
        "database/irish_energy_analytics",
        "table/irish_energy_analytics/daily_electricity_demand",
    ]


def test_no_scan_batch_transaction_table_management_or_admin_permission_exists_anywhere(
    templates,
):
    forbidden = re.compile(
        r"^(dynamodb:(Scan|Batch|Transact|Create|Delete(Table|Backup)|Update(Table|Time)|Describe|List|Restore|Import|Export"
        r"|ConditionCheck)|iam:|cognito|ec2:(?!Create|Describe)|sts:(?!AssumeRole)|athena:|kms:|secretsmanager:|"
        r"states:(?!(StartExecution|DescribeExecution)$)|lambda:(?!InvokeFunction$))"
    )
    for statement in all_statements(templates):
        for action in as_list(statement["Action"]):
            assert "*" not in action and not forbidden.match(action), action


def test_the_only_wildcard_resource_is_the_ecr_authorisation_token(templates):
    for statement in all_statements(templates):
        if "*" in as_list(statement["Resource"]):
            assert as_list(statement["Action"]) == ["ecr:GetAuthorizationToken"]


def test_both_gateway_endpoints_are_unchanged(templates):
    endpoints = {
        k: v["Properties"]
        for k, v in templates["ApiEdge"]["Resources"].items()
        if v["Type"] == "AWS::EC2::VPCEndpoint"
    }
    dynamo = next(p for k, p in endpoints.items() if k.startswith("VpcDynamoDb"))
    s3 = next(p for k, p in endpoints.items() if k.startswith("VpcS3"))

    assert "PolicyDocument" not in s3 and len(endpoints) == 2
    assert [(s["Sid"], s["Action"]) for s in dynamo["PolicyDocument"]["Statement"]] == [
        (
            "RunHistoryItemOperations",
            [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
            ],
        ),
        ("RunHistoryListIndexQuery", "dynamodb:Query"),
    ]


# -- no credentials anywhere ----------------------------------------------------------------


def test_no_template_carries_credential_like_environment_or_values(templates):
    text = json.dumps(templates)

    assert not re.search(r"(AKIA|ASIA)[0-9A-Z]{16}", text)
    for template in templates.values():
        for resource in template["Resources"].values():
            props = resource["Properties"]
            names = [
                e["Name"]
                for c in props.get("ContainerDefinitions", [])
                for e in c.get("Environment", [])
            ]
            names += list(props.get("Environment", {}).get("Variables", {}))
            assert not [
                n
                for n in names
                if re.search(r"SECRET|PASSWORD|ACCESS_KEY|SESSION_TOKEN", n)
            ]


def test_credentials_in_the_environment_never_reach_logs_output_run_records_or_objects(
    pipeline, monkeypatch, caplog, capfd
):
    canaries = {
        "AWS_SECRET_ACCESS_KEY": "canarySecretValue123",
        "AWS_SESSION_TOKEN": "canarySessionToken456",
        "AWS_ACCESS_KEY_ID": "AKIACANARYCANARYCANA",
    }
    for key, value in canaries.items():
        monkeypatch.setenv(key, value)
    import logging

    caplog.set_level(logging.DEBUG)
    good, bad = pipeline.new_run(), None
    pipeline.execute(good)
    bad = pipeline.new_run()
    pipeline.fail("gold", RuntimeError(f"boom {canaries['AWS_SECRET_ACCESS_KEY']}"))
    pipeline.execute(bad)

    seen = (
        caplog.text
        + "".join(capfd.readouterr())
        + json.dumps({str(k): v for k, v in pipeline.raw_items().items()}, default=str)
    )
    seen += "".join(
        pipeline.s3.read(key).decode("latin-1") for key in pipeline.s3.object_keys()
    )
    for value in canaries.values():
        assert value not in seen


# -- startup and execution stay safe -----------------------------------------------------------

PRODUCTION = {
    "API_ENV": "production",
    "COGNITO_REGION": "eu-west-1",
    "COGNITO_USER_POOL_ID": "eu-west-1_TestPool1",
    "COGNITO_APP_CLIENT_ID": "testclientid1234567890",
    "CORS_ALLOWED_ORIGINS": "https://energy.example.com",
    "RUN_HISTORY_BACKEND": "dynamodb",
    "RUN_HISTORY_TABLE": "irish-energy-run-history",
    "PIPELINE_STATE_MACHINE_ARN": STATE_MACHINE_ARN,
}


def test_the_complete_production_configuration_loads_with_execution_disabled():
    settings = load_settings(PRODUCTION)

    assert (
        settings.pipeline_execution_enabled is False
        and settings.pipeline_state_machine_arn == STATE_MACHINE_ARN
    )


def test_complete_production_configuration_can_explicitly_enable_execution():
    settings = load_settings({**PRODUCTION, "PIPELINE_EXECUTION_ENABLED": "true"})

    assert settings.pipeline_execution_enabled is True
    assert settings.pipeline_state_machine_arn == STATE_MACHINE_ARN


@pytest.mark.parametrize(
    "broken",
    [
        {"COGNITO_REGION": ""},
        {"COGNITO_USER_POOL_ID": ""},
        {"COGNITO_APP_CLIENT_ID": ""},
        {"RUN_HISTORY_BACKEND": "sqlite"},
        {"RUN_HISTORY_BACKEND": ""},
        {"RUN_HISTORY_TABLE": ""},
        {"ENABLE_API_DOCS": "true"},
        {"CORS_ALLOWED_ORIGINS": "http://energy.example.com"},
        {"PIPELINE_STATE_MACHINE_ARN": "arn:aws:states:bad"},
    ],
)
def test_production_refuses_incomplete_or_unsafe_configuration(broken):
    with pytest.raises(SettingsError):
        load_settings({**PRODUCTION, **broken})
