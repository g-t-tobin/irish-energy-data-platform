"""Phase 9.7.2.3: the three explicitly authorised inspection policies are separate from the general read-only policy."""

import json
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"
IAM_DIR = DOCS / "iam"
ACCOUNT, REGION = "851725545966", "eu-west-1"
READ_ONLY = IAM_DIR / "read-only-verification-policy.json"
DNS = IAM_DIR / "dns-inspection-policy.json"
CFN = IAM_DIR / "cloudformation-diagnostic-policy.json"
COGNITO = IAM_DIR / "cognito-inspection-policy.json"
INSPECTION = {DNS: "route53:ListResourceRecordSets", CFN: "cloudformation:DescribeStackEvents", COGNITO: "cognito-idp:DescribeUserPool"}
MOVED = set(INSPECTION.values()) | {"cognito-idp:ListUserPools"}
POLICIES = sorted(IAM_DIR.glob("*.json"))
STACKS = ["IrishEnergyDataLakeStack", "IrishEnergyApiRegistryStack", "IrishEnergyRunHistoryStack", "IrishEnergyPipelineStack",
          "IrishEnergyWorkflowStack", "IrishEnergyReconcilerStack", "IrishEnergyApiEdgeStack", "IrishEnergyFrontendStack"]
GAME = [
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/BluffStack/abc", f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/CDKToolkit/abc",
    "arn:aws:route53:::hostedzone/Z0GAMEZONE", f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/{REGION}_GamePool",
]
CONTENT_READS = re.compile(
    r"^(s3:GetObject\w*|ecr:(BatchGetImage|GetDownloadUrlForLayer)|dynamodb:(GetItem|BatchGetItem|Query|Scan)|lambda:(GetFunction\w*|GetLayerVersion\w*)|"
    r"secretsmanager:\w+|kms:\w+|ssm:(GetParametersByPath|GetParameters|GetParameterHistory|DescribeParameters)|cognito-idp:(DescribeUserPoolClient|DescribeIdentityProvider|Admin\w*|\w*Secret\w*)|"
    r"states:(GetExecutionHistory|DescribeExecution)|logs:(GetLogEvents|FilterLogEvents)|acm:(GetCertificate|ExportCertificate))$"
)


def statements(path):
    return json.loads(path.read_text())["Statement"]


def as_list(value):
    return value if isinstance(value, list) else [value]


def actions(path):
    return [a for s in statements(path) if s["Effect"] == "Allow" for a in as_list(s["Action"])]


def norm(path):
    return " ".join(path.read_text().lower().split())


def glob(pattern, value):
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), value) is not None


# -- separation ---------------------------------------------------------------------------------------


def test_the_general_policy_contains_none_of_the_moved_permissions():
    granted = set(actions(READ_ONLY))

    assert not granted & MOVED
    assert not {a for a in granted if a.startswith("route53:List") and a != "route53:ListHostedZonesByName"}
    assert {"route53:ListHostedZonesByName", "route53:GetHostedZone", "cloudformation:DescribeStacks"} <= granted


@pytest.mark.parametrize("path,action", list(INSPECTION.items()), ids=lambda v: getattr(v, "name", v))
def test_each_inspection_policy_grants_exactly_one_read_action(path, action):
    document = json.loads(path.read_text())

    assert set(document) == {"Version", "Statement"} and len(document["Statement"]) == 1
    (statement,) = document["Statement"]
    assert statement["Effect"] == "Allow" and as_list(statement["Action"]) == [action] and "Condition" not in statement
    assert "*" not in as_list(statement["Resource"])
    elsewhere = {p.name for p in POLICIES if p != path and action in actions(p)}
    assert elsewhere <= ({"deployment-identity-policy.json"} if path == CFN else set())  # the deployer keeps stack events to monitor deployments


