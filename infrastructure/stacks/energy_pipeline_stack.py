"""Lambda container functions for the four pipeline stages (Phase 9.4).

One shared image (Dockerfile.lambda) built as a CDK image asset; each stage selects its handler
with a command override and has its own execution role. Nothing here invokes a function, and the
Step Functions state machine that chains them is a later phase. Publishing the image asset is a
deployment operation; synthesis makes no AWS calls.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Size, Stack, Tags
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from energy_config import (
    ACCOUNT,
    BRONZE_PREFIX,
    DATA_LAKE_BUCKET,
    GLUE_DATABASE,
    GLUE_TABLE,
    GOLD_PREFIX,
    LOG_RETENTION_DAYS,
    PIPELINE_FUNCTION_PREFIX,
    PIPELINE_STAGES,
    REGION,
    SILVER_PREFIX,
    PipelineStage,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_OBJECTS = f"arn:aws:s3:::{DATA_LAKE_BUCKET}"


def _s3(action: str, sid: str, prefix: str) -> iam.PolicyStatement:
    return iam.PolicyStatement(
        sid=sid, actions=[action], resources=[f"{_OBJECTS}/{prefix}*"]
    )


def _glue_registration() -> iam.PolicyStatement:
    base = f"arn:aws:glue:{REGION}:{ACCOUNT}"
    return iam.PolicyStatement(
        sid="RegisterGoldPartitions",
        actions=["glue:GetTable", "glue:CreatePartition"],
        resources=[
            f"{base}:catalog",
            f"{base}:database/{GLUE_DATABASE}",
            f"{base}:table/{GLUE_DATABASE}/{GLUE_TABLE}",
        ],
    )


# Statements by what each handler actually calls (see src/pipeline_stages.py).
_PERMISSIONS = {
    "bronze": lambda: [_s3("s3:PutObject", "WriteBronzeObjects", BRONZE_PREFIX)],
    "silver": lambda: [
        _s3("s3:GetObject", "ReadBronzeObjects", BRONZE_PREFIX),
        _s3("s3:PutObject", "WriteSilverObjects", SILVER_PREFIX),
    ],
    "gold": lambda: [
        _s3("s3:GetObject", "ReadSilverObjects", SILVER_PREFIX),
        _s3("s3:PutObject", "WriteGoldObjects", GOLD_PREFIX),
    ],
    "glue_registration": lambda: [_glue_registration()],
}


class EnergyPipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Project", "irish-energy-data-platform")
        Tags.of(self).add("Component", "pipeline")

        image = ecr_assets.DockerImageAsset(
            self,
            "PipelineImage",
            directory=str(REPOSITORY_ROOT),
            file="Dockerfile.lambda",
            platform=ecr_assets.Platform.LINUX_AMD64,
            # Applied after the API's root .dockerignore (an allow-list); keep in step with
            # Dockerfile.lambda.dockerignore, which CDK does not read.
            exclude=["!requirements-lambda.txt", "requirements-api.txt"],
        )
        self.functions: dict[str, lambda_.Function] = {}
        for stage in PIPELINE_STAGES:
            self.functions[stage.key] = self._function(stage, image)

    def _function(self, stage: PipelineStage, image: ecr_assets.DockerImageAsset) -> lambda_.Function:
        name = f"{PIPELINE_FUNCTION_PREFIX}-{stage.key.replace('_', '-')}"
        label = "".join(part.capitalize() for part in stage.key.split("_"))
        log_group = logs.LogGroup(
            self,
            f"{label}Logs",
            log_group_name=f"/aws/lambda/{name}",
            retention=logs.RetentionDays.ONE_MONTH if LOG_RETENTION_DAYS == 30 else logs.RetentionDays(LOG_RETENTION_DAYS),
            removal_policy=RemovalPolicy.DESTROY,
        )
        role = iam.Role(
            self,
            f"{label}Role",
            assumed_by=iam.ServicePrincipal(
                "lambda.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": ACCOUNT}},
            ),
            description=f"Execution role of the {stage.key} pipeline stage",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnLogs",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[log_group.log_group_arn],
            )
        )
        for statement in _PERMISSIONS[stage.key]():
            role.add_to_policy(statement)

        function = lambda_.Function(
            self,
            f"{label}Function",
            function_name=name,
            code=lambda_.Code.from_ecr_image(
                image.repository, tag_or_digest=image.image_tag, cmd=[stage.handler]
            ),
            handler=lambda_.Handler.FROM_IMAGE,
            runtime=lambda_.Runtime.FROM_IMAGE,
            architecture=lambda_.Architecture.X86_64,
            role=role,
            timeout=Duration.seconds(stage.timeout_seconds),
            memory_size=stage.memory_mib,
            ephemeral_storage_size=Size.mebibytes(stage.ephemeral_mib),
            environment={"S3_BUCKET": DATA_LAKE_BUCKET},
            log_group=log_group,
        )
        CfnOutput(
            self,
            f"{label}FunctionArn",
            value=function.function_arn,
            export_name=f"IrishEnergyPipeline-{label}FunctionArn",
        )
        return function
