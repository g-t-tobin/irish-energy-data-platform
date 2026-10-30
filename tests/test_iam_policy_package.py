"""Phase 9.7.2: the administrator IAM policy package is valid, narrow and consistent with the CDK templates."""

import json
import re
from pathlib import Path

import botocore.session
import pytest

from tests.test_energy_api_service_infrastructure import SERVICE_CTX
from tests.test_energy_edge_infrastructure import API_CERT, FRONTEND_CERT, run_app

IAM_DIR = Path(__file__).resolve().parents[1] / "docs" / "iam"
ACCOUNT = "851725545966"
POLICIES = sorted(p for p in IAM_DIR.glob("*.json") if p.name != "deployer-role-trust-policy.json")
NAMES = {p.name for p in POLICIES}
EXPECTED = {
    "deployment-identity-policy.json",
    "cloudformation-execution-policy.json",
    "ecr-publishing-policy.json",
    "lambda-deployment-policy.json",
    "ecs-deployment-policy.json",
    "network-deployment-policy.json",
    "passrole-policy.json",
    "read-only-verification-policy.json",
    "rollback-policy.json",
    "runtime-permissions-boundary.json",
    "runtime-permissions-boundary-execution-activation.json",
    "iam-role-management-policy.json",
    "frontend-publishing-policy.json",
    "frontend-stack-update-policy.json",
    "iam-trust-update-policy.json",
    "dns-inspection-policy.json",
    "cloudformation-diagnostic-policy.json",
    "cognito-inspection-policy.json",
    "deployment-permissions-boundary.json",
    "operator-user-policy.json",
}
# (file, Sid) pairs that may use Resource "*" because the service has no resource-level support
WILDCARD_RESOURCE_ALLOWED = {
    ("deployment-identity-policy.json", "ShowCallerIdentity"),
    ("ecr-publishing-policy.json", "RegistryLoginTokenNoResourceLevelSupport"),
    ("ecs-deployment-policy.json", "EcsRegisterAndDescribeTaskDefinitionsNoResourceLevelSupport"),
    ("lambda-deployment-policy.json", "DescribeLogGroupsAccountWideReadOnly"),
    ("network-deployment-policy.json", "Ec2DescribeNoResourceLevelSupport"),
    ("network-deployment-policy.json", "ElbDescribeNoResourceLevelSupport"),
    ("runtime-permissions-boundary.json", "EcsToken"),
    ("runtime-permissions-boundary.json", "DenyStartExecution"),
    ("runtime-permissions-boundary-execution-activation.json", "EcsToken"),
    ("runtime-permissions-boundary-execution-activation.json", "DenyStartExceptApi"),
    ("deployment-permissions-boundary.json", "AllowProjectDeploymentActions"),
}
BOUNDARIES = {"runtime-permissions-boundary.json", "runtime-permissions-boundary-execution-activation.json", "deployment-permissions-boundary.json"}
FRONTEND = "frontend-publishing-policy.json"
READ_ONLY = "read-only-verification-policy.json"
SERVICE_MODELS = {
    "states": "stepfunctions",
    "elasticloadbalancing": "elbv2",
    "servicequotas": "service-quotas",
}
# IAM action names that are not API operation names
NOT_OPERATIONS = {
    "iam:PassRole", "lambda:InvokeFunction", "s3:ListBucket", "s3:PutEncryptionConfiguration", "s3:GetEncryptionConfiguration",
    "s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock", "sts:TagSession", "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource",
}
FORBIDDEN_ACTIONS = re.compile(
    r"^(states:Start\w*|lambda:Invoke\w*|s3:(Get|Put|Delete)Object\w*|dynamodb:(Get|Put|Update|Delete)Item|dynamodb:(Query|Scan)|"
    r"secretsmanager:\w+|iam:(CreateUser|CreateAccessKey|CreateLoginProfile|AttachRolePolicy|AttachUserPolicy|PutUserPolicy|CreatePolicy\w*)|"
    r"cognito-idp:(Admin|Create|Update|Delete|Set)\w*|route53:Change\w*|acm:(Request|Import|Delete)\w*|kms:\w+)$"
)


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def statements(path: Path) -> list[dict]:
    return load(path)["Statement"]


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def all_actions(path: Path) -> list[str]:
    return [a for s in statements(path) for a in as_list(s["Action"])]


def test_the_expected_policy_documents_exist():
    assert NAMES == EXPECTED
    for name in ("README.md",):
        assert (IAM_DIR / name).exists()
    assert (IAM_DIR.parent / "phase-9.7.2-administrator-preparation.md").exists()


