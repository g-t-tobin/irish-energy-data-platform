"""Phase 9.7.2.2 correction: the general package cannot read client secrets or other applications' sensitive configuration."""

import json
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"
IAM_DIR = DOCS / "iam"
ACCOUNT, REGION = "851725545966", "eu-west-1"
POLICIES = sorted(p for p in IAM_DIR.glob("*.json") if p.name != "deployer-role-trust-policy.json")
READ_ONLY = IAM_DIR / "read-only-verification-policy.json"
EXECUTION = IAM_DIR / "cloudformation-execution-policy.json"

ALLOWED_READ_ONLY_ACTIONS = {
    "sts:GetCallerIdentity", "ssm:GetParameter",
    "cloudformation:DescribeStacks", "cloudformation:DescribeStackResources",
    "cloudformation:ListStackResources", "cloudformation:GetTemplate", "cloudformation:ListStacks",
    "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
    "s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock",
    "s3:GetBucketPolicy", "s3:GetBucketOwnershipControls", "s3:ListBucket",
    "ecr:DescribeRepositories", "ecr:GetRepositoryPolicy", "ecr:GetLifecyclePolicy",
    "glue:GetDatabase", "glue:GetTable", "athena:GetWorkGroup",
    "acm:ListCertificates", "acm:DescribeCertificate",
    "cognito-idp:ListUserPoolClients", "cognito-idp:ListGroups",
    "route53:ListHostedZonesByName", "route53:GetHostedZone",
    "lambda:GetPolicy", "dynamodb:DescribeTable", "dynamodb:GetResourcePolicy",
    "states:DescribeStateMachine", "scheduler:GetSchedule",
    "states:ListStateMachines", "logs:DescribeLogGroups", "ecs:ListClusters", "elasticloadbalancing:DescribeLoadBalancers", "ec2:DescribeVpcs",
    "servicequotas:ListServiceQuotas", "servicequotas:GetServiceQuota",
}
# Every action that may use Resource "*" in the read-only policy: lists of names or generic metadata, no configuration of one application
ALLOWED_READ_ONLY_WILDCARD = {
    "sts:GetCallerIdentity", "cloudformation:ListStacks", "acm:ListCertificates",
    "route53:ListHostedZonesByName", "states:ListStateMachines", "logs:DescribeLogGroups", "ecs:ListClusters",
    "elasticloadbalancing:DescribeLoadBalancers", "ec2:DescribeVpcs", "servicequotas:ListServiceQuotas", "servicequotas:GetServiceQuota",
}
# Calls that return secrets, credentials or another application's sensitive configuration.
SENSITIVE = re.compile(
    r"^(cognito-idp:(DescribeUserPoolClient|DescribeIdentityProvider|\w*Secret\w*|Admin\w*|GetUserPoolMfaConfig|CreateUserPoolClient|UpdateUserPoolClient)|"
    r"iam:(ListRoles|GetAccessKeyLastUsed|ListAccessKeys|CreateAccessKey|GetLoginProfile|GetUser|ListUsers|GetAccountAuthorizationDetails|GenerateCredentialReport)|"
    r"cloudformation:ListExports|cloudfront:(ListDistributions|GetDistribution\w*)|ssm:DescribeParameters|"
    r"lambda:(GetFunction|ListFunctions|GetFunctionConfiguration|GetFunctionUrlConfig|GetLayerVersion\w*)|ssm:(GetParametersByPath|GetParameters|GetParameterHistory)|secretsmanager:\w+|kms:\w+|sts:(GetSessionToken|GetFederationToken)|"
    r"acm:(GetCertificate|ExportCertificate)|ecr:GetAuthorizationToken|codeartifact:\w+|s3:GetObject\w*)$"
)


def statements(path):
    return json.loads(path.read_text())["Statement"]


def as_list(value):
    return value if isinstance(value, list) else [value]


def actions(path, effect="Allow"):
    return {a for s in statements(path) if s["Effect"] == effect for a in as_list(s["Action"])}


