"""Phase 9.7.2.2: nothing in the administrator package can reach the game's CloudFront, load balancers or other resources."""

import json
import re
from pathlib import Path

import pytest

from tests.test_energy_api_service_infrastructure import SERVICE_CTX
from tests.test_energy_edge_infrastructure import API_CERT, FRONTEND_CERT, run_app

DOCS = Path(__file__).resolve().parents[1] / "docs"
IAM_DIR = DOCS / "iam"
ACCOUNT, REGION = "851725545966", "eu-west-1"
POLICIES = sorted(p for p in IAM_DIR.glob("*.json") if p.name != "deployer-role-trust-policy.json")
BOUNDARIES = {"runtime-permissions-boundary.json", "runtime-permissions-boundary-execution-activation.json", "deployment-permissions-boundary.json"}
EXECUTION = ["cloudformation-execution-policy.json", "iam-role-management-policy.json", "lambda-deployment-policy.json", "ecs-deployment-policy.json", "network-deployment-policy.json", "passrole-policy.json"]
CLOUDFRONT_FILES = {"frontend-publishing-policy.json", "frontend-stack-update-policy.json"}
NINE_ROLES = [
    "IrishEnergyPipelineStack-BronzeRole", "IrishEnergyPipelineStack-SilverRole", "IrishEnergyPipelineStack-GoldRole",
    "IrishEnergyPipelineStack-GlueRegistrationRole", "IrishEnergyReconcilerStack-Role", "IrishEnergyReconcilerStack-SchedulerRole",
    "IrishEnergyWorkflowStack-StateMachineRole", "IrishEnergyApiEdgeStack-ApiServiceExecutionRole", "IrishEnergyApiEdgeStack-ApiServiceTaskRole",
]
# Plausible names of other applications' resources in this account (the game); none may match any resource pattern.
GAME_RESOURCES = [
    f"arn:aws:cloudfront::{ACCOUNT}:distribution/E1GAMEDIST", f"arn:aws:cloudfront::{ACCOUNT}:origin-access-control/E2GAMEOAC",
    f"arn:aws:cloudfront::{ACCOUNT}:response-headers-policy/3gamepolicy",
    f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/bluff-alb/0123456789abcdef",
    f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/bluff-alb/0123456789abcdef/fedcba9876543210",
    f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener-rule/app/bluff-alb/0123456789abcdef/fedcba9876543210/00aa00aa00aa00aa",
    f"arn:aws:iam::{ACCOUNT}:role/BluffOrBelieveTaskRole", f"arn:aws:iam::{ACCOUNT}:role/cdk-gamequal-deploy-role-{ACCOUNT}-{REGION}",
    "arn:aws:s3:::bluff-or-believe-site", "arn:aws:s3:::bluff-or-believe-site/index.html",
    f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/BluffCluster", f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/BluffCluster/api",
    f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/BluffTask:7",
    f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:bluff-handler", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/bluff-games",
    f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/bluff-handler:*", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/bluff-api",
    f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:bluff-flow", f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/default/bluff-tick",
    f"arn:aws:glue:{REGION}:{ACCOUNT}:database/bluff", f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/BluffStack/abc",
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/CDKToolkit/abc", f"arn:aws:athena:{REGION}:{ACCOUNT}:workgroup/bluff",
]
# Patterns that reach other applications' resources unless a tag condition applies; each must carry one.
TAG_CONDITIONED = {
    ("network-deployment-policy.json", "ElbCreateLoadBalancerAndTargetGroupTagged"),
    ("network-deployment-policy.json", "ElbManageProjectLoadBalancerAndTaggedTargetGroups"),
    ("network-deployment-policy.json", "Ec2CreateNetworkingTaggedWithProject"),
    ("network-deployment-policy.json", "Ec2TagOnCreateOnly"),
    ("network-deployment-policy.json", "Ec2ManageOnlyProjectTaggedNetworking"),
}

READ_ONLY_BOOTSTRAP_STACK = {("read-only-verification-policy.json", "ReadBootstrapAndProjectStacks")}  # describe-only, bootstrap stack inspection


def statements(path):
    return json.loads(path.read_text())["Statement"]


def as_list(value):
    return value if isinstance(value, list) else [value]


def glob(pattern, value):
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), value) is not None


def actions(path, effect="Allow"):
    return [a for s in statements(path) if s["Effect"] == effect for a in as_list(s["Action"])]


