"""Synthesised CloudFormation for the pipeline stage Lambda functions (nothing is invoked)."""

import fnmatch
import json
from pathlib import Path

import pytest

from tests.test_energy_edge_infrastructure import ACCOUNT  # noqa: F401  (also puts infrastructure/ on sys.path)

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

from energy_config import (  # noqa: E402
    BRONZE_PREFIX,
    DATA_LAKE_BUCKET,
    GOLD_PREFIX,
    PIPELINE_LAMBDA_MAX_TIMEOUT_SECONDS,
    PIPELINE_STAGES,
    SILVER_PREFIX,
)
from stacks.energy_pipeline_stack import EnergyPipelineStack  # noqa: E402
from tests import test_pipeline_stages as handler_tests  # noqa: E402
from tests.test_energy_api_service_infrastructure import ENV, as_list, policy_for, synth  # noqa: E402

import src.pipeline_stages as stage_module  # noqa: E402

BUCKET_ARN = f"arn:aws:s3:::{DATA_LAKE_BUCKET}"
GLUE = "arn:aws:glue:eu-west-1:851725545966"
LABELS = {"bronze": "Bronze", "silver": "Silver", "gold": "Gold", "glue_registration": "GlueRegistration"}
EXPECTED = {
    "bronze": [("s3:PutObject", [f"{BUCKET_ARN}/{BRONZE_PREFIX}*"])],
    "silver": [
        ("s3:GetObject", [f"{BUCKET_ARN}/{BRONZE_PREFIX}*"]),
        ("s3:PutObject", [f"{BUCKET_ARN}/{SILVER_PREFIX}*"]),
    ],
    "gold": [
        ("s3:GetObject", [f"{BUCKET_ARN}/{SILVER_PREFIX}*"]),
        ("s3:PutObject", [f"{BUCKET_ARN}/{GOLD_PREFIX}*"]),
    ],
    "glue_registration": [
        (
            "glue:GetTable",
            [f"{GLUE}:catalog", f"{GLUE}:database/irish_energy_analytics",
             f"{GLUE}:table/irish_energy_analytics/daily_electricity_demand"],
        ),
        (
            "glue:CreatePartition",
            [f"{GLUE}:catalog", f"{GLUE}:database/irish_energy_analytics",
             f"{GLUE}:table/irish_energy_analytics/daily_electricity_demand"],
        ),
    ],
}


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return Template.from_stack(EnergyPipelineStack(app, "IrishEnergyPipelineStack", env=ENV))


def of(template, kind):
    return template.find_resources(kind)


def function(template, stage):
    return next(
        v["Properties"] for n, v in of(template, "AWS::Lambda::Function").items() if n.startswith(LABELS[stage] + "Function")
    )


def statements(template, stage):
    return policy_for(template, LABELS[stage] + "RoleDefaultPolicy")


def test_stack_contains_only_logs_roles_policies_and_four_functions(template):
    kinds = sorted(v["Type"] for v in template.to_json()["Resources"].values())

    assert kinds == sorted(["AWS::Lambda::Function"] * 4 + ["AWS::IAM::Role"] * 4 + ["AWS::IAM::Policy"] * 4
                           + ["AWS::Logs::LogGroup"] * 4)


def test_four_functions_use_one_shared_image_with_their_own_handlers(template):
    props = [function(template, s.key) for s in PIPELINE_STAGES]

    assert len(props) == 4
    assert len({json.dumps(p["Code"], sort_keys=True) for p in props}) == 1  # one image, not four
    for stage, p in zip(PIPELINE_STAGES, props):
        assert p["PackageType"] == "Image"
        assert p["ImageConfig"] == {"Command": [stage.handler]}
        assert "Runtime" not in p and "Handler" not in p
    assert [p["FunctionName"] for p in props] == [
        "irish-energy-pipeline-bronze", "irish-energy-pipeline-silver",
        "irish-energy-pipeline-gold", "irish-energy-pipeline-glue-registration",
    ]


def test_image_is_a_local_amd64_docker_asset_built_from_the_lambda_dockerfile():
    app = cdk.App()
    EnergyPipelineStack(app, "IrishEnergyPipelineStack", env=ENV)
    assembly = app.synth()
    manifest = json.loads(Path(f"{assembly.directory}/IrishEnergyPipelineStack.assets.json").read_text())

    assert len(manifest["dockerImages"]) == 1  # one image shared by all four functions
    source = next(iter(manifest["dockerImages"].values()))["source"]
    assert source["dockerFile"] == "Dockerfile.lambda"
    assert source["platform"] == "linux/amd64"


