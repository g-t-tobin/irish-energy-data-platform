"""Hostnames and certificate rules for the Irish Energy Analytics edge stacks.

Everything here is separate from the Bluff or Believe game, which owns
`api.` and `play.` under the same domain and its own ALB, CloudFront
distribution and certificate. This module only validates names and ARNs; it
makes no AWS calls and creates no DNS records or certificates.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ACCOUNT = "851725545966"
REGION = "eu-west-1"
CLOUDFRONT_CERTIFICATE_REGION = "us-east-1"
DOMAIN = "ai-autofy-general-server.com"

API_HOSTNAME = f"energy-api.{DOMAIN}"
FRONTEND_HOSTNAME = f"energy.{DOMAIN}"

# Names in use by the game, plus the apex; the energy stacks must never claim them.
RESERVED_HOSTNAMES = frozenset(
    {DOMAIN, f"www.{DOMAIN}", f"api.{DOMAIN}", f"play.{DOMAIN}"}
)

API_CERTIFICATE_CONTEXT = "apiCertificateArn"
FRONTEND_CERTIFICATE_CONTEXT = "frontendCertificateArn"
API_IMAGE_TAG_CONTEXT = "apiImageTag"
COGNITO_POOL_CONTEXT = "cognitoUserPoolId"
COGNITO_CLIENT_CONTEXT = "cognitoAppClientId"

# Explicit service settings (no secrets): see docs/administrator-handover.md.
ECR_REPOSITORY_NAME = "irish-energy-api"
RUN_HISTORY_TABLE_NAME = "irish-energy-run-history"
DATA_LAKE_BUCKET = "irish-energy-data-platform-851725545966"
GOLD_PREFIX = "gold/eirgrid/daily_demand/"
CONTAINER_PORT = 8000
HEALTH_CHECK_PATH = "/health"
TASK_CPU = 256
TASK_MEMORY_MIB = 1024
DESIRED_COUNT = 1
LOG_RETENTION_DAYS = 30
CONTAINER_USER = "10001:10001"

# Pipeline stage Lambda functions (Phase 9.4). Initial sizes are estimates, not measurements.
PIPELINE_FUNCTION_PREFIX = "irish-energy-pipeline"
PIPELINE_LAMBDA_MAX_TIMEOUT_SECONDS = 900
GLUE_DATABASE = "irish_energy_analytics"
GLUE_TABLE = "daily_electricity_demand"
BRONZE_PREFIX = "bronze/eirgrid/demand_actual/"
SILVER_PREFIX = "silver/eirgrid/demand_actual/"


@dataclass(frozen=True)
class PipelineStage:
    key: str  # state name used in the execution input contract
    handler: str
    timeout_seconds: int
    memory_mib: int
    ephemeral_mib: int = 512  # Parquet is processed in memory; /tmp is not used


PIPELINE_STAGES = (
    PipelineStage("bronze", "src.pipeline_stages.ingest_handler", 120, 256),
    PipelineStage("silver", "src.pipeline_stages.silver_handler", 300, 1024),
    PipelineStage("gold", "src.pipeline_stages.gold_handler", 300, 1024),
    PipelineStage("glue_registration", "src.pipeline_stages.glue_handler", 60, 512),
)

# Step Functions workflow (Phase 9.5). The ARN is derived, not looked up: names are fixed.
STATE_MACHINE_NAME = "irish-energy-pipeline"
STATE_MACHINE_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{STATE_MACHINE_NAME}"
STATE_MACHINE_EXECUTION_ARN_PATTERN = f"arn:aws:states:{REGION}:{ACCOUNT}:execution:{STATE_MACHINE_NAME}:*"
PIPELINE_FUNCTION_EXPORT = "IrishEnergyPipeline-{label}FunctionArn"  # exported by the pipeline stack

_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_POOL_RE = re.compile(r"^(?P<region>[a-z]{2}(?:-[a-z]+)+-\d)_[A-Za-z0-9]+$")
_CLIENT_RE = re.compile(r"^[A-Za-z0-9]{10,128}$")
_SECRET_ARN_RE = re.compile(
    rf"^arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{{6}}$"
)
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_CERT_ARN_RE = re.compile(
    r"^arn:aws:acm:(?P<region>[a-z]{2}(?:-[a-z]+)+-\d):(?P<account>\d{12}):"
    r"certificate/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ConfigError(ValueError):
    """The edge configuration is invalid or unsafe; stop before synthesizing."""


@dataclass(frozen=True)
class EdgeConfig:
    account: str
    region: str
    api_hostname: str
    frontend_hostname: str
    api_certificate_arn: str
    frontend_certificate_arn: str


def validate_hostname(hostname: str) -> str:
    """Exactly one label under the domain, and never a game or apex name."""
    if hostname != hostname.strip().lower() or "*" in hostname:
        raise ConfigError(f"Invalid hostname: {hostname!r}")
    if hostname in RESERVED_HOSTNAMES:
        raise ConfigError(f"{hostname} is reserved for another application.")
    suffix = f".{DOMAIN}"
    label = hostname[: -len(suffix)] if hostname.endswith(suffix) else ""
    if not label or not _LABEL_RE.match(label):
        raise ConfigError(f"{hostname} must be a single label under {DOMAIN}.")
    return hostname


def validate_certificate_arn(arn: str, *, region: str, purpose: str) -> str:
    """The certificate must live in the right account and the region the service requires."""
    match = _CERT_ARN_RE.match(arn or "")
    if not match:
        raise ConfigError(f"{purpose} certificate ARN is not a valid ACM certificate ARN.")
    if match["account"] != ACCOUNT:
        raise ConfigError(f"{purpose} certificate must belong to account {ACCOUNT}.")
    if match["region"] != region:
        raise ConfigError(f"{purpose} certificate must be in {region}.")
    return arn


def load_edge_config(
    get_context: Callable[[str], Any], env: Mapping[str, str]
) -> EdgeConfig | None:
    """None when no certificate ARNs are supplied (edge stacks are then skipped)."""
    default_account = env.get("CDK_DEFAULT_ACCOUNT")
    if default_account and default_account != ACCOUNT:
        raise ConfigError(f"Refusing to synthesize for account {default_account}; expected {ACCOUNT}.")

    api_arn = get_context(API_CERTIFICATE_CONTEXT)
    frontend_arn = get_context(FRONTEND_CERTIFICATE_CONTEXT)
    if not api_arn and not frontend_arn:
        return None
    if not api_arn or not frontend_arn:
        raise ConfigError(
            f"Provide both -c {API_CERTIFICATE_CONTEXT}=... and -c {FRONTEND_CERTIFICATE_CONTEXT}=..."
        )

    return EdgeConfig(
        account=ACCOUNT,
        region=REGION,
        api_hostname=validate_hostname(API_HOSTNAME),
        frontend_hostname=validate_hostname(FRONTEND_HOSTNAME),
        api_certificate_arn=validate_certificate_arn(api_arn, region=REGION, purpose="API"),
        frontend_certificate_arn=validate_certificate_arn(
            frontend_arn, region=CLOUDFRONT_CERTIFICATE_REGION, purpose="Frontend"
        ),
    )


@dataclass(frozen=True)
class ApiServiceConfig:
    """Settings of the ECS Fargate API service. Cognito IDs are identifiers, not secrets."""

    image_tag: str
    cognito_region: str
    user_pool_id: str
    app_client_id: str
    bucket: str = DATA_LAKE_BUCKET
    allowed_origin: str = f"https://{FRONTEND_HOSTNAME}"
    container_port: int = CONTAINER_PORT
    cpu: int = TASK_CPU
    memory_mib: int = TASK_MEMORY_MIB
    desired_count: int = DESIRED_COUNT
    health_check_path: str = HEALTH_CHECK_PATH
    log_retention_days: int = LOG_RETENTION_DAYS
    # Environment variable name -> Secrets Manager ARN. Empty: the API needs no secrets.
    secret_arns: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name, arn in self.secret_arns:
            if not re.match(r"^[A-Z][A-Z0-9_]*$", name) or not _SECRET_ARN_RE.match(arn):
                raise ConfigError(f"Invalid secret mapping for {name!r}.")


def load_service_config(get_context: Callable[[str], Any]) -> ApiServiceConfig | None:
    """None when none of the service inputs are supplied; all three are needed together."""
    tag = get_context(API_IMAGE_TAG_CONTEXT)
    pool = get_context(COGNITO_POOL_CONTEXT)
    client = get_context(COGNITO_CLIENT_CONTEXT)
    if not (tag or pool or client):
        return None
    if not (tag and pool and client):
        raise ConfigError(
            f"Provide all of -c {API_IMAGE_TAG_CONTEXT}=..., -c {COGNITO_POOL_CONTEXT}=... "
            f"and -c {COGNITO_CLIENT_CONTEXT}=... to include the API service."
        )
    if tag == "latest" or not _IMAGE_TAG_RE.match(str(tag)):
        # Repository tags are immutable; a moving tag would never deploy a new image.
        raise ConfigError("apiImageTag must be a specific version tag (not 'latest').")
    match = _POOL_RE.match(str(pool))
    if not match or match["region"] != REGION:
        raise ConfigError(f"cognitoUserPoolId must be a user pool in {REGION}.")
    if not _CLIENT_RE.match(str(client)):
        raise ConfigError("cognitoAppClientId is not a valid app client ID.")
    return ApiServiceConfig(
        image_tag=str(tag), cognito_region=REGION, user_pool_id=str(pool), app_client_id=str(client)
    )
