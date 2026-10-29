
#!/usr/bin/env python3

import os

import aws_cdk as cdk

from stacks.data_lake_stack import DataLakeStack


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

app.synth()
