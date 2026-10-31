"""Static checks of the Dockerfile, build context and image dependencies, plus the health probe."""

import ast
import fnmatch
import http.server
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src import healthcheck

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text()
REQUIREMENTS = (REPO / "requirements-api.txt").read_text()


def instructions(name):
    return [line.split(None, 1)[1] for line in DOCKERFILE.splitlines() if line.upper().startswith(name + " ")]


# Dockerfile


def test_base_image_is_a_pinned_supported_slim_python():
    bases = instructions("FROM")

    assert bases == ["python:3.13-slim-bookworm AS builder", "python:3.13-slim-bookworm"]


def test_runs_as_a_numeric_non_root_user():
    users = instructions("USER")

    assert users == ["10001:10001"]
    assert "useradd --system --uid 10001" in DOCKERFILE and "nologin" in DOCKERFILE
    assert DOCKERFILE.splitlines().index("USER 10001:10001") > max(
        i for i, line in enumerate(DOCKERFILE.splitlines()) if line.startswith(("RUN", "COPY"))
    )  # nothing runs as root after the switch


def test_only_requirements_and_source_are_copied():
    copies = [c.replace("--from=builder ", "") for c in instructions("COPY")]

    assert copies == ["requirements-api.txt .", "/opt/venv /opt/venv", "src ./src"]
    assert "COPY . " not in DOCKERFILE


def test_installs_only_the_production_requirements_file():
    runs = [r for r in instructions("RUN") if "pip" in r]

    assert runs == ["python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements-api.txt"]
    assert "requirements-dev" not in DOCKERFILE and "pytest" not in DOCKERFILE


def test_serves_with_uvicorn_on_all_interfaces_and_a_configurable_default_port():
    command = instructions("CMD")[0]

    assert "uvicorn src.api:app" in command
    assert "--host 0.0.0.0" in command and "--port ${PORT}" in command
    assert "--proxy-headers" in command and "--reload" not in command
    assert "PORT=8000" in DOCKERFILE and instructions("EXPOSE") == ["8000"]


def test_health_check_is_unauthenticated_stdlib_only():
    check = DOCKERFILE[DOCKERFILE.index("HEALTHCHECK") :].split("\n# ")[0]

    assert instructions("HEALTHCHECK")
    assert '"python", "-m", "src.healthcheck"' in check
    assert "curl" not in DOCKERFILE


def test_no_credentials_or_production_switches_are_baked_in():
    env_block = "\n".join(
        line for line in DOCKERFILE.splitlines() if line.strip().startswith(("ENV", "ARG", "PORT", "PYTHON", "PATH", "RUN_HISTORY"))
    ).upper()

    for forbidden in ("AWS_", "SECRET", "TOKEN", "PASSWORD", "COGNITO", "PIPELINE_EXECUTION", "API_ENV"):
        assert forbidden not in env_block


# Dependencies


IMPORT_TO_REQUIREMENT = {"jwt": "pyjwt", "fastapi": "fastapi", "boto3": "boto3", "pandas": "pandas"}
# Imported only through other packages, but needed at run time.
TRANSITIVE = {"pydantic": "fastapi", "botocore": "boto3", "starlette": "fastapi"}
API_MODULES = [
    "api", "auth", "analytics", "ratelimit", "logging_config", "run_store", "dynamodb_run_store",
    "executor", "executor_stepfunctions", "run_store_factory", "run_history_schema", "settings", "healthcheck",
]


