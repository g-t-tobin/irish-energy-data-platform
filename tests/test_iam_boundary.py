"""Phase 9.7.2.1: the runtime permissions boundary and the role-creation policies, checked offline."""

import json
import re
from pathlib import Path

import pytest

from tests.test_energy_api_service_infrastructure import SERVICE_CTX
from tests.test_energy_edge_infrastructure import API_CERT, FRONTEND_CERT, run_app

IAM_DIR = Path(__file__).resolve().parents[1] / "docs" / "iam"
ACCOUNT, REGION = "851725545966", "eu-west-1"
BOUNDARY_NAME = "irish-energy-runtime-boundary"
BOUNDARY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_NAME}"
INITIAL = IAM_DIR / "runtime-permissions-boundary.json"
ACTIVATION = IAM_DIR / "runtime-permissions-boundary-execution-activation.json"
ROLE_MGMT = IAM_DIR / "iam-role-management-policy.json"
FRONTEND = IAM_DIR / "frontend-publishing-policy.json"
EXECUTION_POLICIES = [IAM_DIR / n for n in (
    "cloudformation-execution-policy.json", "iam-role-management-policy.json", "lambda-deployment-policy.json",
    "ecs-deployment-policy.json", "network-deployment-policy.json", "passrole-policy.json",
)]
DEPLOYER_POLICIES = [IAM_DIR / n for n in ("deployment-identity-policy.json", "ecr-publishing-policy.json", "rollback-policy.json")]
SERVICE_FOR_PREFIX = {  # stack-and-role prefix -> service principal that must be in its trust policy
    "IrishEnergyPipelineStack-BronzeRole": "lambda.amazonaws.com",
    "IrishEnergyPipelineStack-SilverRole": "lambda.amazonaws.com",
    "IrishEnergyPipelineStack-GoldRole": "lambda.amazonaws.com",
    "IrishEnergyPipelineStack-GlueRegistrationRole": "lambda.amazonaws.com",
    "IrishEnergyReconcilerStack-Role": "lambda.amazonaws.com",
    "IrishEnergyReconcilerStack-SchedulerRole": "scheduler.amazonaws.com",
    "IrishEnergyWorkflowStack-StateMachineRole": "states.amazonaws.com",
    "IrishEnergyApiEdgeStack-ApiServiceExecutionRole": "ecs-tasks.amazonaws.com",
    "IrishEnergyApiEdgeStack-ApiServiceTaskRole": "ecs-tasks.amazonaws.com",
}


def statements(path):
    return json.loads(path.read_text())["Statement"]


def as_list(value):
    return value if isinstance(value, list) else [value]


def pattern(glob):
    return re.compile("^" + re.escape(glob).replace(r"\*", ".*") + "$")


def matches(globs, value):
    return any(pattern(g).match(value) for g in as_list(globs))


def role_patterns(path, sid):
    (statement,) = [s for s in statements(path) if s["Sid"] == sid]
    return as_list(statement["Resource"])


def action_set(path, effect="Allow"):
    return {a for s in statements(path) if s["Effect"] == effect for a in as_list(s["Action"])}


@pytest.fixture(scope="module")
def synthesized(tmp_path_factory):
    out = tmp_path_factory.mktemp("boundary")
    ctx = {
        "apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, **SERVICE_CTX,
        "reconcilerEnabled": "true", "@aws-cdk/core:permissionsBoundary": {"name": BOUNDARY_NAME},
    }
    result, templates = run_app(out, ctx)
    assert result.returncode == 0, result.stderr
    assert len(templates) == 9
    return {t.name.split(".")[0]: json.loads(t.read_text()) for t in out.glob("*.template.json")}


def roles(synthesized):
    for stack, template in synthesized.items():
        for logical, resource in template["Resources"].items():
            if resource["Type"] == "AWS::IAM::Role":
                yield stack, logical, resource


# -- presence and shape of the boundary --------------------------------------------------------------


