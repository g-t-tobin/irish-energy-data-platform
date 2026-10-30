import pytest

from src.settings import (
    LOCAL_DEV_ORIGINS,
    CognitoConfig,
    RateLimits,
    Settings,
    SettingsError,
    load_settings,
)


def test_defaults_are_safe():
    settings = load_settings(COGNITO_ENV)

    assert settings == Settings(
        api_env="production",
        cors_allowed_origins=(),
        pipeline_execution_enabled=False,
        enable_api_docs=False,
        cognito=CognitoConfig(
            region="eu-west-1",
            user_pool_id="eu-west-1_AbC123xyz",
            client_id="abcdefghij1234567890",
        ),
        rate_limits=RateLimits(),
    )


def test_local_env_defaults_to_dev_origins_only():
    settings = load_settings({"API_ENV": "local"})

    assert settings.cors_allowed_origins == LOCAL_DEV_ORIGINS
    assert settings.pipeline_execution_enabled is False
    assert settings.enable_api_docs is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
@pytest.mark.parametrize("api_env", ["local", "production"])
def test_execution_can_never_be_enabled(value, api_env):
    with pytest.raises(SettingsError, match="cannot be enabled"):
        load_settings({"API_ENV": api_env, "PIPELINE_EXECUTION_ENABLED": value})


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off"])
def test_explicitly_disabled_values_are_accepted(value):
    settings = load_settings({"API_ENV": "local", "PIPELINE_EXECUTION_ENABLED": value})

    assert settings.pipeline_execution_enabled is False


@pytest.mark.parametrize("name", ["PIPELINE_EXECUTION_ENABLED", "ENABLE_API_DOCS"])
def test_ambiguous_boolean_is_rejected(name):
    with pytest.raises(SettingsError, match=name):
        load_settings({name: "maybe"})


def test_invalid_api_env_is_rejected():
    with pytest.raises(SettingsError, match="API_ENV"):
        load_settings({"API_ENV": "staging"})


def test_docs_only_in_local():
    assert load_settings({"API_ENV": "local", "ENABLE_API_DOCS": "true"}).enable_api_docs
    with pytest.raises(SettingsError, match="docs"):
        load_settings({"API_ENV": "production", "ENABLE_API_DOCS": "true"})
    with pytest.raises(SettingsError, match="docs"):
        load_settings({"ENABLE_API_DOCS": "true"})


def test_production_origins_must_be_https_and_not_local():
    ok = load_settings(
        {
            **COGNITO_ENV,
            "CORS_ALLOWED_ORIGINS": "https://app.example.com/, https://app.example.com",
        }
    )
    assert ok.cors_allowed_origins == ("https://app.example.com",)

    for bad in (
        "http://app.example.com",
        "https://localhost:5173",
        "https://127.0.0.1",
        "*",
    ):
        with pytest.raises(SettingsError):
            load_settings({"CORS_ALLOWED_ORIGINS": bad})


@pytest.mark.parametrize(
    "bad", ["*", "app.example.com", "https://app.example.com/path", "ftp://x.com", "https://"]
)
@pytest.mark.parametrize("api_env", ["local", "production"])
def test_malformed_origins_rejected_in_every_environment(bad, api_env):
    with pytest.raises(SettingsError):
        load_settings({"API_ENV": api_env, "CORS_ALLOWED_ORIGINS": bad})


def test_local_may_use_explicit_localhost_origin():
    settings = load_settings(
        {"API_ENV": "local", "CORS_ALLOWED_ORIGINS": "http://localhost:3000"}
    )

    assert settings.cors_allowed_origins == ("http://localhost:3000",)


COGNITO_ENV = {
    "COGNITO_REGION": "eu-west-1",
    "COGNITO_USER_POOL_ID": "eu-west-1_AbC123xyz",
    "COGNITO_APP_CLIENT_ID": "abcdefghij1234567890",
}


def test_local_without_cognito_is_unconfigured_not_disabled():
    assert load_settings({"API_ENV": "local"}).cognito is None


