"""ECR repository for the energy API image. Independent of the game's registries."""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ecr as ecr
from constructs import Construct

from energy_config import ECR_REPOSITORY_NAME


class EnergyApiRegistryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Project", "irish-energy-data-platform")
        Tags.of(self).add("Component", "api-registry")

        self.repository = ecr.Repository(
            self,
            "ApiRepository",
            repository_name=ECR_REPOSITORY_NAME,
            image_scan_on_push=True,
            encryption=ecr.RepositoryEncryption.AES_256,
            # Tags cannot be overwritten, so a deployed tag always means the same image.
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    rule_priority=1,
                    description="Remove untagged images after 14 days",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=Duration.days(14),
                ),
                ecr.LifecycleRule(
                    rule_priority=2,
                    description="Keep the 30 newest images",
                    tag_status=ecr.TagStatus.ANY,
                    max_image_count=30,
                ),
            ],
        )
        CfnOutput(self, "RepositoryUri", value=self.repository.repository_uri)
        CfnOutput(self, "RepositoryName", value=self.repository.repository_name)