def requirement_names():
    names = set()
    for line in REQUIREMENTS.splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[\[=<>~! ]", line, maxsplit=1)[0].lower())
    return names


def third_party_imports():
    found = set()
    for module in API_MODULES:
        tree = ast.parse((REPO / "src" / f"{module}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {m for m in found if m not in sys.stdlib_module_names and m != "src"}


def test_every_third_party_import_of_the_api_is_covered_by_a_requirement():
    names = requirement_names()

    for module in third_party_imports():
        owner = IMPORT_TO_REQUIREMENT.get(module) or TRANSITIVE.get(module)
        assert owner in names, f"{module} is imported by the API but not provided"


def test_boto3_pandas_and_pyarrow_are_genuinely_required():
    imports = third_party_imports()

    assert {"boto3", "pandas"} <= imports  # analytics reads S3 and builds frames
    assert "read_parquet" in (REPO / "src" / "analytics.py").read_text()  # which needs a parquet engine
    assert "pyarrow" in requirement_names()


def test_requirements_are_pinned_and_free_of_development_packages():
    for line in REQUIREMENTS.splitlines():
        line = line.split("#")[0].strip()
        if line:
            assert "==" in line, f"{line} is not pinned"
    names = requirement_names()
    assert names == {"fastapi", "uvicorn", "pyjwt", "boto3", "pandas", "pyarrow"}
    assert "[standard]" not in REQUIREMENTS
    for dev in ("pytest", "httpx", "moto", "requests", "ruff"):
        assert dev not in names


def test_development_requirements_build_on_the_production_file():
    assert "-r requirements-api.txt" in (REPO / "requirements-dev.txt").read_text()


# Build context


def context_files():
    skipped = {".git", ".venv", "node_modules", "dist", "cdk.out"}
    rules = [line.strip() for line in (REPO / ".dockerignore").read_text().splitlines() if line.strip() and not line.startswith("#")]
    included = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skipped]
        for name in files:
            path = (Path(root) / name).relative_to(REPO).as_posix()
            keep = False
            for rule in rules:  # last matching rule wins, as in Docker
                negate = rule.startswith("!")
                pattern = rule.lstrip("!").rstrip("/")
                parts = path.split("/")
                if pattern.startswith("**/"):
                    hit = any(fnmatch.fnmatch(part, pattern[3:]) for part in parts)
                elif pattern == "*":
                    hit = True
                else:
                    hit = path == pattern or path.startswith(pattern + "/")
                if hit:
                    keep = negate
            if keep:
                included.append(path)
    return included


def test_build_context_contains_only_requirements_and_source():
    files = context_files()

    assert "requirements-api.txt" in files
    assert files and all(f == "requirements-api.txt" or f.startswith("src/") for f in files)


def test_build_context_excludes_tests_secrets_databases_caches_and_other_projects():
    files = context_files()
    joined = "\n".join(files)

    for forbidden in ("tests/", "frontend/", "docs/", "infrastructure/", ".local", ".env", "aws-access-key", "key-git", ".sqlite3", "__pycache__", ".pyc", "requirements-dev"):
        assert forbidden not in joined


# Health probe


class Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status if self.path == "/health" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Handler.status = 200
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()


def test_probe_passes_on_200_and_fails_otherwise(server):
    assert healthcheck.check(server) is True
    assert healthcheck.check(server, "/other") is False
    Handler.status = 503
    assert healthcheck.check(server) is False


def test_probe_fails_when_nothing_is_listening():
    unused = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = unused.server_address[1]
    unused.server_close()

    assert healthcheck.check(port, timeout=1) is False


def test_probe_main_reads_the_port_and_path_from_the_environment(server, monkeypatch):
    monkeypatch.setenv("PORT", str(server))
    assert healthcheck.main() == 0
    monkeypatch.setenv("HEALTH_CHECK_PATH", "/missing")
    assert healthcheck.main() == 1
    monkeypatch.setenv("HEALTH_CHECK_PATH", "/health")
    monkeypatch.setenv("PORT", "1")
    assert healthcheck.main() == 1


def test_the_health_route_itself_needs_no_token():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src import api
    from src.settings import Settings

    client = TestClient(api.create_app(Settings(api_env="local")))

    assert client.get("/health").status_code == 200
    health = next(r for r in api.router.routes if getattr(r, "path", "") == "/health")
    assert not health.dependant.dependencies and not health.dependencies


# Optional real build: RUN_DOCKER_TESTS=1 pytest tests/test_container_image.py


@pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="set RUN_DOCKER_TESTS=1 to build the image")
def test_image_builds_and_runs_as_non_root_without_dev_files():
    tag = "irish-energy-api:test"
    subprocess.run(["docker", "build", "--load", "--platform", "linux/amd64", "-t", tag, str(REPO)], check=True, timeout=900)
    listing = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "id -u; ls -A /app; ls /app/tests /app/.env 2>&1"],
        capture_output=True, text=True, timeout=120, check=False,
    ).stdout

    assert listing.splitlines()[0] == "10001"
    assert "src" in listing and "No such file" in listing