@pytest.mark.parametrize("path", POLICIES, ids=lambda p: p.name)
def test_policy_structure_and_size(path):
    document = load(path)
    text = path.read_text()

    assert document["Version"] == "2012-10-17" and set(document) == {"Version", "Statement"}
    assert len(re.sub(r"\s", "", text)) <= 6144  # managed policy limit
    sids = [s["Sid"] for s in document["Statement"]]
    assert len(sids) == len(set(sids)) and all(re.fullmatch(r"[A-Za-z0-9]+", s) for s in sids)
    for s in document["Statement"]:
        assert s["Effect"] in {"Allow", "Deny"} and as_list(s["Action"]) and as_list(s["Resource"])
        assert "NotAction" not in s and "NotResource" not in s and "Principal" not in s


@pytest.mark.parametrize("path", POLICIES, ids=lambda p: p.name)
def test_no_wildcard_actions_and_no_unjustified_wildcard_resources(path):
    for s in statements(path):
        assert all("*" not in action for action in as_list(s["Action"])), s["Sid"]
        if "*" in as_list(s["Resource"]) or any(r == "*" for r in as_list(s["Resource"])):
            allowed = (path.name, s["Sid"]) in WILDCARD_RESOURCE_ALLOWED or (
                path.name == READ_ONLY and not any(re.search(r"(Create|Put|Update|Delete)", a) for a in as_list(s["Action"]))
            )
            assert allowed, f"{path.name}:{s['Sid']} uses Resource *"


@pytest.mark.parametrize("path", POLICIES, ids=lambda p: p.name)
def test_only_the_qualifier_placeholder_remains_and_scope_is_the_project_account(path):
    text = path.read_text()

    assert not re.search(r"<[A-Za-z_ -]+>|TODO|FIXME|example|123456789012", text)
    assert set(re.findall(r"REPLACE_[A-Z_]+", text)) <= {"REPLACE_SITE_BUCKET", "REPLACE_DISTRIBUTION_ID", "REPLACE_OAC_ID", "REPLACE_RESPONSE_HEADERS_POLICY_ID",
        "REPLACE_USER_POOL_ID", "REPLACE_HOSTED_ZONE_ID", "REPLACE_API_CERT_ID", "REPLACE_FRONTEND_CERT_ID",
    }
    assert set(re.findall(r"arn:aws:[a-z0-9-]+:([a-z0-9-]*):(\d*):", text)) <= {
        ("eu-west-1", ACCOUNT), ("us-east-1", ACCOUNT), ("", ACCOUNT), ("", "")
    }


@pytest.mark.parametrize("path", POLICIES, ids=lambda p: p.name)
def test_no_data_plane_secret_or_privilege_escalation_actions(path):
    if path.name in BOUNDARIES:
        return  # a boundary is a runtime ceiling: tests/test_iam_boundary.py checks it
    for action in all_actions(path):
        if path.name == FRONTEND and action in {"s3:PutObject", "s3:DeleteObject"}:
            continue  # the optional publisher, limited to the one site bucket by test_iam_boundary.py
        if any(s["Effect"] == "Deny" and action in as_list(s["Action"]) for s in statements(path)):
            continue
        assert not FORBIDDEN_ACTIONS.match(action), f"{path.name}: {action}"


def test_nothing_grants_start_execution_or_invocation_to_any_identity():
    identity = [p for p in POLICIES if p.name not in BOUNDARIES]
    actions = {a for p in identity for a in all_actions(p)}

    assert not {a for a in actions if a.lower().startswith(("states:start", "lambda:invoke"))}
    assert not any("StartExecution" in p.read_text() for p in identity)


def test_assume_role_is_limited_to_the_three_bootstrap_roles():
    allow = [s for s in statements(IAM_DIR / "deployment-identity-policy.json") if "sts:AssumeRole" in as_list(s["Action"])]
    resources = [r for s in allow for r in as_list(s["Resource"])]

    assert len(resources) == 3
    assert all(re.fullmatch(rf"arn:aws:iam::{ACCOUNT}:role/cdk-energydev01-(deploy|file-publishing|image-publishing)-role-{ACCOUNT}-eu-west-1", r) for r in resources)
    assert not any("sts:AssumeRole" in all_actions(p) for p in POLICIES if p.name not in {"deployment-identity-policy.json", "operator-user-policy.json"} and p.name not in BOUNDARIES)


