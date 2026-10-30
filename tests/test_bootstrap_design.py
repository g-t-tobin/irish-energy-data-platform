"""Offline checks of the Phase 9.7.4 bootstrap design artifacts and the synthesized manifest.

These tests inspect repository files only. They do not prove effective AWS permissions.
"""

import fnmatch
import json
import re
from pathlib import Path

import pytest
import yaml

from tests.test_energy_api_service_infrastructure import SERVICE_CTX
from tests.test_energy_edge_infrastructure import API_CERT, FRONTEND_CERT, run_app

ROOT = Path(__file__).resolve().parent.parent
IAM = ROOT / "docs" / "iam"
BOOT = IAM / "bootstrap"
DOC = IAM / "phase-9.7.4-bootstrap-and-deployment-identity.md"
ACCOUNT = "851725545966"
DEPLOYER = f"arn:aws:iam::{ACCOUNT}:role/irish-energy-cdk-deployer"
STACKS = {
    "IrishEnergyDataLakeStack", "IrishEnergyApiRegistryStack", "IrishEnergyRunHistoryStack",
    "IrishEnergyPipelineStack", "IrishEnergyWorkflowStack", "IrishEnergyReconcilerStack",
    "IrishEnergyApiEdgeStack", "IrishEnergyFrontendStack",
    "IrishEnergyCognitoStack",
}
STATELESS = {"IrishEnergyPipelineStack", "IrishEnergyWorkflowStack", "IrishEnergyReconcilerStack", "IrishEnergyApiEdgeStack"}
TEMPLATE = BOOT / "energydev01-bootstrap-template.yaml"
FILES = ["deploy-role-policy.json", "cfn-exec-protection-policy.json", "operator-roles-trust-policy.json", "cfn-exec-trust-policy.json"]


def load(name):
    return json.loads((BOOT / name).read_text())


def statements(name):
    return load(name)["Statement"]


def as_list(value):
    return value if isinstance(value, list) else [value]


def stack_names(arns):
    return {a.split("stack/")[1].split("/")[0] for a in as_list(arns)}


@pytest.mark.parametrize("name", FILES)
def test_bootstrap_documents_are_valid_and_small(name):
    text = (BOOT / name).read_text()
    assert json.loads(text)["Version"] == "2012-10-17"
    assert len("".join(text.split())) <= 6144
    assert "AKIA" not in text and "secret" not in text.lower()


def test_only_expected_bootstrap_files_exist():
    assert sorted(p.name for p in BOOT.glob("*")) == sorted([*FILES, TEMPLATE.name])


def test_deploy_role_policy_is_scoped_to_project_stacks():
    st = {s["Sid"]: s for s in statements("deploy-role-policy.json")}
    assert all(s["Effect"] == "Allow" for s in st.values())
    change = st["ChangeOnlyTheProjectStacks"]
    assert stack_names(change["Resource"]) == STACKS
    assert "cloudformation:DeleteStack" not in change["Action"]
    delete = st["DeleteOnlyTheStatelessProjectStacks"]
    assert delete["Action"] == ["cloudformation:DeleteStack"]
    assert stack_names(delete["Resource"]) == STATELESS
    for s in st.values():
        for action in as_list(s["Action"]):
            assert "*" not in action, action
    services = {a.split(":")[0] for s in st.values() for a in as_list(s["Action"])}
    assert services == {"cloudformation", "iam", "s3", "ssm", "sts"}
    text = (BOOT / "deploy-role-policy.json").read_text()
    for forbidden in ("CDKToolkit", "UpdateTerminationProtection", "kms:", "s3:PutObject", "s3:DeleteObject", "lookup-role", "ReadOnlyAccess"):
        assert forbidden not in text


