
#!/usr/bin/env python3

import os

import aws_cdk as cdk

from energy_config import ConfigError, load_edge_config
from stacks.data_lake_stack import DataLakeStack
from stacks.energy_edge_stacks import EnergyApiEdgeStack, EnergyFrontendStack


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

# Edge stacks are independent of the game and need both certificate ARNs
# (-c apiCertificateArn=... -c frontendCertificateArn=...); otherwise they are skipped.
try:
    edge = load_edge_config(app.node.try_get_context, os.environ)
except ConfigError as error:
    raise SystemExit(f"Configuration error: {error}")

if edge is None:
    print("Energy edge stacks skipped: certificate ARNs not provided.")
else:
    edge_env = cdk.Environment(account=edge.account, region=edge.region)
    EnergyApiEdgeStack(app, "IrishEnergyApiEdgeStack", config=edge, env=edge_env)
    EnergyFrontendStack(app, "IrishEnergyFrontendStack", config=edge, env=edge_env)

app.synth()