def test_pass_role_exists_only_where_intended_and_is_fully_constrained():
    for path in POLICIES:
        if path.name in BOUNDARIES:
            continue
        for s in statements(path):
            if "iam:PassRole" not in as_list(s["Action"]):
                continue
            assert path.name in {"passrole-policy.json", "deployment-identity-policy.json"}
            assert s["Effect"] == "Allow" and "*" not in as_list(s["Resource"])
            service = s["Condition"]["StringEquals"]["iam:PassedToService"]
            assert isinstance(service, str) and service.endswith(".amazonaws.com")
            for resource in as_list(s["Resource"]):
                assert re.fullmatch(rf"arn:aws:iam::{ACCOUNT}:role/(IrishEnergy[A-Za-z]+Stack-[A-Za-z]+\*|cdk-energydev01-cfn-exec-role-{ACCOUNT}-eu-west-1)", resource), resource
    deployer = statements(IAM_DIR / "deployment-identity-policy.json")
    passes = [s for s in deployer if "iam:PassRole" in as_list(s["Action"])]
    assert len(passes) == 1 and as_list(passes[0]["Resource"]) == [f"arn:aws:iam::{ACCOUNT}:role/cdk-energydev01-cfn-exec-role-{ACCOUNT}-eu-west-1"]
    assert passes[0]["Condition"]["StringEquals"]["iam:PassedToService"] == "cloudformation.amazonaws.com"


def test_stateful_resources_cannot_be_deleted_and_stacks_are_guarded():
    execution = set(all_actions(IAM_DIR / "cloudformation-execution-policy.json"))
    for forbidden in ("dynamodb:DeleteTable", "ecr:DeleteRepository", "s3:DeleteBucket", "logs:DeleteLogGroup", "s3:CreateBucket"):
        assert forbidden not in execution
    assert "ecr:DeleteRepository" not in all_actions(IAM_DIR / "ecr-publishing-policy.json")

    rollback = statements(IAM_DIR / "rollback-policy.json")
    denied = {r for s in rollback if s["Effect"] == "Deny" for r in as_list(s["Resource"])}
    allowed = {r for s in rollback if s["Effect"] == "Allow" for r in as_list(s["Resource"])}
    for stateful in ("IrishEnergyDataLakeStack", "IrishEnergyApiRegistryStack", "IrishEnergyRunHistoryStack"):
        assert any(f"stack/{stateful}/" in r for r in denied)
        assert not any(f"stack/{stateful}/" in r for r in allowed)
    assert any("CDKToolkit" in r for r in denied)
    deployer = statements(IAM_DIR / "deployment-identity-policy.json")
    assert any(s["Effect"] == "Deny" and any("CDKToolkit" in r for r in as_list(s["Resource"])) for s in deployer)


def test_ecr_publishing_reaches_only_the_api_repository():
    resources = {r for s in statements(IAM_DIR / "ecr-publishing-policy.json") for r in as_list(s["Resource"])}

    assert resources == {"*", f"arn:aws:ecr:eu-west-1:{ACCOUNT}:repository/irish-energy-api"}
    assert "ecr:GetAuthorizationToken" in all_actions(IAM_DIR / "ecr-publishing-policy.json")
    assert "ecr:PutImage" in all_actions(IAM_DIR / "ecr-publishing-policy.json")


def test_every_action_is_a_real_operation_of_its_service():
    session = botocore.session.get_session()
    models = {}
    unknown = []
    for action in sorted({a for p in POLICIES for a in all_actions(p)}):
        service, name = action.split(":")
        if action in NOT_OPERATIONS:
            continue
        key = SERVICE_MODELS.get(service, service)
        if key not in models:
            models[key] = set(session.get_service_model(key).operation_names)
        if name not in models[key]:
            unknown.append(action)
    assert unknown == []


def test_resource_names_match_the_synthesized_templates(tmp_path):
    ctx = {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, **SERVICE_CTX, "reconcilerEnabled": "true"}
    result, templates = run_app(tmp_path, ctx)
    assert result.returncode == 0 and len(templates) == 9
    found = {}
    for template in tmp_path.glob("*.template.json"):
        for resource in json.loads(template.read_text())["Resources"].values():
            props = resource.get("Properties", {})
            found.setdefault(resource["Type"], set()).update(
                str(props[key]) for key in ("FunctionName", "TableName", "RepositoryName", "StateMachineName", "Name", "LogGroupName") if key in props and isinstance(props[key], str)
            )
    text = " ".join(p.read_text() for p in POLICIES)

    for name in found["AWS::Lambda::Function"] | found["AWS::DynamoDB::Table"] | found["AWS::ECR::Repository"] | found["AWS::StepFunctions::StateMachine"] | found["AWS::Scheduler::Schedule"]:
        assert name in text, name
    for name in found["AWS::Logs::LogGroup"]:
        assert name in text, name
    assert "irish-energy-analytics" in found["AWS::Athena::WorkGroup"] and "workgroup/irish-energy-analytics" in text
    assert "irish_energy_analytics" in text and "daily_electricity_demand" in text
    for stack in (template.name.split(".")[0] for template in tmp_path.glob("*.template.json")):
        assert stack in text, stack  # every deployed stack is covered by some statement
