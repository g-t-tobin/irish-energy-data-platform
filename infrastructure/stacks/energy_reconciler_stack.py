"""Optional scheduled run reconciler (Phase 9.6.1). Synthesised only with -c reconcilerEnabled=true.

A small zip-packaged Lambda (src/reconcile_runs.py) plus an EventBridge Scheduler schedule that is
DISABLED unless explicitly enabled. Its role can read executions of this one state machine and
read, update and delete items in the run-history table; it has no StartExecution, PutItem,
transaction, S3, Glue, Athena, Cognito, IAM or ECS permission.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, IgnoreMode, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct
from energy_config import (
    ACCOUNT,
    LOG_RETENTION_DAYS,
    RECONCILER_FUNCTION_NAME,
    RECONCILER_MEMORY_MIB,
    RECONCILER_RATE_MINUTES,
    RECONCILER_SCHEDULE_NAME,
    RECONCILER_TIMEOUT_SECONDS,
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_ARN,
    STATE_MACHINE_EXECUTION_ARN_PATTERN,
    ReconcilerConfig,
)

from src import run_history_schema as schema

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# Docker-style rules: only src/ goes into the zip.
PACKAGE_RULES = ["*", "!src/", "**/__pycache__", "**/*.pyc", "**/*.sqlite3*", "**/.env*"]


class EnergyReconcilerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: ReconcilerConfig,
                 table: dynamodb.ITable, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Project", "irish-energy-data-platform")
        Tags.of(self).add("Component", "run-reconciler")

        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/lambda/{RECONCILER_FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_MONTH if LOG_RETENTION_DAYS == 30 else logs.RetentionDays(LOG_RETENTION_DAYS),
            removal_policy=RemovalPolicy.DESTROY,
        )
        role = iam.Role(
            self,
            "Role",
            assumed_by=iam.ServicePrincipal(
                "lambda.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": ACCOUNT}}
            ),
            description="Run reconciler: read executions of one state machine, update run history",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnLogs", actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[log_group.log_group_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectOwnExecutions", actions=["states:DescribeExecution"],
                resources=[STATE_MACHINE_EXECUTION_ARN_PATTERN],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SettleRunHistoryItems",
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                resources=[table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListRunsByIndex", actions=["dynamodb:Query"],
                resources=[f"{table.table_arn}/index/{schema.LIST_INDEX}"],
            )
        )

        function = lambda_.Function(
            self,
            "Function",
            function_name=RECONCILER_FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.X86_64,
            handler="src.reconcile_runs.handler",
            code=lambda_.Code.from_asset(str(REPOSITORY_ROOT), exclude=PACKAGE_RULES, ignore_mode=IgnoreMode.DOCKER),
            role=role,
            timeout=Duration.seconds(RECONCILER_TIMEOUT_SECONDS),
            memory_size=RECONCILER_MEMORY_MIB,
            environment={
                "RUN_HISTORY_TABLE": RUN_HISTORY_TABLE_NAME,
                "PIPELINE_STATE_MACHINE_ARN": STATE_MACHINE_ARN,
            },
            log_group=log_group,
        )

        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": ACCOUNT}}
            ),
            description="EventBridge Scheduler: invoke the run reconciler only",
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeReconciler", actions=["lambda:InvokeFunction"], resources=[function.function_arn]
            )
        )
        scheduler.CfnSchedule(
            self,
            "Schedule",
            name=RECONCILER_SCHEDULE_NAME,
            description="Settle run history from Step Functions outcomes",
            schedule_expression=f"rate({RECONCILER_RATE_MINUTES} minutes)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            state=config.schedule_state,
            target=scheduler.CfnSchedule.TargetProperty(
                arn=function.function_arn,
                role_arn=scheduler_role.role_arn,
                input="{}",
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=0, maximum_event_age_in_seconds=60
                ),
            ),
        )
        CfnOutput(self, "ReconcilerFunctionArn", value=function.function_arn)
