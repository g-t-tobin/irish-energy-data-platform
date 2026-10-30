#!/usr/bin/env python3

import os
import sys
from pathlib import Path

# The run-history table is built from the shared specification and schema in the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aws_cdk as cdk
from energy_config import (
    ACCOUNT,
    REGION,
    ConfigError,
    load_edge_config,
    load_reconciler_config,
    load_service_config,
    validate_deployment_account,
)
from stacks.data_lake_stack import DataLakeStack
from stacks.energy_api_registry_stack import EnergyApiRegistryStack
from stacks.energy_cognito_stack import EnergyCognitoStack
from stacks.energy_edge_stacks import EnergyApiEdgeStack, EnergyFrontendStack
from stacks.energy_pipeline_stack import EnergyPipelineStack
from stacks.energy_reconciler_stack import EnergyReconcilerStack
from stacks.energy_run_history_stack import EnergyRunHistoryStack
from stacks.energy_workflow_stack import EnergyWorkflowStack

app = cdk.App()

# Also set here because `python app.py` does not read cdk.json; a test keeps the two equal.
app.node.set_context("@aws-cdk/core:bootstrapQualifier", "energydev01")

# Pre-populated context: the VPC would otherwise look up the availability zones at synthesis, which
# needs the bootstrap lookup role (a broad read-only role) that the deployment identity must not use.
app.node.set_context(
    f"availability-zones:account={ACCOUNT}:region={REGION}", [f"{REGION}a", f"{REGION}b", f"{REGION}c"]
)

# Every stack, the data lake included, is pinned to the project's account and region; a different
# active account stops synthesis before any stack exists.
try:
    validate_deployment_account(os.environ)
except ConfigError as error:
    raise SystemExit(f"Configuration error: {error}")

DataLakeStack(
    app,
    "IrishEnergyDataLakeStack",
    bucket_name="irish-energy-data-platform-851725545966",
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

# Cognito authentication is independent of the API image and ECS service.
cognito_stack = EnergyCognitoStack(
    app,
    "IrishEnergyCognitoStack",
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

# Validate configuration first: this refuses any account other than the project's.
try:
    edge = load_edge_config(app.node.try_get_context, os.environ)
    service = load_service_config(app.node.try_get_context)
    reconciler = load_reconciler_config(app.node.try_get_context)
    if service is not None and edge is None:
        raise ConfigError("The API service needs both certificate ARNs (edge stack) as well.")
except ConfigError as error:
    raise SystemExit(f"Configuration error: {error}")

# The registry needs no certificates and is deployed first so the image can be pushed.
registry = EnergyApiRegistryStack(
    app,
    "IrishEnergyApiRegistryStack",
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

# Stateful run history: needs no certificates and is deployed before the API service.
run_history = EnergyRunHistoryStack(
    app,
    "IrishEnergyRunHistoryStack",
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

# Pipeline stage Lambda functions: no certificates needed; not invoked by anything yet.
pipeline = EnergyPipelineStack(
    app,
    "IrishEnergyPipelineStack",
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

# Standard workflow chaining the stage functions; nothing starts an execution.
workflow = EnergyWorkflowStack(
    app,
    "IrishEnergyWorkflowStack",
    table=run_history.table,
    pipeline_stack=pipeline,
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)

# Optional (-c reconcilerEnabled=true): the scheduled reconciler; its schedule is DISABLED by default.
if reconciler is not None:
    EnergyReconcilerStack(
        app,
        "IrishEnergyReconcilerStack",
        config=reconciler,
        table=run_history.table,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )

# Edge stacks are independent of the game and need both certificate ARNs
# (-c apiCertificateArn=... -c frontendCertificateArn=...); otherwise they are skipped.
if edge is None:
    print("Energy edge stacks skipped: certificate ARNs not provided.")
else:
    edge_env = cdk.Environment(account=edge.account, region=edge.region)
    if service is None:
        print("Energy API service skipped: image tag and Cognito IDs not provided.")
    api_edge = EnergyApiEdgeStack(
        app,
        "IrishEnergyApiEdgeStack",
        config=edge,
        service=service,
        repository=registry.repository if service else None,
        table=run_history.table if service else None,
        env=edge_env,
    )
    if service:
        api_edge.add_dependency(workflow)  # the task role names the state machine
    EnergyFrontendStack(app, "IrishEnergyFrontendStack", config=edge, env=edge_env)

app.synth()
