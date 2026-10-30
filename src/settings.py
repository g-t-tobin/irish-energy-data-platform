"""Validated API security settings with fail-closed defaults."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

LOCAL_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class SettingsError(ValueError):
    """The configuration is invalid or unsafe; startup must stop."""


_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_POOL_RE = re.compile(r"^(?P<region>[a-z]{2}(?:-[a-z]+)+-\d)_[A-Za-z0-9]+$")
_CLIENT_RE = re.compile(r"^[A-Za-z0-9]{10,128}$")


@dataclass(frozen=True)
class CognitoConfig:
    region: str
    user_pool_id: str
    client_id: str

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


@dataclass(frozen=True)
class RateLimits:
    """Per-user limits (per API process; see src/ratelimit.py)."""

    read_per_minute: int = 60
    analytics_per_minute: int = 30
    write_per_hour: int = 5
    auth_failures_per_minute: int = 20  # per client address


@dataclass(frozen=True)
class RunHistorySettings:
    backend: str = "sqlite"  # "sqlite" for local development, "dynamodb" when shared
    table: str | None = None
    region: str = "eu-west-1"


_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")


def load_run_history_settings(env: Mapping[str, str]) -> RunHistorySettings:
    backend = (env.get("RUN_HISTORY_BACKEND") or "sqlite").strip().lower()
    if backend not in {"sqlite", "dynamodb"}:
        raise SettingsError("RUN_HISTORY_BACKEND must be 'sqlite' or 'dynamodb'.")
    table = (env.get("RUN_HISTORY_TABLE") or "").strip() or None
    region = (env.get("RUN_HISTORY_REGION") or "eu-west-1").strip()
    if backend == "dynamodb":
        if not table or not _TABLE_RE.match(table):
            raise SettingsError("RUN_HISTORY_TABLE must name the DynamoDB table.")
        if not _REGION_RE.match(region):
            raise SettingsError("RUN_HISTORY_REGION is invalid.")
    return RunHistorySettings(backend=backend, table=table, region=region)


@dataclass(frozen=True)
class Settings:
    api_env: str = "production"
    cors_allowed_origins: tuple[str, ...] = ()
    pipeline_execution_enabled: bool = False
    enable_api_docs: bool = False
    # None means authentication is unconfigured: protected routes fail closed.
    cognito: CognitoConfig | None = None
    rate_limits: RateLimits = RateLimits()
    run_history: RunHistorySettings = RunHistorySettings()


def _parse_limit(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number.") from None
    if not 1 <= value <= 100_000:
        raise SettingsError(f"{name} must be between 1 and 100000.")
    return value


def _load_rate_limits(env: Mapping[str, str]) -> RateLimits:
    defaults = RateLimits()
    return RateLimits(
        read_per_minute=_parse_limit(env, "RATE_LIMIT_READ_PER_MINUTE", defaults.read_per_minute),
        analytics_per_minute=_parse_limit(
            env, "RATE_LIMIT_ANALYTICS_PER_MINUTE", defaults.analytics_per_minute
        ),
        write_per_hour=_parse_limit(env, "RATE_LIMIT_WRITE_PER_HOUR", defaults.write_per_hour),
        auth_failures_per_minute=_parse_limit(
            env, "RATE_LIMIT_AUTH_FAILURES_PER_MINUTE", defaults.auth_failures_per_minute
        ),
    )


def _load_cognito(env: Mapping[str, str]) -> CognitoConfig | None:
    names = ("COGNITO_REGION", "COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID")
    values = {name: (env.get(name) or "").strip() for name in names}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SettingsError(f"Incomplete Cognito configuration: set {missing}.")

    region, pool_id, client_id = (values[name] for name in names)
    pool = _POOL_RE.match(pool_id)
    if not _REGION_RE.match(region) or not pool or pool["region"] != region:
        raise SettingsError("Invalid COGNITO_REGION or COGNITO_USER_POOL_ID.")
    if not _CLIENT_RE.match(client_id):
        raise SettingsError("Invalid COGNITO_APP_CLIENT_ID.")
    return CognitoConfig(region=region, user_pool_id=pool_id, client_id=client_id)


def _parse_bool(name: str, raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise SettingsError(f"{name} must be true or false.")


def _parse_origin(origin: str, production: bool) -> str:
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    if (
        origin == "*"
        or parsed.scheme not in {"http", "https"}
        or not host
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise SettingsError(f"Invalid CORS origin: {origin!r}.")
    if production and (parsed.scheme != "https" or host in _LOCAL_HOSTS):
        raise SettingsError(
            "Production CORS origins must be https and not localhost."
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from the environment; anything unsafe raises."""
    env = os.environ if env is None else env

    api_env = (env.get("API_ENV") or "production").strip().lower()
    if api_env not in {"local", "production"}:
        raise SettingsError("API_ENV must be 'local' or 'production'.")
    production = api_env == "production"

    # No authentication exists yet, so execution must never be switchable on.
    if _parse_bool("PIPELINE_EXECUTION_ENABLED", env.get("PIPELINE_EXECUTION_ENABLED")):
        raise SettingsError(
            "Pipeline execution cannot be enabled: authentication and managed "
            "execution are not implemented."
        )

    enable_docs = _parse_bool("ENABLE_API_DOCS", env.get("ENABLE_API_DOCS"))
    if enable_docs and production:
        raise SettingsError("API docs can only be enabled when API_ENV=local.")

    raw_origins = [
        item.strip()
        for item in (env.get("CORS_ALLOWED_ORIGINS") or "").split(",")
        if item.strip()
    ]
    origins = tuple(dict.fromkeys(_parse_origin(o, production) for o in raw_origins))
    if not origins and not production:
        origins = LOCAL_DEV_ORIGINS

    cognito = _load_cognito(env)
    if production and cognito is None:
        raise SettingsError(
            "API_ENV=production requires COGNITO_REGION, COGNITO_USER_POOL_ID "
            "and COGNITO_APP_CLIENT_ID."
        )

    run_history = load_run_history_settings(env)
    if production and run_history.backend != "dynamodb":
        # A SQLite file is neither shared nor durable across tasks; never fall back to it.
        raise SettingsError(
            "API_ENV=production requires RUN_HISTORY_BACKEND=dynamodb and RUN_HISTORY_TABLE."
        )

    return Settings(
        api_env=api_env,
        cors_allowed_origins=origins,
        pipeline_execution_enabled=False,
        enable_api_docs=enable_docs,
        cognito=cognito,
        rate_limits=_load_rate_limits(env),
        run_history=run_history,
    )