def by_sid(path):
    return {s["Sid"]: s for s in statements(path)}


# -- Critical 1: CloudFront ---------------------------------------------------------------------------


def test_the_general_execution_policies_hold_no_cloudfront_and_no_site_bucket_permissions():
    for name in EXECUTION:
        text = (IAM_DIR / name).read_text()
        assert "cloudfront" not in text and "irishenergyfrontendstack" not in text, name
        assert not {a for a in actions(IAM_DIR / name) if a.startswith(("s3:Create", "s3:Put", "s3:Delete"))}, name


def test_cloudfront_actions_exist_only_in_three_documents_and_only_on_exact_identifiers():
    for path in POLICIES:
        cloudfront = [s for s in statements(path) if s["Effect"] == "Allow" and any(a.startswith("cloudfront:") for a in as_list(s["Action"]))]
        if path.name not in CLOUDFRONT_FILES:
            assert not cloudfront, path.name
            continue
        for s in cloudfront:
            for resource in as_list(s["Resource"]):
                assert re.fullmatch(rf"arn:aws:cloudfront::{ACCOUNT}:(distribution|origin-access-control|response-headers-policy)/REPLACE_[A-Z_]+", resource), resource
            assert not [a for a in as_list(s["Action"]) if re.search(r"(Create|Delete|Copy|Associate)", a) and a != "cloudfront:CreateInvalidation"]


def test_no_policy_can_create_or_delete_a_distribution_oac_or_response_headers_policy():
    everything = {a for p in POLICIES for a in actions(p)}

    assert not {a for a in everything if re.match(r"cloudfront:(Create(Distribution|OriginAccessControl|ResponseHeadersPolicy)|Delete\w+|Update\w+Policy)", a) and a != "cloudfront:UpdateResponseHeadersPolicy"}
    assert "cloudfront:UpdateDistribution" in actions(IAM_DIR / "frontend-stack-update-policy.json")
    assert not any(a == "cloudfront:UpdateDistribution" for p in POLICIES if p.name != "frontend-stack-update-policy.json" for a in actions(p))


def test_the_frontend_stack_is_a_supervised_stack_the_package_cannot_deploy_or_delete():
    rollback = by_sid(IAM_DIR / "rollback-policy.json")
    allowed = " ".join(as_list(rollback["CancelRollbackOrDeleteStatelessProjectStacks"]["Resource"]))
    denied = " ".join(as_list(rollback["DenyDeletingStatefulSupervisedOrBootstrapStacks"]["Resource"]))

    assert "IrishEnergyFrontendStack" not in allowed and "IrishEnergyFrontendStack" in denied
    assert "supervised" in (DOCS / "administrator-handover.md").read_text().lower()
    assert "frontend-stack-update-policy.json" in (IAM_DIR / "README.md").read_text()


# -- Critical 2: ELB listeners ------------------------------------------------------------------------


def test_listener_rule_and_load_balancer_permissions_name_the_project_load_balancer():
    elb = [s for p in POLICIES if p.name not in BOUNDARIES for s in statements(p) if s["Effect"] == "Allow" and any(a.startswith("elasticloadbalancing:") and not a.startswith("elasticloadbalancing:Describe") for a in as_list(s["Action"]))]
    assert {s["Sid"] for s in elb} == {"ElbCreateLoadBalancerAndTargetGroupTagged", "ElbManageProjectLoadBalancerAndTaggedTargetGroups", "ElbListenersAndRulesOfTheProjectLoadBalancerOnly"}
    for s in elb:
        for resource in as_list(s["Resource"]):
            if "targetgroup" in resource:
                assert "Condition" in s, s["Sid"]  # target groups are generated, so they are isolated by the Project tag
            else:
                assert "/app/irish-energy-api/" in resource, resource
    listeners = by_sid(IAM_DIR / "network-deployment-policy.json")["ElbListenersAndRulesOfTheProjectLoadBalancerOnly"]
    assert not [a for a in as_list(listeners["Action"]) if a.startswith("elasticloadbalancing:Add")]