@pytest.mark.parametrize("env", [{}, {"API_ENV": "production"}])
def test_production_requires_cognito(env):
    with pytest.raises(SettingsError, match="requires COGNITO"):
        load_settings(env)


def test_partial_cognito_in_production_still_fails():
    with pytest.raises(SettingsError, match="Incomplete Cognito"):
        load_settings({"COGNITO_REGION": "eu-west-1"})


def test_rate_limit_defaults_and_overrides():
    defaults = load_settings(COGNITO_ENV).rate_limits
    custom = load_settings(
        {**COGNITO_ENV, "RATE_LIMIT_READ_PER_MINUTE": "10", "RATE_LIMIT_WRITE_PER_HOUR": "2"}
    ).rate_limits

    assert defaults == RateLimits()
    assert (custom.read_per_minute, custom.write_per_hour) == (10, 2)
    assert custom.analytics_per_minute == defaults.analytics_per_minute


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", "100001"])
def test_invalid_rate_limits_are_rejected(value):
    with pytest.raises(SettingsError, match="RATE_LIMIT_READ_PER_MINUTE"):
        load_settings({**COGNITO_ENV, "RATE_LIMIT_READ_PER_MINUTE": value})


def test_cognito_config_and_derived_urls():
    cognito = load_settings(COGNITO_ENV).cognito

    assert cognito.issuer == "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_AbC123xyz"
    assert cognito.jwks_url == cognito.issuer + "/.well-known/jwks.json"
    assert cognito.client_id == "abcdefghij1234567890"


@pytest.mark.parametrize("missing", list(COGNITO_ENV))
def test_partial_cognito_config_is_rejected(missing):
    env = {k: v for k, v in COGNITO_ENV.items() if k != missing}

    with pytest.raises(SettingsError, match="Incomplete Cognito"):
        load_settings(env)


@pytest.mark.parametrize(
    "override",
    [
        {"COGNITO_REGION": "us-east-1"},
        {"COGNITO_REGION": "not a region"},
        {"COGNITO_USER_POOL_ID": "no-underscore"},
        {"COGNITO_USER_POOL_ID": "eu-west-1_bad/pool"},
        {"COGNITO_APP_CLIENT_ID": "short"},
        {"COGNITO_APP_CLIENT_ID": "has spaces in it 12345"},
    ],
)
def test_invalid_cognito_values_are_rejected(override):
    with pytest.raises(SettingsError):
        load_settings({**COGNITO_ENV, **override})


def test_run_history_defaults_to_local_sqlite():
    from src.settings import RunHistorySettings

    assert load_settings({"API_ENV": "local"}).run_history == RunHistorySettings()


def test_dynamodb_backend_needs_a_valid_table_and_region():
    ok = load_settings(
        {**COGNITO_ENV, "RUN_HISTORY_BACKEND": "dynamodb", "RUN_HISTORY_TABLE": "irish-energy-runs"}
    ).run_history

    assert (ok.backend, ok.table, ok.region) == ("dynamodb", "irish-energy-runs", "eu-west-1")
    for bad in (
        {"RUN_HISTORY_BACKEND": "dynamodb"},
        {"RUN_HISTORY_BACKEND": "dynamodb", "RUN_HISTORY_TABLE": "bad table!"},
        {"RUN_HISTORY_BACKEND": "dynamodb", "RUN_HISTORY_TABLE": "ok-table", "RUN_HISTORY_REGION": "nowhere"},
        {"RUN_HISTORY_BACKEND": "redis"},
    ):
        with pytest.raises(SettingsError):
            load_settings({**COGNITO_ENV, **bad})


def test_enabling_execution_is_still_impossible_with_shared_history():
    with pytest.raises(SettingsError, match="cannot be enabled"):
        load_settings(
            {**COGNITO_ENV, "PIPELINE_EXECUTION_ENABLED": "true", "RUN_HISTORY_BACKEND": "dynamodb",
             "RUN_HISTORY_TABLE": "irish-energy-runs"}
        )