def test_inspection_resource_scopes_are_exact():
    assert [r for s in statements(DNS) for r in as_list(s["Resource"])] == ["arn:aws:route53:::hostedzone/REPLACE_HOSTED_ZONE_ID"]
    assert [r for s in statements(COGNITO) for r in as_list(s["Resource"])] == [f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/REPLACE_USER_POOL_ID"]
    stacks = [r for s in statements(CFN) for r in as_list(s["Resource"])]
    assert stacks == [f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{n}/*" for n in STACKS]
    assert not [r for r in stacks if "CDKToolkit" in r]  # bootstrap stacks are not part of the diagnostic policy


def test_no_inspection_resource_matches_a_plausible_game_resource():
    for path in INSPECTION:
        for resource in [r for s in statements(path) for r in as_list(s["Resource"])]:
            assert not [g for g in GAME if glob(resource, g)], (path.name, resource)


# -- no writes, no content reads ----------------------------------------------------------------------


def test_the_package_grants_no_dns_modification_or_hosted_zone_management():
    route53 = {a for p in POLICIES for a in actions(p) if a.startswith("route53:")}

    assert route53 == {"route53:ListHostedZonesByName", "route53:GetHostedZone", "route53:ListResourceRecordSets"}
    assert not {a for a in route53 if re.search(r"Change|Create|Delete|Associate|Disassociate|Update|Enable|Disable", a)}


def test_the_diagnostic_policy_grants_no_cloudformation_write_deploy_or_rollback():
    granted = set(actions(CFN))

    assert granted == {"cloudformation:DescribeStackEvents"}
    assert not any(re.search(r"Create|Update|Delete|Execute|Rollback|Continue|Cancel|Signal|SetStackPolicy|Import|Detect|Register", a) for a in granted)
    writes = re.compile(r"cloudformation:(Create|Update|Delete|Execute|Rollback|ContinueUpdateRollback|CancelUpdateStack|SetStackPolicy|SignalResource)")
    for path in POLICIES:
        if path.name not in {"rollback-policy.json", "deployment-identity-policy.json"}:
            assert not [a for a in actions(path) if writes.match(a)], path.name


def test_the_package_grants_no_cognito_user_pool_or_user_management_write_or_secret_retrieval():
    cognito = {a for p in POLICIES for a in actions(p) if a.startswith("cognito-idp:")}

    assert cognito == {"cognito-idp:ListUserPoolClients", "cognito-idp:ListGroups", "cognito-idp:DescribeUserPool"}
    assert not any(re.search(r"Admin|Create|Update|Delete|Set|Initiate|Respond|Confirm|SignUp|ForgotPassword|ListUsers|GetUser|Client\b|IdentityProvider|Secret|ListUserPools$", a) for a in cognito - {"cognito-idp:ListUserPoolClients"})


def test_no_new_policy_and_no_general_policy_can_read_content_or_secrets():
    for path in (READ_ONLY, DNS, CFN, COGNITO):
        for action in actions(path):
            assert not CONTENT_READS.match(action), (path.name, action)
    assert not any(a == "s3:GetObject" for a in actions(READ_ONLY))


def test_the_bootstrap_version_parameter_permission_is_still_exact():
    ssm = [s for s in statements(READ_ONLY) if any(a.startswith("ssm:") for a in as_list(s["Action"]))]

    assert len(ssm) == 1 and as_list(ssm[0]["Action"]) == ["ssm:GetParameter"]
    assert as_list(ssm[0]["Resource"]) == [f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/energydev01/version"]
    for forbidden in ("lambda:GetFunction", "lambda:GetFunctionConfiguration", "ssm:GetParametersByPath"):
        assert forbidden not in actions(READ_ONLY)


def test_every_policy_is_valid_json_within_the_iam_size_limit():
    for path in POLICIES:
        document = json.loads(path.read_text())
        assert document["Version"] == "2012-10-17"
        assert len(re.sub(r"\s", "", path.read_text())) <= 6144, path.name


# -- documentation ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["iam/README.md", "phase-9.7.2-administrator-preparation.md", "administrator-handover.md"])
def test_the_documents_describe_the_separate_inspection_policies_and_their_exposure(name):
    text = norm(DOCS / name)

    for policy in ("dns-inspection-policy.json", "cloudformation-diagnostic-policy.json", "cognito-inspection-policy.json"):
        assert policy in text, policy
    assert "explicit administrator approval" in text or "explicitly authorised" in text or "explicit approval" in text
    assert "read-only" in text and ("not the same as safe" in text or "read-only does not mean safe" in text)


def test_the_phase_document_states_the_exposure_and_the_unverified_status():
    text = norm(DOCS / "phase-9.7.2-administrator-preparation.md")

    assert "cannot restrict" in text and "individual record names" in text and "every record in the shared zone" in text
    assert "resource properties" in text and "does not prevent sensitive" in text
    assert "sms" in text and "lambda trigger" in text
    assert "unverified until separately authorised" in text