def test_no_policy_in_the_package_can_describe_a_user_pool_client_or_read_any_client_secret():
    for path in POLICIES:
        found = {a for a in actions(path) if a.startswith("cognito-idp:")}
        assert "cognito-idp:DescribeUserPoolClient" not in found, path.name
        assert not {a for a in found if re.search(r"Client|IdentityProvider|Secret|Admin|Create|Update|Delete|Set", a) and a != "cognito-idp:ListUserPoolClients"}, path.name
    assert "DescribeUserPoolClient" not in " ".join(p.read_text() for p in POLICIES)


def test_cognito_access_is_limited_to_client_names_and_groups_of_the_energy_user_pool():
    read_only = statements(READ_ONLY)
    cognito = [s for s in read_only if any(a.startswith("cognito-idp:") for a in as_list(s["Action"]))]

    assert not [s for s in cognito if "*" in as_list(s["Resource"])]  # no cross-pool listing: ListUserPools is not granted
    assert {a for s in cognito for a in as_list(s["Action"])} == {"cognito-idp:ListUserPoolClients", "cognito-idp:ListGroups"}
    assert {r for s in cognito for r in as_list(s["Resource"])} == {f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/REPLACE_USER_POOL_ID"}
    others = {p.name for p in POLICIES if p != READ_ONLY and any(a.startswith("cognito-idp:") for a in actions(p))}
    assert others == {"cognito-inspection-policy.json"}


def test_the_read_only_policy_is_exactly_the_reviewed_allow_list():
    assert actions(READ_ONLY) == ALLOWED_READ_ONLY_ACTIONS
    assert not any(s["Effect"] == "Deny" for s in statements(READ_ONLY))


def test_read_only_wildcard_resources_are_lists_and_generic_metadata_only():
    wildcard = {a for s in statements(READ_ONLY) if "*" in as_list(s["Resource"]) for a in as_list(s["Action"])}

    assert wildcard == ALLOWED_READ_ONLY_WILDCARD


def test_no_policy_grants_a_call_that_returns_secrets_or_other_applications_configuration():
    for path in POLICIES:
        for s in statements(path):
            if s["Effect"] != "Allow":
                continue
            resources = as_list(s["Resource"])
            exact_project = "*" not in resources and all(("irish-energy" in r or "REPLACE_" in r) and not r.endswith("*") for r in resources)
            for action in as_list(s["Action"]):
                if not SENSITIVE.match(action):
                    continue
                # documented exceptions: the ECR login token, the boundary's runtime data access, and reads of one exact project resource
                boundary = path.name in {"runtime-permissions-boundary.json", "runtime-permissions-boundary-execution-activation.json", "deployment-permissions-boundary.json"}
                assert action == "ecr:GetAuthorizationToken" or boundary or exact_project, (path.name, s["Sid"], action)


def test_function_and_resource_policy_reads_name_only_project_resources():
    for s in statements(READ_ONLY):
        for action in as_list(s["Action"]):
            if action in {"lambda:GetPolicy", "dynamodb:DescribeTable", "dynamodb:GetResourcePolicy", "states:DescribeStateMachine", "scheduler:GetSchedule"}:
                assert all("irish-energy" in r for r in as_list(s["Resource"])), (s["Sid"], action)


def test_parameter_store_access_is_the_single_bootstrap_version_parameter():
    ssm = [s for s in statements(READ_ONLY) if any(a.startswith("ssm:") for a in as_list(s["Action"]))]

    assert len(ssm) == 1 and as_list(ssm[0]["Action"]) == ["ssm:GetParameter"]
    assert as_list(ssm[0]["Resource"]) == [f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/energydev01/version"]


def test_the_general_read_only_policy_grants_no_lambda_configuration_function_download_or_ssm_listing():
    granted = actions(READ_ONLY)

    for forbidden in ("lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:ListFunctions", "lambda:GetFunctionUrlConfig",
                      "ssm:GetParametersByPath", "ssm:GetParameters", "ssm:DescribeParameters", "ssm:GetParameterHistory"):
        assert forbidden not in granted, forbidden
    assert {a for a in granted if a.startswith("lambda:")} == {"lambda:GetPolicy"}
    lam = [s for s in statements(READ_ONLY) if "lambda:GetPolicy" in as_list(s["Action"])]
    functions = {f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{n}" for n in (
        "irish-energy-pipeline-bronze", "irish-energy-pipeline-silver", "irish-energy-pipeline-gold",
        "irish-energy-pipeline-glue-registration", "irish-energy-run-reconciler")}
    lambda_resources = {r for r in as_list(lam[0]["Resource"]) if r.startswith("arn:aws:lambda")}
    assert len(lam) == 1 and lambda_resources == functions


def test_the_read_only_policy_cannot_read_objects_image_layers_items_secrets_or_packages():
    reads_content = re.compile(
        r"^(s3:(GetObject\w*)|ecr:(BatchGetImage|GetDownloadUrlForLayer|GetAuthorizationToken)|dynamodb:(GetItem|BatchGetItem|Query|Scan|PartiQL\w*)|"
        r"lambda:(GetFunction\w*|GetLayerVersion\w*|Invoke\w*)|states:(GetExecutionHistory|DescribeExecution|StartExecution)|logs:(GetLogEvents|FilterLogEvents|StartQuery|GetQueryResults)|"
        r"secretsmanager:\w+|kms:\w+|acm:(GetCertificate|ExportCertificate)|athena:(StartQueryExecution|GetQueryResults)|glue:GetPartitions?)$"
    )
    for action in actions(READ_ONLY):
        assert not reads_content.match(action), action
    listing = [s for s in statements(READ_ONLY) if "s3:ListBucket" in as_list(s["Action"])]
    assert len(listing) == 1 and as_list(listing[0]["Resource"]) == [f"arn:aws:s3:::irish-energy-data-platform-{ACCOUNT}"]  # names only, project bucket


def test_bucket_inspection_cannot_list_the_bootstrap_bucket_and_certificates_are_exact():
    by_sid = {s["Sid"]: s for s in statements(READ_ONLY)}

    assert "s3:ListBucket" not in as_list(by_sid["InspectBootstrapAssetBucketSettings"]["Action"])
    assert as_list(by_sid["InspectDataLakeBucket"]["Resource"]) == [f"arn:aws:s3:::irish-energy-data-platform-{ACCOUNT}"]
    assert all("REPLACE_" in r for r in as_list(by_sid["DescribeOnlyTheTwoEnergyCertificates"]["Resource"]))
    assert all("REPLACE_" in r for r in as_list(by_sid["GetOnlyTheEnergyHostedZone"]["Resource"]))
    assert not any(a.startswith("cloudfront:") for a in actions(READ_ONLY))


def test_a_function_environment_or_any_secret_bearing_call_is_not_available_for_unrelated_names():
    for s in statements(READ_ONLY):
        if "*" in as_list(s["Resource"]):
            assert not {a for a in as_list(s["Action"]) if SENSITIVE.match(a)}, s["Sid"]


# -- Glue and Athena deletion ------------------------------------------------------------------------------


def test_the_execution_policy_cannot_delete_glue_or_athena_objects():
    granted = actions(EXECUTION)

    assert not {a for a in granted if re.match(r"(glue|athena):(Delete|Batch)", a)}
    assert {"glue:CreateDatabase", "glue:UpdateDatabase", "glue:CreateTable", "glue:UpdateTable", "athena:CreateWorkGroup", "athena:UpdateWorkGroup"} <= granted
    assert not any(re.match(r"(glue|athena):Delete", a) for p in POLICIES if not p.name.startswith("runtime-permissions-boundary") for a in actions(p))


# -- documentation -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["phase-9.7.2-administrator-preparation.md", "administrator-handover.md"])
def test_the_documents_require_a_separate_identity_for_the_app_client_without_recording_the_secret(name):
    text = " ".join((DOCS / name).read_text().lower().split())

    assert "describeuserpoolclient" in text and "separately authorised" in text
    assert "client secret" in text and ("is not a security boundary" in text or "is not an iam" in text)
    assert "without printing" in text or "never print" in text


def test_the_documents_describe_the_glue_deletion_and_failed_creation_rollback_consequence():
    text = " ".join((DOCS / "phase-9.7.2-administrator-preparation.md").read_text().lower().split())

    assert "glue:deletedatabase" in text and "glue:deletetable" in text
    assert "failed creation" in text and "administrator cleanup" in text
