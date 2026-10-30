"""Pipeline execution stays disabled even though the state machine ARN is now known to the API."""

from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from src import api  # noqa: E402
from src.auth import JwksKeyProvider  # noqa: E402
from src.run_store import RunStore  # noqa: E402
from src.settings import Settings, SettingsError, load_settings  # noqa: E402
from tests import auth_helpers as h  # noqa: E402

ARN = "arn:aws:states:eu-west-1:851725545966:stateMachine:irish-energy-pipeline"
LOCAL = {"API_ENV": "local"}
OPERATOR = h.auth_header(h.token(groups=["operator"]))


def test_a_state_machine_arn_does_not_enable_execution():
    settings = load_settings({**LOCAL, "PIPELINE_STATE_MACHINE_ARN": ARN})

    assert settings.pipeline_state_machine_arn == ARN
    assert settings.pipeline_execution_enabled is False


@pytest.mark.parametrize("flag", ["true", "1", "yes", "TRUE", "on"])
def test_the_execution_switch_is_still_refused_with_or_without_an_arn(flag):
    for extra in ({}, {"PIPELINE_STATE_MACHINE_ARN": ARN}):
        with pytest.raises(SettingsError, match="cannot be enabled"):
            load_settings({**LOCAL, "PIPELINE_EXECUTION_ENABLED": flag, **extra})


@pytest.mark.parametrize(
    "arn",
    ["not-an-arn", "arn:aws:states:eu-west-1:851725545966:execution:m:run", "arn:aws:states:::stateMachine:m",
     "arn:aws:lambda:eu-west-1:851725545966:function:f", ARN + "/../x", "arn:aws:states:eu-west-1:1:stateMachine:m"],
)
def test_a_malformed_arn_fails_startup_safely(arn):
    with pytest.raises(SettingsError, match="PIPELINE_STATE_MACHINE_ARN"):
        load_settings({**LOCAL, "PIPELINE_STATE_MACHINE_ARN": arn})


def test_the_arn_is_optional_and_unset_by_default():
    assert load_settings(LOCAL).pipeline_state_machine_arn is None
    assert Settings().pipeline_state_machine_arn is None and Settings().pipeline_execution_enabled is False


def test_operator_post_is_503_and_no_aws_client_or_run_is_created(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(api, "_store", store)
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=h.jwks)
    settings = Settings(api_env="local", cognito=h.CONFIG, pipeline_state_machine_arn=ARN)

    def refuse(*args, **kwargs):
        raise AssertionError("AWS client created")

    with patch("boto3.client", side_effect=refuse), patch("boto3.resource", side_effect=refuse):
        client = TestClient(api.create_app(settings, key_provider=provider))
        response = client.post("/api/runs", json={"days_back": 2}, headers=OPERATOR)

    assert response.status_code == 503
    assert response.json() == {"detail": "Pipeline execution is disabled."}
    assert store.list_runs() == []


def test_the_api_does_not_import_or_construct_the_step_functions_executor():
    source = (Path(api.__file__)).read_text()

    for name in ("executor_stepfunctions", "StepFunctionsExecutor", "start_execution", "stepfunctions"):
        assert name not in source
