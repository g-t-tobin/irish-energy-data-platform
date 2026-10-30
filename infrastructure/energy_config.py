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