def test_deploy_role_policy_resources_are_exact_except_caller_identity():
    for s in statements("deploy-role-policy.json"):
        if s["Sid"] == "ShowCallerIdentity":
            assert s["Action"] == ["sts:GetCallerIdentity"] and s["Resource"] == "*"
        else:
            for r in as_list(s["Resource"]):
                assert r != "*" and ":stack/" not in r or r.endswith("/*")
                assert r.startswith("arn:aws:")


def test_deploy_role_passrole_is_exact():
    s = next(s for s in statements("deploy-role-policy.json") if s["Action"] == ["iam:PassRole"])
    assert s["Resource"] == [f"arn:aws:iam::{ACCOUNT}:role/cdk-energydev01-cfn-exec-role-{ACCOUNT}-eu-west-1"]
    assert s["Condition"] == {"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}}


def test_deploy_role_ssm_parameter_is_exact():
    s = next(s for s in statements("deploy-role-policy.json") if s["Action"][0].startswith("ssm:"))
    assert s["Resource"] == [f"arn:aws:ssm:eu-west-1:{ACCOUNT}:parameter/cdk-bootstrap/energydev01/version"]


def test_deploy_role_stack_lists_match_the_other_policies():
    rollback = json.loads((IAM / "rollback-policy.json").read_text())
    rolled = {n for s in rollback["Statement"] if s["Effect"] == "Allow" and "cloudformation:DeleteStack" in as_list(s["Action"]) for n in stack_names(s["Resource"])}
    assert rolled <= STATELESS


def test_protection_policy_is_deny_only_and_covers_bootstrap_resources():
    sts = statements("cfn-exec-protection-policy.json")
    assert all(s["Effect"] == "Deny" and "Condition" not in s for s in sts)
    text = json.dumps(sts)
    for needle in (
        "iam:UpdateAssumeRolePolicy", "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion",
        "role/cdk-energydev01-*", "policy/irish-energy-*", "s3:PutObject", "s3:PutBucketPolicy", "ecr:PutImage",
        "ecr:SetRepositoryPolicy", "ssm:PutParameter", "parameter/cdk-bootstrap/*", "stack/CDKToolkit*/*", "cloudformation:DeleteStack",
        "sts:AssumeRole", "iam:CreateAccessKey", "iam:CreateUser",
    ):
        assert needle in text, needle


def test_execution_policies_do_not_allow_changing_bootstrap_resources():
    protection = statements("cfn-exec-protection-policy.json")
    denied = {a: [r for s in protection for r in as_list(s["Resource"]) if a in as_list(s["Action"])] for s in protection for a in as_list(s["Action"])}
    samples = [
        f"arn:aws:iam::{ACCOUNT}:role/cdk-energydev01-deploy-role-{ACCOUNT}-eu-west-1",
        f"arn:aws:s3:::cdk-energydev01-assets-{ACCOUNT}-eu-west-1/x",
        f"arn:aws:ecr:eu-west-1:{ACCOUNT}:repository/cdk-energydev01-container-assets-{ACCOUNT}-eu-west-1",
        f"arn:aws:ssm:eu-west-1:{ACCOUNT}:parameter/cdk-bootstrap/energydev01/version",
        f"arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/CDKToolkit-irish-energy/abc",
    ]
    names = ["cloudformation-execution", "lambda-deployment", "ecs-deployment", "network-deployment", "iam-role-management", "passrole"]
    for n in names:
        for s in json.loads((IAM / f"{n}-policy.json").read_text())["Statement"]:
            if s["Effect"] != "Allow":
                continue
            for a in as_list(s["Action"]):
                if a not in denied:
                    continue
                for r in as_list(s["Resource"]):
                    for sample in samples:
                        assert not fnmatch.fnmatchcase(sample, r), (n, a, r)


def test_operator_roles_trust_is_restricted_to_the_deployer():
    sts = statements("operator-roles-trust-policy.json")
    assert {a for s in sts for a in as_list(s["Action"])} == {"sts:AssumeRole", "sts:TagSession"}
    for s in sts:
        assert s["Effect"] == "Allow"
        assert s["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}
        assert s["Condition"]["ArnEquals"] == {"aws:PrincipalArn": DEPLOYER}
    assert sts[0]["Condition"]["Null"] == {"sts:ExternalId": "true"}
    assert '"*"' not in (BOOT / "operator-roles-trust-policy.json").read_text()


def test_execution_role_trust_is_cloudformation_for_project_stacks_only():
    (s,) = statements("cfn-exec-trust-policy.json")
    assert s["Principal"] == {"Service": "cloudformation.amazonaws.com"}
    assert s["Action"] == "sts:AssumeRole"
    assert s["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    assert stack_names(s["Condition"]["ArnLike"]["aws:SourceArn"]) == STACKS
    assert "CDKToolkit" not in json.dumps(s)


def test_no_policy_allows_assuming_the_lookup_role():
    for path in list(IAM.glob("*.json")) + list(BOOT.glob("*.json")):
        text = path.read_text()
        assert "lookup-role" not in text or path.name == "deployment-identity-policy.json", path.name
    identity = json.loads((IAM / "deployment-identity-policy.json").read_text())
    for s in identity["Statement"]:
        if s["Effect"] == "Allow":
            assert "lookup-role" not in json.dumps(s.get("Resource"))


@pytest.mark.parametrize(
    "ctx",
    [
        {},
        {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT},
        {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, **SERVICE_CTX},
        {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, **SERVICE_CTX, "reconcilerEnabled": "true"},
    ],
    ids=["none", "certs", "service", "full"],
)
def test_synthesis_has_no_missing_context_and_needs_no_lookup_role(tmp_path, ctx):
    result, _ = run_app(tmp_path, ctx)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest.get("missing", []) == []


def test_availability_zones_are_prepopulated_in_the_app():
    app = (ROOT / "infrastructure" / "app.py").read_text()
    assert "availability-zones:account={ACCOUNT}:region={REGION}" in app
    assert 'f"{REGION}a", f"{REGION}b", f"{REGION}c"' in app


def test_design_document_states_the_required_conclusions():
    text = DOC.read_text()
    for needle in (
        "offline design", "No AWS API", "A. Existing bootstrap", "B. Dedicated qualifier", "C. Dedicated execution role",
        "Recommendation: B", "AdministratorAccess", "account root", "InputPermissionsBoundary", "CDKToolkit",
        "unverified", "not applied", "REQUIRES EXPLICIT ADMINISTRATOR APPROVAL", "lookup role", "BootstrapVariant",
        "energydev01", "Possible without changing the existing `CDKToolkit` stack",
    ):
        assert needle in text, needle


QUALIFIER = "energydev01"
BOOTSTRAP_REFERENCES = [
    f"/cdk-bootstrap/{QUALIFIER}/version",
    f"cdk-{QUALIFIER}-assets-{ACCOUNT}-eu-west-1",
    f"cdk-{QUALIFIER}-deploy-role-{ACCOUNT}-eu-west-1",
    f"cdk-{QUALIFIER}-cfn-exec-role-{ACCOUNT}-eu-west-1",
]


def test_cdk_json_and_app_use_the_dedicated_qualifier():
    cdk_json = json.loads((ROOT / "infrastructure" / "cdk.json").read_text())
    assert cdk_json["context"] == {
        "@aws-cdk/core:bootstrapQualifier": QUALIFIER,
        "@aws-cdk/core:permissionsBoundary": {
            "name": "irish-energy-runtime-boundary"
        },
    }
    assert f'set_context("@aws-cdk/core:bootstrapQualifier", "{QUALIFIER}")' in (ROOT / "infrastructure" / "app.py").read_text()


@pytest.mark.parametrize(
    "ctx",
    [
        {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT, **SERVICE_CTX, "reconcilerEnabled": "true"},
        {},
    ],
    ids=["full", "none"],
)
def test_synthesized_assembly_uses_only_the_dedicated_qualifier(tmp_path, ctx):
    result, _ = run_app(tmp_path, ctx)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    stacks = {k: v for k, v in manifest["artifacts"].items() if v["type"] == "aws:cloudformation:stack"}
    assert len(stacks) >= 5
    for name, art in stacks.items():
        props = art["properties"]
        assert f"cdk-{QUALIFIER}-deploy-role-{ACCOUNT}-eu-west-1" in props["assumeRoleArn"], name
        assert f"cdk-{QUALIFIER}-cfn-exec-role-{ACCOUNT}-eu-west-1" in props["cloudFormationExecutionRoleArn"], name
        assert props["bootstrapStackVersionSsmParameter"] == f"/cdk-bootstrap/{QUALIFIER}/version", name
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".template"}:
            assert "hnb659fds" not in path.read_text(), path.name
    assembly = "".join(p.read_text() for p in tmp_path.glob("*.json"))
    for ref in BOOTSTRAP_REFERENCES:
        assert ref in assembly, ref
    assert manifest.get("missing", []) == []


# ---- customised bootstrap template -------------------------------------------------------------------

REGION = "eu-west-1"
TEMPLATE_VARS = {
    "AWS::Partition": "aws",
    "AWS::AccountId": ACCOUNT,
    "AWS::Region": REGION,
    "Qualifier": QUALIFIER,
    "DeployerRoleName": "irish-energy-cdk-deployer",
    "StagingBucket.Arn": f"arn:aws:s3:::cdk-{QUALIFIER}-assets-{ACCOUNT}-{REGION}",
    "CdkBootstrapVersion": f"/cdk-bootstrap/{QUALIFIER}/version",
    "ContainerAssetsRepository.Arn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/cdk-{QUALIFIER}-container-assets-{ACCOUNT}-{REGION}",
}


def template():
    return yaml.safe_load(TEMPLATE.read_text())


def render(node):
    if isinstance(node, dict):
        if set(node) == {"Fn::Sub"}:
            return re.sub(r"\$\{([^}]+)\}", lambda m: TEMPLATE_VARS[m.group(1)], node["Fn::Sub"])
        if node == {"Ref": "AWS::AccountId"}:
            return ACCOUNT
        return {k: render(v) for k, v in node.items()}
    if isinstance(node, list):
        return [render(v) for v in node]
    return node


def resource(name):
    return template()["Resources"][name]


def by_sid(statement_list):
    return {s["Sid"]: s for s in statement_list}


def normalise(statement):
    return {k: (sorted(as_list(v)) if k in {"Action", "Resource"} else v) for k, v in statement.items()}


def test_template_has_only_the_expected_resources():
    types = {n: r["Type"] for n, r in template()["Resources"].items()}
    assert types == {
        "StagingBucket": "AWS::S3::Bucket", "StagingBucketPolicy": "AWS::S3::BucketPolicy",
        "ContainerAssetsRepository": "AWS::ECR::Repository", "FilePublishingRole": "AWS::IAM::Role",
        "ImagePublishingRole": "AWS::IAM::Role", "FilePublishingRoleDefaultPolicy": "AWS::IAM::Policy",
        "ImagePublishingRoleDefaultPolicy": "AWS::IAM::Policy", "DeploymentActionRole": "AWS::IAM::Role",
        "CloudFormationExecutionRole": "AWS::IAM::Role", "CdkBootstrapVersion": "AWS::SSM::Parameter",
    }


def test_template_has_no_lookup_role_kms_key_admin_or_read_only_policy():
    text = TEMPLATE.read_text()
    for forbidden in (
        "LookupRole", "lookup-role", "AWS::KMS", "kms:", "EncryptionKey", "AdministratorAccess", "ReadOnlyAccess",
        "AWSCloudFormationReadOnlyAccess", "Refactor", "UpdateTerminationProtection", "CdkBoostrapPermissionsBoundaryPolicy",
        "PermissionsBoundaryPolicy", "emr-serverless",
    ):
        assert forbidden not in text, forbidden
    resources = json.dumps(template()["Resources"]) + json.dumps(template()["Conditions"])
    assert "TrustedAccounts" not in resources and "HasTrustedAccounts" not in resources
    assert resources.count('"Principal": "*"') == 1


def test_template_never_references_the_default_qualifier_or_toolkit_stack():
    text = TEMPLATE.read_text()
    assert "hnb659fds" not in text
    assert "CDKToolkit" not in text.replace("CDKToolkit-energydev01", "").replace("Never deploy it under the default qualifier or the stack name CDKToolkit.", "")
    t = template()
    assert t["Parameters"]["Qualifier"]["Default"] == QUALIFIER
    assert t["Parameters"]["Qualifier"]["AllowedValues"] == [QUALIFIER]


def test_template_names_agree_with_the_synthesized_references():
    t = template()
    rendered = {
        "bucket": render(t["Resources"]["StagingBucket"]["Properties"]["BucketName"]),
        "repo": render(t["Resources"]["ContainerAssetsRepository"]["Properties"]["RepositoryName"]),
        "version": render(t["Resources"]["CdkBootstrapVersion"]["Properties"]["Name"]),
        "deploy": render(t["Resources"]["DeploymentActionRole"]["Properties"]["RoleName"]),
        "exec": render(t["Resources"]["CloudFormationExecutionRole"]["Properties"]["RoleName"]),
        "file": render(t["Resources"]["FilePublishingRole"]["Properties"]["RoleName"]),
        "image": render(t["Resources"]["ImagePublishingRole"]["Properties"]["RoleName"]),
    }
    assert rendered["bucket"] == f"cdk-{QUALIFIER}-assets-{ACCOUNT}-{REGION}"
    assert rendered["repo"] == f"cdk-{QUALIFIER}-container-assets-{ACCOUNT}-{REGION}"
    assert rendered["version"] == f"/cdk-bootstrap/{QUALIFIER}/version"
    assert rendered["deploy"] == f"cdk-{QUALIFIER}-deploy-role-{ACCOUNT}-{REGION}"
    assert rendered["exec"] == f"cdk-{QUALIFIER}-cfn-exec-role-{ACCOUNT}-{REGION}"
    assert rendered["file"] == f"cdk-{QUALIFIER}-file-publishing-role-{ACCOUNT}-{REGION}"
    assert rendered["image"] == f"cdk-{QUALIFIER}-image-publishing-role-{ACCOUNT}-{REGION}"
    assert all(len(v) <= 64 for k, v in rendered.items() if k in {"deploy", "exec", "file", "image"})
    # Same names as the policy documents and the synthesized app references.
    docs = "".join(p.read_text() for p in IAM.glob("*.json")) + "".join(p.read_text() for p in BOOT.glob("*.json"))
    for name in (rendered["bucket"], rendered["repo"], rendered["deploy"], rendered["exec"], rendered["file"], rendered["image"]):
        assert name in docs or name in TEMPLATE.read_text(), name
    for ref in (rendered["bucket"], rendered["deploy"], rendered["exec"], rendered["version"]):
        assert ref in BOOTSTRAP_REFERENCES


def test_template_trust_for_the_three_operator_roles_matches_the_trust_document():
    expected = [normalise(s) for s in statements("operator-roles-trust-policy.json")]
    for name in ("DeploymentActionRole", "FilePublishingRole", "ImagePublishingRole"):
        trust = render(resource(name)["Properties"]["AssumeRolePolicyDocument"])
        assert [normalise(s) for s in trust["Statement"]] == expected, name


def test_template_execution_role_trust_matches_the_trust_document():
    trust = render(resource("CloudFormationExecutionRole")["Properties"]["AssumeRolePolicyDocument"])
    (expected,) = statements("cfn-exec-trust-policy.json")
    (actual,) = trust["Statement"]
    assert actual["Principal"] == expected["Principal"] and actual["Action"] == expected["Action"]
    assert actual["Condition"]["StringEquals"] == expected["Condition"]["StringEquals"]
    assert sorted(actual["Condition"]["ArnLike"]["aws:SourceArn"]) == sorted(expected["Condition"]["ArnLike"]["aws:SourceArn"])
    assert stack_names(actual["Condition"]["ArnLike"]["aws:SourceArn"]) == STACKS


def test_template_deployment_role_policy_matches_the_policy_document():
    props = resource("DeploymentActionRole")["Properties"]
    assert "ManagedPolicyArns" not in props
    (policy,) = props["Policies"]
    actual = by_sid(render(policy["PolicyDocument"]["Statement"]))
    expected = by_sid(statements("deploy-role-policy.json"))
    assert actual.keys() == expected.keys()
    for sid in expected:
        assert normalise(actual[sid]) == normalise(expected[sid]), sid


def test_template_execution_role_has_no_administrator_fallback_and_no_example_boundary():
    t = template()
    props = t["Resources"]["CloudFormationExecutionRole"]["Properties"]
    assert props["ManagedPolicyArns"] == {
        "Fn::If": ["HasCloudFormationExecutionPolicies", {"Ref": "CloudFormationExecutionPolicies"}, {"Ref": "AWS::NoValue"}]
    }
    assert t["Parameters"]["UseExamplePermissionsBoundary"]["AllowedValues"] == ["false"]
    assert (
        t["Parameters"]["InputPermissionsBoundary"]["Default"]
        == "irish-energy-deployment-boundary"
    )
    assert "ShouldCreatePermissionsBoundary" not in t["Conditions"]


def test_template_repository_policy_allows_only_project_lambda_functions():
    props = resource("ContainerAssetsRepository")["Properties"]
    (statement,) = props["RepositoryPolicyText"]["Statement"]
    assert statement["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert sorted(statement["Action"]) == ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    source = render(statement["Condition"]["StringLike"]["aws:sourceArn"])
    assert source == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:irish-energy-*"
    assert props["ImageTagMutability"] == "IMMUTABLE"


def test_template_bucket_is_private_encrypted_without_a_customer_key_and_retained():
    bucket = resource("StagingBucket")
    props = bucket["Properties"]
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True
    }
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    assert bucket["DeletionPolicy"] == "Retain" and bucket["UpdateReplacePolicy"] == "Retain"
    (deny,) = resource("StagingBucketPolicy")["Properties"]["PolicyDocument"]["Statement"]
    assert deny["Effect"] == "Deny" and deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    params = template()["Parameters"]
    assert params["FileAssetsBucketKmsKeyId"]["AllowedValues"] == ["", "AWS_MANAGED_KEY"]
    assert params["FileAssetsBucketName"]["AllowedValues"] == [""]
    assert params["ContainerAssetsRepositoryName"]["AllowedValues"] == [""]


def test_template_keeps_every_parameter_the_cli_passes_and_changes_the_variant():
    params = template()["Parameters"]
    for name in (
        "TrustedAccounts", "TrustedAccountsForLookup", "CloudFormationExecutionPolicies", "FileAssetsBucketName", "FileAssetsBucketKmsKeyId",
        "ContainerAssetsRepositoryName", "Qualifier", "PublicAccessBlockConfiguration", "InputPermissionsBoundary",
        "UseExamplePermissionsBoundary", "BootstrapVariant", "DenyExternalId",
    ):
        assert name in params, name
    assert params["BootstrapVariant"]["Default"] != "AWS CDK: Default Resources"
    assert params["DenyExternalId"]["AllowedValues"] == ["true"]
    assert re.fullmatch(params["DeployerRoleName"]["AllowedPattern"], "irish-energy-cdk-deployer")
    assert params["DeployerRoleName"]["Default"] == "irish-energy-cdk-deployer"


def test_template_trust_conditions_forbid_wildcards():
    for name in ("DeploymentActionRole", "FilePublishingRole", "ImagePublishingRole", "CloudFormationExecutionRole"):
        trust = json.dumps(render(resource(name)["Properties"]["AssumeRolePolicyDocument"]))
        assert '"*"' not in trust.replace("/*", ""), name


def test_seven_execution_policies_are_documented_with_the_dedicated_names():
    text = DOC.read_text()
    for needle in (
        "--qualifier energydev01", "--toolkit-stack-name CDKToolkit-energydev01", "--template docs/iam/bootstrap/energydev01-bootstrap-template.yaml",
        "--termination-protection", "seven managed policies", "cfn-exec-protection-policy.json", "`cloudformation-execution`", "`passrole`",
        "Termination protection** is not a template property", "REQUIRES EXPLICIT ADMINISTRATOR APPROVAL", "cdk bootstrap --show-template",
    ):
        assert needle in text, needle
    command = text[text.index("cdk bootstrap aws://"):]
    assert "hnb659fds" not in command.split("```")[0] and "--no-bootstrap-customer-key" not in command.split("```")[0]
    assert len([p for p in (IAM).glob("*-policy.json") if p.stem.removesuffix("-policy") in {
        "cloudformation-execution", "lambda-deployment", "ecs-deployment", "network-deployment", "iam-role-management", "passrole"}]) == 6


def test_no_document_or_test_still_uses_a_conflicting_name():
    conflicting = ("irenergy", "CDKToolkit-irish-energy", "REPLACE_QUALIFIER")
    paths = [*IAM.rglob("*"), *(ROOT / "docs").glob("*.md"), ROOT / "README.md", *(ROOT / "tests").glob("test_*.py")]
    for path in paths:
        if path.is_file() and path.name != Path(__file__).name:
            text = path.read_text()
            for word in conflicting:
                assert word not in text, (path.name, word)
    assert "CDKToolkit-energydev01" in DOC.read_text()


def test_template_keeps_the_file_asset_key_output_without_a_kms_key():
    t = template()
    output = t["Outputs"]["FileAssetKeyArn"]
    assert render(output["Export"]["Name"]) == f"CdkBootstrap-{QUALIFIER}-FileAssetKeyArn"
    assert output["Export"]["Name"] == {"Fn::Sub": "CdkBootstrap-${Qualifier}-FileAssetKeyArn"}
    assert output["Value"] == {"Fn::If": ["UseAwsManagedKey", {"Fn::Sub": "${FileAssetsBucketKmsKeyId}"}, "AWS_MANAGED_KEY"]}
    assert t["Conditions"]["UseAwsManagedKey"] == {"Fn::Equals": ["AWS_MANAGED_KEY", {"Ref": "FileAssetsBucketKmsKeyId"}]}
    assert not [n for n, r in t["Resources"].items() if r["Type"].startswith("AWS::KMS::")]
    assert "kms:" not in TEMPLATE.read_text()
    sse = t["Resources"]["StagingBucket"]["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert sse == [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]


def test_kms_key_parameter_accepts_only_empty_or_aws_managed_key():
    param = template()["Parameters"]["FileAssetsBucketKmsKeyId"]
    assert param["Type"] == "String" and param["Default"] == ""
    assert param["AllowedValues"] == ["", "AWS_MANAGED_KEY"]
    for value in (
        "1234abcd-12ab-34cd-56ef-1234567890ab",
        f"arn:aws:kms:{REGION}:{ACCOUNT}:key/1234abcd-12ab-34cd-56ef-1234567890ab",
        "alias/my-key",
        "aws_managed_key",
        " ",
    ):
        assert value not in param["AllowedValues"], value
    assert "AllowedPattern" not in param
    uses = [name for name, node in template()["Resources"].items() if "FileAssetsBucketKmsKeyId" in json.dumps(node)]
    assert uses == []
