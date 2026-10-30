"""ECS Fargate service for the energy API, attached to the existing energy ALB listener.

Tasks run in the public subnets with a public IP because the API must reach Cognito's
JWKS endpoint and there is no NAT gateway; the task security group only accepts the
ALB's traffic and only allows outbound HTTPS. See docs/administrator-handover.md.
"""

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from energy_config import (
    ACCOUNT,
    CONTAINER_USER,
    GOLD_PREFIX,
    REGION,
    STATE_MACHINE_ARN,
    STATE_MACHINE_EXECUTION_ARN_PATTERN,
    ApiServiceConfig,
)
from src import run_history_schema as schema

_RETENTION = {
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    90: logs.RetentionDays.THREE_MONTHS,
}


class ApiService(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        listener: elbv2.ApplicationListener,
        repository: ecr.IRepository,
        table: dynamodb.ITable,
        config: ApiServiceConfig,
        api_hostname: str,
    ) -> None:
        super().__init__(scope, construct_id)

        task_sg = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=vpc,
            description="Energy API tasks: ingress from the ALB only",
            allow_all_outbound=False,
        )
        task_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to AWS services, ECR and Cognito JWKS"
        )

        def trust() -> iam.IPrincipal:
            return iam.PrincipalWithConditions(
                iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
            )

        # Execution role: pulls the image and writes logs (CDK adds exactly those grants).
        execution_role = iam.Role(
            self, "ExecutionRole", assumed_by=trust(), description="ECS agent: pull image, write logs"
        )
        # Application role: a read of the Gold prefix (src/analytics.py) and the run-history
        # table operations src/dynamodb_run_store.py performs, on this table and index only.
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=trust(),
            description="Energy API application: read Gold, use the run-history table",
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="ListGoldPrefixOnly",
                actions=["s3:ListBucket"],
                resources=[f"arn:aws:s3:::{config.bucket}"],
                conditions={"StringLike": {"s3:prefix": [GOLD_PREFIX + "*"]}},
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadGoldObjectsOnly",
                actions=["s3:GetObject"],
                resources=[f"arn:aws:s3:::{config.bucket}/{GOLD_PREFIX}*"],
            )
        )

        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="RunHistoryItems",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",  # also covers the Put items of TransactWriteItems
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[table.table_arn],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="RunHistoryListIndex",
                actions=["dynamodb:Query"],  # list_runs queries the index only
                resources=[f"{table.table_arn}/index/{schema.LIST_INDEX}"],
            )
        )

        # Dormant: nothing calls the executor while execution is disabled (src/settings.py).
        # Exactly one state machine and its executions; never states:*.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="StartAndInspectOneStateMachine",
                actions=["states:StartExecution"],
                resources=[STATE_MACHINE_ARN],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReconcileOwnExecutions",
                actions=["states:DescribeExecution"],
                resources=[STATE_MACHINE_EXECUTION_ARN_PATTERN],
            )
        )

        log_group = logs.LogGroup(
            self,
            "LogGroup",
            retention=_RETENTION.get(config.log_retention_days, logs.RetentionDays.ONE_MONTH),
            removal_policy=RemovalPolicy.RETAIN,
        )

        task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDefinition",
            cpu=config.cpu,
            memory_limit_mib=config.memory_mib,
            execution_role=execution_role,
            task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task_definition.add_volume(name="tmp")

        linux = ecs.LinuxParameters(self, "LinuxParameters", init_process_enabled=True)
        linux.drop_capabilities(ecs.Capability.ALL)

        # No credentials here: AWS access comes from the task role. Execution is not enabled.
        environment = {
            "API_ENV": "production",
            "PORT": str(config.container_port),
            "HEALTH_CHECK_PATH": config.health_check_path,
            "AWS_DEFAULT_REGION": config.cognito_region,
            "COGNITO_REGION": config.cognito_region,
            "COGNITO_USER_POOL_ID": config.user_pool_id,
            "COGNITO_APP_CLIENT_ID": config.app_client_id,
            "CORS_ALLOWED_ORIGINS": config.allowed_origin,
            "S3_BUCKET": config.bucket,
            "RUN_HISTORY_BACKEND": "dynamodb",
            "RUN_HISTORY_TABLE": table.table_name,
            "RUN_HISTORY_REGION": REGION,
            "PIPELINE_STATE_MACHINE_ARN": STATE_MACHINE_ARN,
            # Trust X-Forwarded-For only from the ALB's network, never from clients.
            "FORWARDED_ALLOW_IPS": vpc.vpc_cidr_block,
        }
        secrets = {
            name: ecs.Secret.from_secrets_manager(
                secretsmanager.Secret.from_secret_complete_arn(self, f"Secret{index}", arn)
            )
            for index, (name, arn) in enumerate(config.secret_arns)
        }

        container = task_definition.add_container(
            "api",
            image=ecs.ContainerImage.from_ecr_repository(repository, tag=config.image_tag),
            essential=True,
            user=CONTAINER_USER,
            readonly_root_filesystem=True,
            environment=environment,
            secrets=secrets or None,
            linux_parameters=linux,
            stop_timeout=Duration.seconds(30),
            port_mappings=[ecs.PortMapping(container_port=config.container_port)],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=log_group),
            health_check=ecs.HealthCheck(
                command=["CMD", "python", "-m", "src.healthcheck"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(30),
            ),
        )
        container.add_mount_points(
            ecs.MountPoint(source_volume="tmp", container_path="/tmp", read_only=False)
        )

        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED
        )
        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=config.desired_count,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[task_sg],
            min_healthy_percent=100,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            health_check_grace_period=Duration.seconds(60),
            enable_execute_command=False,
        )

        # Registering the service lets CDK open ALB -> task on the container port only.
        listener.add_targets(
            "EnergyApiTargets",
            priority=10,
            conditions=[elbv2.ListenerCondition.host_headers([api_hostname])],
            port=config.container_port,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                path=config.health_check_path,
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )

        self.cluster = cluster
        self.service = service
        self.task_security_group = task_sg