def test_no_listener_rule_or_load_balancer_pattern_matches_the_games_load_balancer():
    patterns = [r for p in POLICIES for s in statements(p) if s["Effect"] == "Allow" for r in as_list(s["Resource"]) if "elasticloadbalancing" in r and "targetgroup" not in r]
    game = [g for g in GAME_RESOURCES if "elasticloadbalancing" in g]

    assert patterns and game
    assert not [(r, g) for r in patterns for g in game if glob(r, g)]
    project = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/irish-energy-api/0123456789abcdef/fedcba9876543210"
    assert any(glob(r, project) for r in patterns)


def test_the_edge_stack_load_balancer_has_the_fixed_name_the_policies_rely_on(tmp_path):
    ctx = {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, **SERVICE_CTX}
    result, _ = run_app(tmp_path, ctx)
    assert result.returncode == 0, result.stderr
    template = json.loads((tmp_path / "IrishEnergyApiEdgeStack.template.json").read_text())
    names = [r["Properties"].get("Name") for r in template["Resources"].values() if r["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"]

    assert names == ["irish-energy-api"]


def test_every_ec2_change_carries_a_project_tag_condition():
    net = by_sid(IAM_DIR / "network-deployment-policy.json")
    for sid, s in net.items():
        if sid.startswith("Ec2") and "Describe" not in sid:
            condition = json.dumps(s["Condition"])
            assert "aws:RequestTag/Project" in condition or "aws:ResourceTag/Project" in condition or "ec2:CreateAction" in condition, sid
    assert all(a.startswith("ec2:Describe") for a in as_list(net["Ec2DescribeNoResourceLevelSupport"]["Action"]))
    assert not any(a in as_list(net["Ec2TagOnCreateOnly"]["Action"]) for a in ("ec2:DeleteTags",))


# -- High 3: trust-policy modification ---------------------------------------------------------------


def test_trust_policy_updates_are_not_part_of_normal_deployment():
    holders = [p.name for p in POLICIES if "iam:UpdateAssumeRolePolicy" in actions(p)]

    assert holders == ["iam-trust-update-policy.json"]
    assert "iam-trust-update-policy.json" not in EXECUTION
    trust = statements(IAM_DIR / "iam-trust-update-policy.json")
    assert len(trust) == 1 and set(as_list(trust[0]["Resource"])) == {f"arn:aws:iam::{ACCOUNT}:role/{r}*" for r in NINE_ROLES}
    assert as_list(trust[0]["Action"]) == ["iam:UpdateAssumeRolePolicy"] and "Condition" not in trust[0]


def test_the_handover_requires_review_of_every_trust_policy_change():
    text = (DOCS / "administrator-handover.md").read_text().lower()

    assert "iam-trust-update-policy.json" in text and "every runtime-role trust-policy change" in text
    assert "boundary does not protect trust" in text


# -- game-resource exclusion and wildcard audit ---------------------------------------------------------


def test_no_allow_statement_reaches_a_plausible_game_resource():
    offenders = []
    for path in POLICIES:
        if path.name in BOUNDARIES:
            continue
        for s in statements(path):
            if s["Effect"] != "Allow":
                continue
            for resource in as_list(s["Resource"]):
                if resource == "*":
                    continue
                for game in GAME_RESOURCES:
                    if glob(resource, game) and (path.name, s["Sid"]) not in TAG_CONDITIONED | READ_ONLY_BOOTSTRAP_STACK:
                        offenders.append((path.name, s["Sid"], resource, game))
    assert offenders == []


def test_tag_conditioned_patterns_really_carry_the_condition():
    for path in POLICIES:
        for s in statements(path):
            if (path.name, s["Sid"]) in TAG_CONDITIONED:
                assert "Project" in json.dumps(s["Condition"]) or "ec2:CreateAction" in json.dumps(s["Condition"]), s["Sid"]
                assert "irish-energy-data-platform" in json.dumps(s["Condition"]) or "CreateAction" in json.dumps(s["Condition"])


def test_resource_wildcard_statements_are_read_only_or_have_no_destructive_action():
    risky = re.compile(r"(Create|Delete|Put|Update|Modify|Attach|Detach|Deregister|Revoke|Authorize|Associate|Disassociate|Tag|Invoke|Start)")
    allowed_star = {  # no resource-level support; each documented in docs/iam/README.md
        ("deployment-identity-policy.json", "ShowCallerIdentity"),
        ("ecr-publishing-policy.json", "RegistryLoginTokenNoResourceLevelSupport"),
        ("ecs-deployment-policy.json", "EcsRegisterAndDescribeTaskDefinitionsNoResourceLevelSupport"),
    }
    for path in POLICIES:
        for s in statements(path):
            if s["Effect"] == "Allow" and "*" in as_list(s["Resource"]) and path.name not in BOUNDARIES:
                danger = [a for a in as_list(s["Action"]) if not a.split(":")[1].startswith(("Describe", "Get", "List")) and risky.search(a.split(":")[1])]
                assert not danger or (path.name, s["Sid"]) in allowed_star, (path.name, s["Sid"], danger)
    ecs = by_sid(IAM_DIR / "ecs-deployment-policy.json")
    assert as_list(ecs["EcsDeregisterOnlyTheProjectTaskDefinitionFamily"]["Action"]) == ["ecs:DeregisterTaskDefinition"]
    assert "IrishEnergyApiEdgeStackApiServiceTaskDefinition" in ecs["EcsDeregisterOnlyTheProjectTaskDefinitionFamily"]["Resource"][0]


def test_describe_log_groups_is_documented_as_an_account_wide_read_only_action():
    lam = by_sid(IAM_DIR / "lambda-deployment-policy.json")["DescribeLogGroupsAccountWideReadOnly"]

    assert as_list(lam["Action"]) == ["logs:DescribeLogGroups"] and as_list(lam["Resource"]) == ["*"]
    assert "account-wide" in (DOCS / "phase-9.7.2-administrator-preparation.md").read_text().lower()


def test_read_only_policy_cannot_read_function_environments_of_other_applications():
    ro = by_sid(IAM_DIR / "read-only-verification-policy.json")

    for sid, s in ro.items():
        for a in as_list(s["Action"]):
            if a in {"lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:ListFunctions", "lambda:GetPolicy"}:
                assert "*" not in as_list(s["Resource"]) and all("irish-energy" in r for r in as_list(s["Resource"])), sid
    assert not any(a.startswith(("secretsmanager:", "kms:")) for p in POLICIES for a in actions(p))


# -- boundary: StartExecution for every runtime role -------------------------------------------------------


@pytest.mark.parametrize("prefix", NINE_ROLES)
def test_initial_boundary_denies_start_execution_for_every_runtime_role(prefix):
    principal = f"arn:aws:iam::{ACCOUNT}:role/{prefix}XYZ-ABCDEFGHIJKL"
    boundary = statements(IAM_DIR / "runtime-permissions-boundary.json")

    assert not any(s["Effect"] == "Allow" and "states:StartExecution" in as_list(s["Action"]) for s in boundary)
    assert any(s["Effect"] == "Deny" and "states:StartExecution" in as_list(s["Action"]) and "Condition" not in s for s in boundary)
    assert principal


@pytest.mark.parametrize("prefix", NINE_ROLES)
def test_activation_variant_allows_start_execution_only_for_the_api_task_role(prefix):
    principal = f"arn:aws:iam::{ACCOUNT}:role/{prefix}XYZ-ABCDEFGHIJKL"
    boundary = statements(IAM_DIR / "runtime-permissions-boundary-execution-activation.json")
    allowed = any(
        s["Effect"] == "Allow" and "states:StartExecution" in as_list(s["Action"]) and any(glob(p, principal) for p in as_list(s["Condition"]["ArnLike"]["aws:PrincipalArn"]))
        for s in boundary
    )
    denied = any(
        s["Effect"] == "Deny" and "states:StartExecution" in as_list(s["Action"]) and not any(glob(p, principal) for p in as_list(s["Condition"]["ArnNotLike"]["aws:PrincipalArn"]))
        for s in boundary
    )

    assert allowed == (prefix == "IrishEnergyApiEdgeStack-ApiServiceTaskRole") and denied == (prefix != "IrishEnergyApiEdgeStack-ApiServiceTaskRole")


# -- resource-based policies bypass review -----------------------------------------------------------------


def test_the_documents_explain_resource_based_policy_bypass_and_how_to_inspect_it():
    text = (DOCS / "phase-9.7.2.1-boundary-hardening.md").read_text() + (DOCS / "phase-9.7.2-administrator-preparation.md").read_text()

    assert "resource-based" in text.lower() and "bypass" in text.lower()
    read_only = actions(IAM_DIR / "read-only-verification-policy.json")
    assert {"s3:GetBucketPolicy", "ecr:GetRepositoryPolicy", "lambda:GetPolicy", "dynamodb:GetResourcePolicy"} <= set(read_only)