def test_x86_64_timeouts_memory_and_ephemeral_storage_are_valid(template):
    for stage in PIPELINE_STAGES:
        p = function(template, stage.key)
        assert p["Architectures"] == ["x86_64"]
        assert p["Timeout"] == stage.timeout_seconds
        assert 1 <= p["Timeout"] <= PIPELINE_LAMBDA_MAX_TIMEOUT_SECONDS == 900
        assert p["Timeout"] <= 600  # the Phase 6 state machine allows each task 600 seconds
        assert 128 <= p["MemorySize"] <= 10240 and p["MemorySize"] == stage.memory_mib
        assert 512 <= p["EphemeralStorage"]["Size"] <= 10240


def test_stages_are_sized_individually(template):
    sizes = {s.key: (function(template, s.key)["MemorySize"], function(template, s.key)["Timeout"]) for s in PIPELINE_STAGES}

    assert len(set(sizes.values())) == 3  # silver and gold run the same pandas/Parquet workload
    assert sizes["bronze"][0] < sizes["silver"][0] == sizes["gold"][0]
    assert sizes["glue_registration"][1] < sizes["bronze"][1] < sizes["silver"][1]


def test_each_function_logs_to_its_own_group_with_30_day_retention(template):
    groups = of(template, "AWS::Logs::LogGroup")

    assert len(groups) == 4
    for stage in PIPELINE_STAGES:
        p = function(template, stage.key)
        ref = p["LoggingConfig"]["LogGroup"]["Ref"]
        assert groups[ref]["Properties"]["RetentionInDays"] == 30
        assert groups[ref]["Properties"]["LogGroupName"] == f"/aws/lambda/{p['FunctionName']}"


def test_environment_is_only_the_bucket_name_and_no_secrets_or_execution_switch(template):
    for stage in PIPELINE_STAGES:
        assert function(template, stage.key)["Environment"] == {"Variables": {"S3_BUCKET": DATA_LAKE_BUCKET}}


def test_each_function_has_its_own_role_trusted_by_lambda_for_this_account_only(template):
    roles = of(template, "AWS::IAM::Role")

    assert len(roles) == 4
    assigned = [function(template, s.key)["Role"]["Fn::GetAtt"][0] for s in PIPELINE_STAGES]
    assert len(set(assigned)) == 4 and set(assigned) == set(roles)
    for role in roles.values():
        props = role["Properties"]
        assert "ManagedPolicyArns" not in props and "Policies" not in props
        assert props["AssumeRolePolicyDocument"]["Statement"] == [{
            "Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Condition": {"StringEquals": {"aws:SourceAccount": "851725545966"}},
        }]


@pytest.mark.parametrize("stage", [s.key for s in PIPELINE_STAGES])
def test_exact_permissions_per_stage(template, stage):
    logs, *rest = statements(template, stage)

    assert logs["Action"] == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert list(logs["Resource"]) == ["Fn::GetAtt"] and logs["Resource"]["Fn::GetAtt"][0].startswith(LABELS[stage] + "Logs")
    found = [(a, as_list(s["Resource"])) for s in rest for a in as_list(s["Action"])]
    assert found == EXPECTED[stage]
    assert all(s["Effect"] == "Allow" and not {"Condition", "NotAction", "NotResource", "Principal"} & set(s) for s in rest)


def test_no_unnecessary_service_permissions_or_wildcards(template):
    for stage in PIPELINE_STAGES:
        for statement in statements(template, stage.key):
            for action in as_list(statement["Action"]):
                assert action.split(":")[0] in {"logs", "s3", "glue"}
                assert not action.endswith("*") and action not in {"s3:ListBucket", "s3:DeleteObject", "s3:PutObjectAcl"}
            for resource in as_list(statement["Resource"]):
                assert resource != "*"
                assert not isinstance(resource, str) or not resource.endswith(":bucket") and resource != BUCKET_ARN
                if isinstance(resource, str) and resource.startswith("arn:aws:s3"):
                    assert resource.startswith(BUCKET_ARN + "/") and resource.endswith("/*") and resource.count("/") >= 3
    text = json.dumps(template.to_json())
    for forbidden in ("dynamodb:", "athena:", "cognito", "iam:", "ec2:", "states:", "kms:", "lambda:", "ecr:", "sts:Get"):
        assert forbidden not in text.replace("sts:AssumeRole", "")


def test_stages_cannot_write_outside_their_own_layer(template):
    puts = {s: [r for st in statements(template, s) if "s3:PutObject" in as_list(st["Action"]) for r in as_list(st["Resource"])]
            for s in LABELS}

    assert puts == {
        "bronze": [f"{BUCKET_ARN}/{BRONZE_PREFIX}*"],
        "silver": [f"{BUCKET_ARN}/{SILVER_PREFIX}*"],
        "gold": [f"{BUCKET_ARN}/{GOLD_PREFIX}*"],
        "glue_registration": [],
    }


