"""Local, offline checks of the energy edge CDK stacks (no AWS access, nothing deployed)."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infrastructure"
sys.path.insert(0, str(INFRA))

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402

from energy_config import (  # noqa: E402
    ACCOUNT,
    API_HOSTNAME,
    DOMAIN,
    FRONTEND_HOSTNAME,
    ConfigError,
    load_edge_config,
    validate_certificate_arn,
    validate_hostname,
)
from stacks.energy_edge_stacks import EnergyApiEdgeStack, EnergyFrontendStack  # noqa: E402

API_CERT = f"arn:aws:acm:eu-west-1:{ACCOUNT}:certificate/00000000-0000-4000-8000-000000000001"
FRONTEND_CERT = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/00000000-0000-4000-8000-000000000002"
GAME_HOSTS = (f"api.{DOMAIN}", f"play.{DOMAIN}")


def context(values):
    return lambda key: values.get(key)


def config():
    loaded = load_edge_config(
        context({"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT}), {}
    )
    assert loaded is not None
    return loaded


def synth():
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region="eu-west-1")
    api = EnergyApiEdgeStack(app, "IrishEnergyApiEdgeStack", config=config(), env=env)
    site = EnergyFrontendStack(app, "IrishEnergyFrontendStack", config=config(), env=env)
    return Template.from_stack(api), Template.from_stack(site)


@pytest.fixture(scope="module")
def templates():
    return synth()


# Configuration rules


def test_the_confirmed_hostnames_are_valid_and_separate_from_the_game():
    assert validate_hostname(API_HOSTNAME) == "energy-api.ai-autofy-general-server.com"
    assert validate_hostname(FRONTEND_HOSTNAME) == "energy.ai-autofy-general-server.com"
    assert {API_HOSTNAME, FRONTEND_HOSTNAME}.isdisjoint(GAME_HOSTS)


@pytest.mark.parametrize(
    "hostname",
    [
        f"api.{DOMAIN}",
        f"play.{DOMAIN}",
        DOMAIN,
        f"www.{DOMAIN}",
        f"*.{DOMAIN}",
        f"energy.{DOMAIN}.evil.com",
        f"a.b.{DOMAIN}",
        "energy.example.com",
        f"Energy.{DOMAIN}",
        f"-bad.{DOMAIN}",
        f".{DOMAIN}",
    ],
)
def test_game_apex_wildcard_and_foreign_hostnames_are_rejected(hostname):
    with pytest.raises(ConfigError):
        validate_hostname(hostname)


def test_certificates_must_be_in_the_right_region_and_account():
    assert validate_certificate_arn(API_CERT, region="eu-west-1", purpose="API") == API_CERT
    for arn, region in [
        (FRONTEND_CERT, "eu-west-1"),  # a CloudFront certificate cannot front the ALB
        (API_CERT, "us-east-1"),  # an ALB certificate cannot front CloudFront
        (f"arn:aws:acm:eu-west-1:111111111111:certificate/{API_CERT[-36:]}", "eu-west-1"),
        ("arn:aws:acm:eu-west-1:851725545966:certificate/not-a-uuid", "eu-west-1"),
        ("arn:aws:iam::851725545966:role/x", "eu-west-1"),
        ("", "eu-west-1"),
    ]:
        with pytest.raises(ConfigError):
            validate_certificate_arn(arn, region=region, purpose="test")


def test_no_certificates_means_the_edge_stacks_are_skipped():
    assert load_edge_config(context({}), {}) is None


def test_both_certificates_are_required_together():
    for only in ({"apiCertificateArn": API_CERT}, {"frontendCertificateArn": FRONTEND_CERT}):
        with pytest.raises(ConfigError, match="both"):
            load_edge_config(context(only), {})


def test_the_frontend_certificate_must_be_us_east_1_and_the_api_one_eu_west_1():
    with pytest.raises(ConfigError):
        load_edge_config(
            context({"apiCertificateArn": FRONTEND_CERT, "frontendCertificateArn": FRONTEND_CERT}), {}
        )
    with pytest.raises(ConfigError):
        load_edge_config(
            context({"apiCertificateArn": API_CERT, "frontendCertificateArn": API_CERT}), {}
        )


def test_synthesis_refuses_a_different_aws_account():
    with pytest.raises(ConfigError, match="Refusing"):
        load_edge_config(context({}), {"CDK_DEFAULT_ACCOUNT": "111111111111"})


# API: HTTPS ALB


def test_alb_has_an_https_listener_with_the_api_certificate_and_modern_tls(templates):
    api, _ = templates

    api.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 443,
            "Protocol": "HTTPS",
            "Certificates": [{"CertificateArn": API_CERT}],
            "SslPolicy": Match.string_like_regexp("TLS13"),
        },
    )
    certificates = [
        json.dumps(listener["Properties"].get("Certificates"))
        for listener in api.find_resources("AWS::ElasticLoadBalancingV2::Listener").values()
        if listener["Properties"]["Port"] == 443
    ]
    assert certificates == [json.dumps([{"CertificateArn": API_CERT}])]
    assert FRONTEND_CERT not in json.dumps(api.to_json())


def test_http_only_redirects_to_https(templates):
    api, _ = templates

    api.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 80,
            "DefaultActions": [
                {"Type": "redirect", "RedirectConfig": {"Protocol": "HTTPS", "Port": "443", "StatusCode": "HTTP_301"}}
            ],
        },
    )


def test_only_the_energy_api_host_header_is_routed(templates):
    api, _ = templates

    api.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 10,
            "Conditions": [{"Field": "host-header", "HostHeaderConfig": {"Values": [API_HOSTNAME]}}],
        },
    )
    hosts = [
        value
        for rule in api.find_resources("AWS::ElasticLoadBalancingV2::ListenerRule").values()
        for condition in rule["Properties"]["Conditions"]
        for value in condition["HostHeaderConfig"]["Values"]
    ]
    assert hosts == [API_HOSTNAME]


def test_alb_is_internet_facing_hardened_and_owns_its_network(templates):
    api, _ = templates

    api.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {
            "Scheme": "internet-facing",
            "LoadBalancerAttributes": Match.array_with(
                [
                    {"Key": "deletion_protection.enabled", "Value": "true"},
                    {"Key": "routing.http.drop_invalid_header_fields.enabled", "Value": "true"},
                ]
            ),
        },
    )
    api.resource_count_is("AWS::EC2::VPC", 1)
    api.resource_count_is("AWS::EC2::NatGateway", 0)
    for output in ("ApiHostname", "AlbDnsName", "AlbCanonicalHostedZoneId"):
        api.has_output(output, {})


# Frontend: CloudFront


def test_distribution_serves_only_the_energy_hostname_with_the_us_east_1_certificate(templates):
    _, site = templates

    site.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "Aliases": [FRONTEND_HOSTNAME],
                    "ViewerCertificate": {
                        "AcmCertificateArn": FRONTEND_CERT,
                        "MinimumProtocolVersion": "TLSv1.2_2021",
                        "SslSupportMethod": "sni-only",
                    },
                    "DefaultRootObject": "index.html",
                    "DefaultCacheBehavior": Match.object_like({"ViewerProtocolPolicy": "redirect-to-https"}),
                }
            )
        },
    )
    assert API_CERT not in json.dumps(site.to_json())


def test_site_bucket_is_private_encrypted_https_only_and_retained(templates):
    _, site = templates

    site.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )
    site.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Retain"})
    site.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    assert '"Principal": "*"' not in json.dumps(site.to_json())


def test_security_headers_and_spa_fallbacks_are_configured(templates):
    _, site = templates

    site.has_resource_properties(
        "AWS::CloudFront::ResponseHeadersPolicy",
        {
            "ResponseHeadersPolicyConfig": Match.object_like(
                {
                    "SecurityHeadersConfig": Match.object_like(
                        {
                            "StrictTransportSecurity": Match.object_like({"Override": True}),
                            "ContentTypeOptions": {"Override": True},
                        }
                    )
                }
            )
        },
    )
    distribution = next(iter(site.find_resources("AWS::CloudFront::Distribution").values()))
    codes = {r["ErrorCode"]: r["ResponseCode"] for r in distribution["Properties"]["DistributionConfig"]["CustomErrorResponses"]}
    assert codes == {403: 200, 404: 200}


# Isolation from the game and from DNS/certificate creation


def test_templates_create_no_dns_records_certificates_or_foreign_resources(templates):
    for template in templates:
        types = {r["Type"] for r in template.to_json()["Resources"].values()}
        assert not {t for t in types if t.startswith("AWS::Route53")}
        assert not {t for t in types if t.startswith("AWS::CertificateManager")}
        assert "AWS::IAM::User" not in types and "AWS::Cognito::UserPool" not in types


def test_templates_never_mention_the_game_hostnames_or_resources(templates):
    for template in templates:
        text = json.dumps(template.to_json()).lower()
        for host in GAME_HOSTS:
            assert not re.search(rf"(?<![\w-]){re.escape(host)}", text)
        assert "bluff" not in text and "believe" not in text


def test_stacks_use_their_own_names_and_project_tags(templates):
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region="eu-west-1")
    stacks = [
        EnergyApiEdgeStack(app, "IrishEnergyApiEdgeStack", config=config(), env=env),
        EnergyFrontendStack(app, "IrishEnergyFrontendStack", config=config(), env=env),
    ]

    assert [s.stack_name for s in stacks] == ["IrishEnergyApiEdgeStack", "IrishEnergyFrontendStack"]
    for template in templates:
        assert not any("name" in str(p).lower() and "game" in str(p).lower() for p in template.to_json().get("Parameters", {}))


# Whole app, run exactly as `python app.py` does (CDK_CONTEXT_JSON is how the CLI passes -c)


def run_app(tmp_path, ctx=None, env_extra=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AWS_") and k not in {"CDK_DEFAULT_ACCOUNT", "CDK_CONTEXT_JSON"}
    }
    env.update({"CDK_OUTDIR": str(tmp_path), "AWS_EC2_METADATA_DISABLED": "true", **(env_extra or {})})
    if ctx is not None:
        env["CDK_CONTEXT_JSON"] = json.dumps(ctx)
    result = subprocess.run(
        [sys.executable, "app.py"], cwd=INFRA, env=env, capture_output=True, text=True, timeout=300
    )
    return result, sorted(p.name for p in tmp_path.glob("*.template.json"))


def test_app_synthesizes_the_data_lake_and_registry_without_certificates(tmp_path):
    result, templates_ = run_app(tmp_path)

    assert result.returncode == 0, result.stderr
    assert templates_ == [
        "IrishEnergyApiRegistryStack.template.json",
        "IrishEnergyDataLakeStack.template.json",
        "IrishEnergyPipelineStack.template.json",
        "IrishEnergyRunHistoryStack.template.json",
    ]
    assert "skipped" in result.stdout


def test_app_synthesizes_all_independent_stacks_with_both_certificates(tmp_path):
    ctx = {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT}

    result, templates_ = run_app(tmp_path, ctx)

    assert result.returncode == 0, result.stderr
    assert templates_ == [
        "IrishEnergyApiEdgeStack.template.json",
        "IrishEnergyApiRegistryStack.template.json",
        "IrishEnergyDataLakeStack.template.json",
        "IrishEnergyFrontendStack.template.json",
        "IrishEnergyPipelineStack.template.json",
        "IrishEnergyRunHistoryStack.template.json",
    ]


def test_existing_data_lake_template_is_identical_with_and_without_edge_stacks(tmp_path):
    ctx = {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT}
    without_dir, with_dir = tmp_path / "without", tmp_path / "with"
    without_dir.mkdir()
    with_dir.mkdir()

    run_app(without_dir)
    run_app(with_dir, ctx)

    name = "IrishEnergyDataLakeStack.template.json"
    assert json.loads((without_dir / name).read_text()) == json.loads((with_dir / name).read_text())


@pytest.mark.parametrize(
    "ctx,message",
    [
        ({"apiCertificateArn": API_CERT}, "both"),
        ({"apiCertificateArn": FRONTEND_CERT, "frontendCertificateArn": FRONTEND_CERT}, "eu-west-1"),
        ({"apiCertificateArn": API_CERT, "frontendCertificateArn": API_CERT}, "us-east-1"),
    ],
)
def test_app_stops_on_unsafe_certificate_configuration(tmp_path, ctx, message):
    result, templates_ = run_app(tmp_path, ctx)

    assert result.returncode != 0
    assert message in result.stderr
    assert templates_ == []


def test_app_refuses_to_run_for_another_account(tmp_path):
    result, _ = run_app(tmp_path, env_extra={"CDK_DEFAULT_ACCOUNT": "111111111111"})

    assert result.returncode != 0
    assert "Refusing" in result.stderr
