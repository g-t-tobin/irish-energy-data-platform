"""Phase 9.7.1: account pinning, hostname and credential hygiene, and no new way to start a run."""

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src import api
from src.settings import Settings, SettingsError, load_settings
from tests.test_energy_edge_infrastructure import ConfigError, run_app

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "851725545966"
GIT = shutil.which("git")


def validate_deployment_account(env):
    # infrastructure/ is on sys.path once the edge test module is imported
    return importlib.import_module("energy_config").validate_deployment_account(env)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, *args], cwd=ROOT, capture_output=True, text=True, check=False)


needs_git = pytest.mark.skipif(GIT is None or not (ROOT / ".git").exists(), reason="not a git checkout")


# -- every stack is pinned to the project account and region -------------------------------------------


def test_all_stacks_including_the_data_lake_are_pinned_to_the_project_environment(tmp_path):
    result, templates = run_app(tmp_path)

    assert result.returncode == 0, result.stderr
    artifacts = json.loads((tmp_path / "manifest.json").read_text())["artifacts"]
    stacks = {name: a["environment"] for name, a in artifacts.items() if name.endswith("Stack")}
    assert "IrishEnergyDataLakeStack" in stacks and len(stacks) == len(templates) == 6
    assert set(stacks.values()) == {f"aws://{ACCOUNT}/eu-west-1"}  # no unknown-account stack


def test_the_active_profile_region_cannot_move_the_stacks(tmp_path):
    result, _ = run_app(tmp_path, env_extra={"CDK_DEFAULT_REGION": "us-east-1", "CDK_DEFAULT_ACCOUNT": ACCOUNT})

    artifacts = json.loads((tmp_path / "manifest.json").read_text())["artifacts"]
    assert result.returncode == 0, result.stderr
    assert {a["environment"] for n, a in artifacts.items() if n.endswith("Stack")} == {f"aws://{ACCOUNT}/eu-west-1"}


def test_the_correct_account_and_region_synthesize_every_configuration(tmp_path):
    result, templates = run_app(tmp_path, env_extra={"CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": "eu-west-1"})

    assert result.returncode == 0, result.stderr
    assert len(templates) == 6


BAD_ACCOUNTS = {
    "wrong": "111111111111",
    "all zeros": "000000000000",
    "off by one": "851725545967",
    "not a number": "abc",
    "padded": f" {ACCOUNT} ",
    "empty": "",
    "space": " ",
    "tab and newline": "\t\n",
    "missing": None,
}


@pytest.mark.parametrize("account", list(BAD_ACCOUNTS.values()), ids=list(BAD_ACCOUNTS))
@pytest.mark.parametrize("ctx", [None, {"reconcilerEnabled": "true"}], ids=["default", "reconciler"])
def test_a_missing_blank_or_different_account_stops_synthesis_before_any_stack_exists(tmp_path, account, ctx):
    result, templates = run_app(tmp_path, ctx, env_extra={"CDK_DEFAULT_ACCOUNT": account})

    output = result.stdout + result.stderr
    assert result.returncode != 0 and templates == []
    assert "Configuration error" in output and ACCOUNT in output and "CDK_DEFAULT_ACCOUNT" in output
    assert list(tmp_path.glob("*.template.json")) == []


@pytest.mark.parametrize("account", [None, "", " ", "\t", "111111111111", f"{ACCOUNT} "])
def test_validate_deployment_account_rejects_anything_but_the_exact_account(account):
    env = {} if account is None else {"CDK_DEFAULT_ACCOUNT": account}

    with pytest.raises(ConfigError, match=ACCOUNT):
        validate_deployment_account(env)


def test_validate_deployment_account_accepts_the_exact_account_whatever_the_region():
    validate_deployment_account({"CDK_DEFAULT_ACCOUNT": ACCOUNT})
    validate_deployment_account({"CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": "us-east-1"})


# -- hostnames, credentials and local files -----------------------------------------------------------------


def test_game_hostnames_appear_in_the_energy_configuration_only_as_the_reserved_list():
    pattern = re.compile(r"""(?<![\w-])(api|play|www)\.(\{DOMAIN\}|ai-autofy-general-server\.com)""")
    hits = []
    roots = [ROOT / "infrastructure", ROOT / "src", ROOT / "frontend" / "src"]
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or "cdk.out" in path.parts or "node_modules" in path.parts:
                continue
            if path.name.endswith((".test.ts", ".test.tsx")):
                continue  # tests state the forbidden hostnames on purpose
            hits += [(path.relative_to(ROOT).as_posix(), m.group(0)) for m in pattern.finditer(path.read_text())]
    assert {path for path, _ in hits} == {"infrastructure/energy_config.py"}
    assert {hit for _, hit in hits} == {"api.{DOMAIN}", "play.{DOMAIN}", "www.{DOMAIN}"}


@needs_git
def test_credential_and_key_filenames_are_ignored_and_not_tracked_or_in_history():
    for name in ("aws-access-key.txt", "irish-energy-data-platform-public-key-git"):
        assert git("check-ignore", "-q", name).returncode == 0, name
        assert git("ls-files", "--error-unmatch", name).returncode != 0, name  # not in the index
        assert git("log", "--all", "--oneline", "--", name).stdout.strip() == "", name  # never committed


@needs_git
def test_only_the_environment_template_is_tracked_among_env_and_secret_looking_files():
    tracked = git("ls-files").stdout.splitlines()
    suspicious = [f for f in tracked if re.search(r"(^|/)\.env($|\.)|credential|secret|\.pem$|id_rsa|access-key", f, re.IGNORECASE)]

    assert suspicious == ["frontend/.env.example"]
    values = [line.split("=", 1)[1] for line in (ROOT / "frontend/.env.example").read_text().splitlines()
              if re.match(r"^[A-Z_]+=", line)]
    assert all(not re.search(r"AKIA|ASIA|secret|token", v, re.IGNORECASE) for v in values)


# -- execution stays disabled and nothing new can start one --------------------------------------------------


@pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes", "on"])
def test_enabling_pipeline_execution_requires_security_configuration(flag):
    with pytest.raises(SettingsError, match="requires Cognito authentication"):
        load_settings({"API_ENV": "local", "PIPELINE_EXECUTION_ENABLED": flag})


def test_execution_is_disabled_by_default():
    assert Settings().pipeline_execution_enabled is False
    assert load_settings({"API_ENV": "local"}).pipeline_execution_enabled is False


def test_only_the_executor_module_can_start_an_execution():
    starters = {p.relative_to(ROOT).as_posix() for p in (ROOT / "src").rglob("*.py") if "start_execution" in p.read_text()}
    reconciler_files = ("src/reconciliation.py", "src/reconcile_runs.py")

    assert starters == {"src/executor_stepfunctions.py"}
    assert all("start_execution" not in (ROOT / f).read_text() for f in reconciler_files)


def test_no_api_route_can_invoke_a_pipeline_function():
    paths = set(api.create_app(Settings(api_env="local")).openapi()["paths"])
    source = (ROOT / "src" / "api.py").read_text()

    assert not [p for p in paths if re.search(r"lambda|invoke|stage|execute", p, re.IGNORECASE)]
    for forbidden in ('client("lambda"', "client('lambda'", ".invoke(", "pipeline_stages", "executor_stepfunctions"):
        assert forbidden not in source