def test_boundary_documents_exist_and_are_allow_lists_with_the_project_scope():
    for path in (INITIAL, ACTIVATION):
        text = path.read_text()
        allow = [s for s in statements(path) if s["Effect"] == "Allow"]
        assert allow
        assert all("*" not in a for s in allow for a in as_list(s["Action"]))
        for s in allow:
            for resource in as_list(s["Resource"]):
                assert resource == "*" and s["Sid"] == "EcsToken" or resource.startswith(
                    (f"arn:aws:s3:::irish-energy-data-platform-{ACCOUNT}", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:",
                     f"arn:aws:glue:{REGION}:{ACCOUNT}:", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/irish-energy-run-history",
                     f"arn:aws:states:{REGION}:{ACCOUNT}:", f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:irish-energy-",
                     f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/irish-energy-api")), (path.name, resource)
        assert len(re.sub(r"\s", "", text)) <= 6144


def test_boundary_services_are_only_those_the_platform_uses():
    services = {a.split(":")[0] for a in action_set(INITIAL)}

    assert services == {"logs", "s3", "glue", "dynamodb", "states", "lambda", "ecr"}
    assert not any(a.split(":")[0] in {"iam", "sts", "ec2", "kms", "secretsmanager", "ssm", "cloudformation", "cognito-idp"} for a in action_set(INITIAL))


def test_the_initial_boundary_cannot_start_executions_and_the_activation_variant_only_adds_the_api_role():
    initial = {s["Sid"]: s for s in statements(INITIAL)}
    activation = {s["Sid"]: s for s in statements(ACTIVATION)}

    assert "states:StartExecution" not in action_set(INITIAL)
    assert initial["DenyStartExecution"]["Effect"] == "Deny" and "Condition" not in initial["DenyStartExecution"]
    assert set(initial) - set(activation) == {"DenyStartExecution"}
    assert set(activation) - set(initial) == {"ApiStartExecution", "DenyStartExceptApi"}
    allow = activation["ApiStartExecution"]
    assert as_list(allow["Resource"]) == [f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:irish-energy-pipeline"]
    assert as_list(allow["Condition"]["ArnLike"]["aws:PrincipalArn"]) == [f"arn:aws:iam::{ACCOUNT}:role/IrishEnergyApiEdgeStack-ApiServiceTaskRole*"]
    assert activation["DenyStartExceptApi"]["Condition"]["ArnNotLike"]["aws:PrincipalArn"] == allow["Condition"]["ArnLike"]["aws:PrincipalArn"]
    for sid in set(initial) & set(activation):
        assert initial[sid] == activation[sid]


def test_the_reconciler_cannot_start_an_execution_under_either_boundary():
    reconciler = f"arn:aws:iam::{ACCOUNT}:role/IrishEnergyReconcilerStack-Role1ABCC5F0-ABCDEFGHIJKL"
    scheduler = f"arn:aws:iam::{ACCOUNT}:role/IrishEnergyReconcilerStack-SchedulerRole59E73443-ABCDEFGHIJKL"
    for path in (INITIAL, ACTIVATION):
        for principal in (reconciler, scheduler):
            allowed = any(
                "states:StartExecution" in as_list(s["Action"]) and s["Effect"] == "Allow"
                and ("Condition" not in s or matches(s["Condition"]["ArnLike"]["aws:PrincipalArn"], principal))
                for s in statements(path)
            )
            denied = any(
                s["Effect"] == "Deny" and "states:StartExecution" in as_list(s["Action"])
                and ("Condition" not in s or not matches(s["Condition"]["ArnNotLike"]["aws:PrincipalArn"], principal))
                for s in statements(path)
            )
            assert not allowed and denied
    assert all("StartExecution" not in json.dumps(s) for p in EXECUTION_POLICIES + DEPLOYER_POLICIES for s in statements(p))


def test_no_game_resources_or_foreign_names_appear_in_the_boundary_or_the_role_policies():
    for path in (INITIAL, ACTIVATION, ROLE_MGMT, FRONTEND):
        text = path.read_text()
        assert not re.search(r"(?i)bluff|believe|game|\bplay\b|\bapi\.ai-autofy", text)
        for arn in re.findall(r'arn:aws:[a-z0-9-]+:[a-z0-9-]*:(\d*):', text):
            assert arn in {"", ACCOUNT}
    for path in (INITIAL, ACTIVATION):
        wildcard = [s["Sid"] for s in statements(path) if "*" in as_list(s["Resource"])]
        assert set(wildcard) <= {"EcsToken", "DenyStartExecution", "DenyStartExceptApi"}


# -- immutability ------------------------------------------------------------------------------------


def test_the_boundary_cannot_be_changed_or_removed_by_the_execution_role_or_the_deployer():
    protected = {"iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion"}
    detach = {"iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary", "iam:AttachRolePolicy"}
    for paths in (EXECUTION_POLICIES, DEPLOYER_POLICIES):
        denies = [s for p in paths for s in statements(p) if s["Effect"] == "Deny"]
        assert protected <= {a for s in denies if BOUNDARY_ARN in as_list(s["Resource"]) for a in as_list(s["Action"])}
        assert detach <= {a for s in denies if f"arn:aws:iam::{ACCOUNT}:role/*" in as_list(s["Resource"]) for a in as_list(s["Action"])}
        allowed = {a for p in paths for s in statements(p) if s["Effect"] == "Allow" for a in as_list(s["Action"])}
        assert not (allowed & (protected | detach))


def test_nothing_in_the_package_lets_any_identity_write_the_boundary_policy():
    for path in IAM_DIR.glob("*.json"):
        for s in statements(path):
            if s["Effect"] == "Allow":
                assert not (set(as_list(s["Action"])) & {"iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicy"})


# -- role creation and names -------------------------------------------------------------------------


def test_role_management_covers_exactly_the_nine_approved_role_name_patterns():
    expected = {f"arn:aws:iam::{ACCOUNT}:role/{prefix}*" for prefix in SERVICE_FOR_PREFIX}
    for sid in ("CreateNineProjectRolesOnlyWithTheBoundary", "WriteInlinePoliciesOnlyWhenTheBoundaryIsAttached", "ReadTagAndDeleteTheNineProjectRoles"):
        assert set(role_patterns(ROLE_MGMT, sid)) == expected
    passrole = {r for s in statements(IAM_DIR / "passrole-policy.json") for r in as_list(s["Resource"])}
    assert passrole == expected


def test_role_creation_and_inline_policies_require_the_boundary():
    by_sid = {s["Sid"]: s for s in statements(ROLE_MGMT)}
    for sid, actions in (("CreateNineProjectRolesOnlyWithTheBoundary", {"iam:CreateRole"}), ("WriteInlinePoliciesOnlyWhenTheBoundaryIsAttached", {"iam:PutRolePolicy", "iam:DeleteRolePolicy"})):
        assert set(as_list(by_sid[sid]["Action"])) == actions
        assert by_sid[sid]["Condition"] == {"StringEquals": {"iam:PermissionsBoundary": BOUNDARY_ARN}}
    deny = by_sid["DenyCreatingOrWritingAnyRoleWithoutTheBoundary"]
    assert deny["Effect"] == "Deny" and set(as_list(deny["Action"])) == {"iam:CreateRole", "iam:PutRolePolicy"}
    assert deny["Condition"] == {"StringNotEquals": {"iam:PermissionsBoundary": BOUNDARY_ARN}}


def test_only_documented_role_actions_are_allowed_and_no_user_or_policy_creation():
    allowed = action_set(ROLE_MGMT)

    assert allowed == {"iam:CreateRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:TagRole", "iam:UntagRole", "iam:DeleteRole"}
    everywhere = {a for p in IAM_DIR.glob("*.json") for s in statements(p) if s["Effect"] == "Allow" for a in as_list(s["Action"])}
    assert not {a for a in everywhere if re.match(r"iam:(Attach|Detach|Create(User|AccessKey|LoginProfile|Policy|Group|InstanceProfile|ServiceLinkedRole)|Put(User|Group)Policy|Add)", a) and a != "iam:CreateServiceLinkedRole"}
    slr = [s for p in IAM_DIR.glob("*.json") for s in statements(p) if "iam:CreateServiceLinkedRole" in as_list(s["Action"])]
    assert slr and all("iam:AWSServiceName" in s["Condition"]["StringEquals"] for s in slr)


def test_the_execution_policy_no_longer_manages_roles():
    assert not [a for a in action_set(IAM_DIR / "cloudformation-execution-policy.json") if a.startswith("iam:")]


# -- PassRole and service principals -----------------------------------------------------------------


def test_every_pass_role_names_one_service_and_matches_the_trust_policy_of_the_roles_it_covers(synthesized):
    services = {}
    for s in statements(IAM_DIR / "passrole-policy.json"):
        for resource in as_list(s["Resource"]):
            services[resource.split("role/")[1]] = s["Condition"]["StringEquals"]["iam:PassedToService"]
    seen = 0
    for stack, logical, resource in roles(synthesized):
        name = f"{stack}-{logical}-ABCDEFGHIJKL"
        hits = [g for g in services if pattern(g).match(name)]
        assert len(hits) == 1, (name, hits)  # exactly one pattern, so one service, per role
        trust = {principal for st in resource["Properties"]["AssumeRolePolicyDocument"]["Statement"] for principal in as_list(st["Principal"]["Service"])}
        assert trust == {services[hits[0]]} == {SERVICE_FOR_PREFIX[hits[0].rstrip("*")]}
        seen += 1
    assert seen == len(SERVICE_FOR_PREFIX) == 9


# -- the boundary against the real roles ------------------------------------------------------------


def resolve(value, stack, template):
    if isinstance(value, str):
        return value
    (key, arg), = value.items()
    if key == "Fn::Join":
        return arg[0].join(resolve(part, stack, template) for part in arg[1])
    if key == "Fn::GetAtt":
        resource = template["Resources"][arg[0]]
        props = resource["Properties"]
        if resource["Type"] == "AWS::Logs::LogGroup":
            return f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{props.get('LogGroupName') or f'{stack}-{arg[0]}-X'}:*"
        if resource["Type"] == "AWS::Lambda::Function":
            return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{props['FunctionName']}"
    if key == "Fn::ImportValue":
        if "RunHistoryTable" in arg:
            return f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/irish-energy-run-history"
        if "ApiRepository" in arg:
            return f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/irish-energy-api"
        function = {"BronzeFunctionArn": "bronze", "SilverFunctionArn": "silver", "GoldFunctionArn": "gold", "GlueRegistrationFunctionArn": "glue-registration"}
        for suffix, name in function.items():
            if arg.endswith(suffix):
                return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:irish-energy-pipeline-{name}"
    raise AssertionError(f"unresolved {value}")


def uncovered(synthesized, boundary_path):
    boundary = statements(boundary_path)
    gaps = set()
    for stack, template in synthesized.items():
        for logical, role in ((n, r) for n, r in template["Resources"].items() if r["Type"] == "AWS::IAM::Role"):
            principal = f"arn:aws:iam::{ACCOUNT}:role/{stack}-{logical}-ABCDEFGHIJKL"
            for policy in template["Resources"].values():
                if policy["Type"] != "AWS::IAM::Policy" or {"Ref": logical} not in policy["Properties"]["Roles"]:
                    continue
                for s in policy["Properties"]["PolicyDocument"]["Statement"]:
                    conditions = s.get("Condition", {})
                    for action in as_list(s["Action"]):
                        for resource in as_list(s["Resource"]):
                            target = resolve(resource, stack, template)
                            ok = any(
                                b["Effect"] == "Allow" and action in as_list(b["Action"]) and matches(b["Resource"], target)
                                and ("Condition" not in b or (
                                    matches(b["Condition"].get("ArnLike", {"aws:PrincipalArn": ["*"]})["aws:PrincipalArn"], principal)
                                    and all(b["Condition"][op] == conditions.get(op) for op in b["Condition"] if op != "ArnLike")))
                                for b in boundary
                            )
                            denied = any(
                                b["Effect"] == "Deny" and action in as_list(b["Action"])
                                and ("Condition" not in b or not matches(b["Condition"]["ArnNotLike"]["aws:PrincipalArn"], principal))
                                for b in boundary
                            )
                            if denied or not ok:
                                gaps.add((re.sub(r"[0-9A-F]{8}$", "", logical), action))
    return gaps


def test_every_role_carries_the_boundary_when_the_context_is_set(synthesized):
    count = 0
    for _stack, _logical, resource in roles(synthesized):
        assert BOUNDARY_NAME in json.dumps(resource["Properties"]["PermissionsBoundary"])
        assert not resource["Properties"].get("ManagedPolicyArns") and not resource["Properties"].get("Policies")
        count += 1
    assert count == 9


def test_the_boundary_preserves_the_existing_least_privilege_runtime_design(synthesized):
    assert uncovered(synthesized, INITIAL) == {("ApiServiceTaskRole", "states:StartExecution")}
    assert uncovered(synthesized, ACTIVATION) == set()


def test_every_role_has_a_boundary_in_the_default_application(tmp_path):
    result, _ = run_app(
            tmp_path,
    {       "@aws-cdk/core:permissionsBoundary": {"name": BOUNDARY_NAME}},
        )
    templates = [json.loads(p.read_text()) for p in tmp_path.glob("*.template.json")]

    assert result.returncode == 0

    role_resources = [
        resource
        for template in templates
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == "AWS::IAM::Role"
    ]

    assert role_resources
    assert all(
        BOUNDARY_NAME in json.dumps(resource["Properties"].get("PermissionsBoundary", {}))
        for resource in role_resources
    )


# -- optional frontend publishing --------------------------------------------------------------------


def test_frontend_publishing_reaches_only_the_site_bucket_and_one_distribution():
    resources = {r for s in statements(FRONTEND) for r in as_list(s["Resource"])}

    assert resources == {
        "arn:aws:s3:::REPLACE_SITE_BUCKET", "arn:aws:s3:::REPLACE_SITE_BUCKET/*",
        f"arn:aws:cloudfront::{ACCOUNT}:distribution/REPLACE_DISTRIBUTION_ID",
    }
    assert action_set(FRONTEND) == {"s3:ListBucket", "s3:GetBucketLocation", "s3:PutObject", "s3:DeleteObject", "cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"}
    assert not any(s["Effect"] == "Deny" for s in statements(FRONTEND)) and not any("*" == r for r in resources)
    assert not any(a.startswith(("iam:", "sts:")) for a in action_set(FRONTEND))