def test_permissions_cover_exactly_what_the_handlers_do(template, monkeypatch):
    """Run each handler against recording doubles and match every call to its own role's policy."""
    from unittest.mock import patch

    monkeypatch.setenv("S3_BUCKET", DATA_LAKE_BUCKET)
    calls: list[tuple] = []
    s3, glue = handler_tests.FakeS3(calls), handler_tests.FakeGlue(calls)
    by_stage: dict[str, list[tuple]] = {}
    state = {"run_id": handler_tests.RUN_ID, "days_back": 2}
    handlers = {
        "bronze": stage_module.ingest_handler, "silver": stage_module.silver_handler,
        "gold": stage_module.gold_handler, "glue_registration": stage_module.glue_handler,
    }
    with patch("boto3.client", side_effect=lambda service, **_: {"s3": s3, "glue": glue}[service]), \
            patch.object(stage_module, "fetch_demand_data", return_value=handler_tests.PAYLOAD):
        for stage, handler in handlers.items():
            start = len(calls)
            state[stage] = {"output": handler(dict(state))}
            by_stage[stage] = calls[start:]

    for stage, made in by_stage.items():
        allowed = [(a, r) for s in statements(template, stage)[1:] for a in as_list(s["Action"]) for r in as_list(s["Resource"])]
        assert made, stage
        for action, first, second in made:
            if action.startswith("s3"):
                arn = f"arn:aws:s3:::{first}/{second}"
            else:
                arn = f"{GLUE}:table/{first}/{second}"
            assert any(a == action and fnmatch.fnmatchcase(arn, r) for a, r in allowed), (stage, action, arn)
        used = {c[0] for c in made}
        granted = {a for a, _ in allowed}
        assert used == granted, (stage, used, granted)  # nothing unused is granted


def test_function_arns_are_exported_for_the_state_machine_phase(template):
    outputs = template.to_json()["Outputs"]

    assert len(outputs) == 4
    for stage in PIPELINE_STAGES:
        output = outputs[LABELS[stage.key] + "FunctionArn"]
        assert output["Export"]["Name"] == f"IrishEnergyPipeline-{LABELS[stage.key]}FunctionArn"
        assert output["Value"]["Fn::GetAtt"][0].startswith(LABELS[stage.key] + "Function")
        assert output["Value"]["Fn::GetAtt"][1] == "Arn"


def test_every_configured_handler_resolves_to_a_real_function():
    for stage in PIPELINE_STAGES:
        module, _, name = stage.handler.rpartition(".")
        assert module == "src.pipeline_stages" and callable(getattr(stage_module, name))


def test_no_step_functions_resources_or_invocation_wiring(template):
    text = json.dumps(template.to_json())

    assert "AWS::StepFunctions" not in text and "AWS::Lambda::Permission" not in text
    assert "EventSourceMapping" not in text and "AWS::Events" not in text


def test_ecs_api_role_gains_nothing_and_has_no_pipeline_permissions():
    edge = synth()[1]
    sids = [s["Sid"] for s in policy_for(edge, "ApiServiceTaskRole")]

    assert sids == ["ListGoldPrefixOnly", "ReadGoldObjectsOnly", "RunHistoryItems", "RunHistoryListIndex"]
    text = json.dumps(edge.to_json())
    assert "lambda:" not in text and "states:" not in text and "irish-energy-pipeline" not in text


def test_api_execution_stays_disabled_and_operator_post_is_503(auth_app, tmp_path, monkeypatch):
    from src import api
    from src.run_store import RunStore
    from tests import auth_helpers as h

    monkeypatch.setattr(api, "_store", RunStore(tmp_path / "runs.sqlite3"))
    response = auth_app.post("/api/runs", json={"days_back": 2}, headers=h.auth_header(h.token(groups=["operator"])))

    assert response.status_code == 503
    assert response.json() == {"detail": "Pipeline execution is disabled."}
    assert api._store.list_runs() == []


def test_staged_image_context_has_only_the_lambda_requirements_and_source():
    import os

    app = cdk.App()
    EnergyPipelineStack(app, "IrishEnergyPipelineStack", env=ENV)
    directory = app.synth().directory
    asset = next(d for d in os.listdir(directory) if d.startswith("asset."))
    top = sorted(os.listdir(f"{directory}/{asset}"))

    assert top == [".dockerignore", "Dockerfile.lambda", "requirements-lambda.txt", "src"]


def test_the_api_image_build_context_is_unchanged_by_the_lambda_files():
    root = Path(__file__).resolve().parents[1]
    api_ignore = (root / ".dockerignore").read_text()

    assert "requirements-lambda" not in api_ignore
    assert (root / "Dockerfile.lambda.dockerignore").read_text().count("!") == 2
