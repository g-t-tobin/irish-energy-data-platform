"""DynamoDB run-history table for the energy API, built from the Phase 6 specification.

Stateful and independent of the service stack: it is retained on deletion, has
deletion protection and point-in-time recovery, and is deployed before the API.
"""

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

from energy_config import RUN_HISTORY_TABLE_NAME
from infrastructure.run_control import spec
from src import run_history_schema as schema


class EnergyRunHistoryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Project", "irish-energy-data-platform")
        Tags.of(self).add("Component", "run-history")

        definition = spec.table_definition(RUN_HISTORY_TABLE_NAME)
        string = dynamodb.AttributeType.STRING
        self.table = dynamodb.Table(
            self,
            "RunHistoryTable",
            table_name=definition["TableName"],
            partition_key=dynamodb.Attribute(name=schema.PARTITION_KEY, type=string),
            sort_key=dynamodb.Attribute(name=schema.SORT_KEY, type=string),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # AWS managed KMS key (alias/aws/dynamodb): encrypted at rest, no key to pay for or manage.
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=definition["PointInTimeRecoveryEnabled"]
            ),
            deletion_protection=definition["DeletionProtectionEnabled"],
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute=definition["TimeToLiveSpecification"]["AttributeName"],
        )
        self.table.add_global_secondary_index(
            index_name=schema.LIST_INDEX,
            partition_key=dynamodb.Attribute(name=schema.LIST_INDEX_PARTITION_KEY, type=string),
            sort_key=dynamodb.Attribute(name=schema.LIST_INDEX_SORT_KEY, type=string),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        CfnOutput(self, "RunHistoryTableName", value=self.table.table_name)
        CfnOutput(self, "RunHistoryTableArn", value=self.table.table_arn)
