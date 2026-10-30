
#!/usr/bin/env python3

import os
import sys
from pathlib import Path

# The run-history table is built from the shared specification and schema in the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aws_cdk as cdk  # noqa: E402

from energy_config import ACCOUNT, REGION, ConfigError, load_edge_config, load_service_config  # noqa: E402
from stacks.data_lake_stack import DataLakeStack  # noqa: E402
from stacks.energy_api_registry_stack import EnergyApiRegistryStack  # noqa: E402
from stacks.energy_run_history_stack import EnergyRunHistoryStack  # noqa: E402
from stacks.energy_edge_stacks import EnergyApiEdgeStack, EnergyFrontendStack  # noqa: E402


app = cdk.App()

account = os.environ.get("CDK_DEFAULT_ACCOUNT")
region = os.environ.get("CDK_DEFAULT_REGION", "eu-west-1")

DataLakeStack(
    app,
    "IrishEnergyDataLakeStack",
    bucket_name="irish-energy-data-platform-851725545966",
    env=cdk.Environment(
        account=account,
        region=region,
    ),
)

# Validate configuration first: this refuses any account other than the project's.
try:
    edge = load_edge_config(app.node.try_get_context, os.environ)
    service = load_service_config(app.node.try_get_context)
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

# Edge stacks are independent of the game and need both certificate ARNs
# (-c apiCertificateArn=... -c frontendCertificateArn=...); otherwise they are skipped.
if edge is None:
    print("Energy edge stacks skipped: certificate ARNs not provided.")
else:
    edge_env = cdk.Environment(account=edge.account, region=edge.region)
    if service is None:
        print("Energy API service skipped: image tag and Cognito IDs not provided.")
    EnergyApiEdgeStack(
        app,
        "IrishEnergyApiEdgeStack",
        config=edge,
        service=service,
        repository=registry.repository if service else None,
        table=run_history.table if service else None,
        env=edge_env,
    )
    EnergyFrontendStack(app, "IrishEnergyFrontendStack", config=edge, env=edge_env)

app.synth()
