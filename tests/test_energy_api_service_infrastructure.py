"""Offline checks of the ECR registry and ECS Fargate API service (no AWS access)."""

import json
import re
import sys
from pathlib import Path

INFRA = Path(__file__).resolve().parents[1] / "infrastructure"
sys.path.insert(0, str(INFRA))

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from energy_config import (
    STATE_MACHINE_ARN,
    ApiServiceConfig,
    ConfigError,
    load_service_config,
)
from stacks.energy_api_registry_stack import EnergyApiRegistryStack
from stacks.energy_edge_stacks import EnergyApiEdgeStack
from stacks.energy_run_history_stack import EnergyRunHistoryStack

from src.healthcheck import main as healthcheck_main  # noqa: F401
from src.settings import load_settings
from tests.test_energy_edge_infrastructure import (
    ACCOUNT,
    API_CERT,
    API_HOSTNAME,
    FRONTEND_CERT,
    GAME_HOSTS,
    INFRA,
    config,
    run_app,
)

REPO = INFRA.parent
SERVICE = ApiServiceConfig(
    image_tag="v0.1.0",
    cognito_region="eu-west-1",
    user_pool_id="eu-west-1_TestPool1",
    app_client_id="testclientid1234567890",
)
SECRET_ARN = f"arn:aws:secretsmanager:eu-west-1:{ACCOUNT}:secret:energy/example-AbCdEf"
ENV = cdk.Environment(account=ACCOUNT, region="eu-west-1")


def synth(service=SERVICE):
    app = cdk.App()
    registry = EnergyApiRegistryStack(app, "IrishEnergyApiRegistryStack", env=ENV)
    run_history = EnergyRunHistoryStack(app, "IrishEnergyRunHistoryStack", env=ENV)
    edge = EnergyApiEdgeStack(
        app,
        "IrishEnergyApiEdgeStack",
        config=config(),
        service=service,
        repository=registry.repository,
        table=run_history.table,
        env=ENV,
    )
    return Template.from_stack(registry), Template.from_stack(edge)


@pytest.fixture(scope="module")
def templates():
    return synth()


def resources(template, kind):
    return template.find_resources(kind)


def only(template, kind):
    found = resources(template, kind)
    assert len(found) == 1, kind
    return next(iter(found.values()))


def container(template):
    task = only(template, "AWS::ECS::TaskDefinition")["Properties"]
    assert len(task["ContainerDefinitions"]) == 1
    return task["ContainerDefinitions"][0]


def all_statements(template):
    return [
        statement
        for policy in resources(template, "AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def as_list(value):
    return value if isinstance(value, list) else [value]


# ECR


def test_repository_is_dedicated_scanned_encrypted_immutable_and_retained(templates):
    registry, _ = templates
    repo = only(registry, "AWS::ECR::Repository")

    props = repo["Properties"]
    assert props["RepositoryName"] == "irish-energy-api"
    assert props["ImageScanningConfiguration"] == {"ScanOnPush": True}
    # AES-256 is ECR's default at-rest encryption, so CDK emits no explicit block; no KMS key is created.
    assert props.get("EncryptionConfiguration", {"EncryptionType": "AES256"}) == {"EncryptionType": "AES256"}
    assert props["ImageTagMutability"] == "IMMUTABLE"
    assert repo["DeletionPolicy"] == "Retain"
    assert "RepositoryPolicyText" not in props  # no resource policy, so no wildcard principal


def test_lifecycle_removes_old_untagged_images_and_caps_the_rest(templates):
    registry, _ = templates
    policy = json.loads(
        only(registry, "AWS::ECR::Repository")["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"]
    )

    rules = {rule["rulePriority"]: rule for rule in policy["rules"]}
    assert rules[1]["selection"] == {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countNumber": 14,
        "countUnit": "days",
    }
    assert rules[2]["selection"]["countType"] == "imageCountMoreThan"
    assert rules[2]["selection"]["countNumber"] == 30
    assert all(rule["action"] == {"type": "expire"} for rule in rules.values())


def test_registry_stack_contains_only_the_repository(templates):
    registry, _ = templates

    assert {r["Type"] for r in registry.to_json()["Resources"].values()} == {"AWS::ECR::Repository"}


# ECS cluster, task definition, service


def test_cluster_has_no_paid_container_insights(templates):
    _, edge = templates

    edge.resource_count_is("AWS::ECS::Cluster", 1)
    edge.has_resource_properties(
        "AWS::ECS::Cluster", {"ClusterSettings": [{"Name": "containerInsights", "Value": "disabled"}]}
    )


def test_task_definition_is_fargate_x86_with_the_configured_size(templates):
    _, edge = templates

    edge.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Cpu": "256",
            "Memory": "1024",
            "NetworkMode": "awsvpc",
            "RequiresCompatibilities": ["FARGATE"],
            "RuntimePlatform": {"CpuArchitecture": "X86_64", "OperatingSystemFamily": "LINUX"},
            "Volumes": [{"Name": "tmp"}],
        },
    )


def test_container_is_hardened_and_uses_the_pinned_image_tag(templates):
    _, edge = templates
    api = container(edge)

    assert api["Name"] == "api" and api["Essential"] is True
    assert api["User"] == "10001:10001"
    assert api["ReadonlyRootFilesystem"] is True
    assert api["PortMappings"] == [{"ContainerPort": 8000, "Protocol": "tcp"}]
    assert api["MountPoints"] == [{"ContainerPath": "/tmp", "ReadOnly": False, "SourceVolume": "tmp"}]
    assert api["LinuxParameters"]["Capabilities"] == {"Drop": ["ALL"]}
    assert api["LinuxParameters"]["InitProcessEnabled"] is True
    assert api["StopTimeout"] == 30
    image = json.dumps(api["Image"])
    assert ":v0.1.0" in image and "latest" not in image
    assert "Privileged" not in api


def test_container_health_check_matches_the_dockerfile_and_the_target_group(templates):
    _, edge = templates
    check = container(edge)["HealthCheck"]
    dockerfile = (REPO / "Dockerfile").read_text()

    assert check["Command"] == ["CMD", "python", "-m", "src.healthcheck"]
    assert (check["Interval"], check["Timeout"], check["Retries"], check["StartPeriod"]) == (30, 5, 3, 30)
    assert '"python", "-m", "src.healthcheck"' in dockerfile
    target_group = only(edge, "AWS::ElasticLoadBalancingV2::TargetGroup")["Properties"]
    assert target_group["HealthCheckPath"] == SERVICE.health_check_path == "/health"


def test_logs_go_to_a_retained_group_with_30_day_retention(templates):
    _, edge = templates
    log_group = only(edge, "AWS::Logs::LogGroup")

    assert log_group["Properties"]["RetentionInDays"] == 30
    assert log_group["DeletionPolicy"] == "Retain"
    logging = container(edge)["LogConfiguration"]
    assert logging["LogDriver"] == "awslogs"
    assert logging["Options"]["awslogs-stream-prefix"] == "api"
    assert logging["Options"]["awslogs-region"] == "eu-west-1"


def test_service_settings(templates):
    _, edge = templates

    edge.has_resource_properties(
        "AWS::ECS::Service",
        {
            "LaunchType": "FARGATE",
            "DesiredCount": 1,
            "HealthCheckGracePeriodSeconds": 60,
            "DeploymentConfiguration": Match.object_like(
                {
                    "MinimumHealthyPercent": 100,
                    "MaximumPercent": 200,
                    "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
                }
            ),
            "NetworkConfiguration": {
                "AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "ENABLED"})
            },
            "LoadBalancers": [Match.object_like({"ContainerName": "api", "ContainerPort": 8000})],
        },
    )
    service = only(edge, "AWS::ECS::Service")["Properties"]
    assert not service.get("EnableExecuteCommand")


# Environment configuration


def environment(template):
    return {item["Name"]: item["Value"] for item in container(template)["Environment"]}


def test_environment_is_explicit_and_contains_no_credentials_or_execution_switch(templates):
    _, edge = templates
    env = environment(edge)

    assert {k: v for k, v in env.items() if k not in ("FORWARDED_ALLOW_IPS", "RUN_HISTORY_TABLE")} == {
        "API_ENV": "production",
        "PORT": "8000",
        "HEALTH_CHECK_PATH": "/health",
        "AWS_DEFAULT_REGION": "eu-west-1",
        "COGNITO_REGION": "eu-west-1",
        "COGNITO_USER_POOL_ID": "eu-west-1_TestPool1",
        "COGNITO_APP_CLIENT_ID": "testclientid1234567890",
        "CORS_ALLOWED_ORIGINS": "https://energy.ai-autofy-general-server.com",
        "S3_BUCKET": "irish-energy-data-platform-851725545966",
        "RUN_HISTORY_BACKEND": "dynamodb",
        "RUN_HISTORY_REGION": "eu-west-1",
        "PIPELINE_STATE_MACHINE_ARN": STATE_MACHINE_ARN,  # dormant: nothing reads it while execution is off
    }
    assert "RUN_HISTORY_DB_PATH" not in env  # no SQLite configuration in production
    assert "irish-energy-run-history" in json.dumps(env["RUN_HISTORY_TABLE"]) or "ImportValue" in json.dumps(
        env["RUN_HISTORY_TABLE"]
    )
    assert "Fn::GetAtt" in json.dumps(env["FORWARDED_ALLOW_IPS"])  # the VPC CIDR, not "*"
    flat = json.dumps(env).upper()
    for forbidden in ("ACCESS_KEY", "SECRET", "SESSION_TOKEN", "PIPELINE_EXECUTION", "ENABLE_API_DOCS"):
        assert forbidden not in flat


def test_deployed_environment_is_valid_production_config_with_execution_disabled(templates):
    _, edge = templates
    env = {
        **environment(edge),
        "FORWARDED_ALLOW_IPS": "10.0.0.0/16",
        "RUN_HISTORY_TABLE": "irish-energy-run-history",  # a cross-stack reference in the template
    }

    settings = load_settings(env)  # raises if production config were incomplete or unsafe

    assert settings.api_env == "production"
    assert settings.cognito is not None and settings.cognito.client_id == "testclientid1234567890"
    assert settings.pipeline_execution_enabled is False
    assert settings.enable_api_docs is False
    assert settings.cors_allowed_origins == ("https://energy.ai-autofy-general-server.com",)
    assert settings.run_history.backend == "dynamodb"
    assert settings.run_history.region == "eu-west-1"


def test_no_secrets_are_wired_unless_configured(templates):
    _, edge = templates

    assert "Secrets" not in container(edge)
    assert "secretsmanager" not in json.dumps(edge.to_json())


def test_secrets_manager_secrets_are_granted_to_the_execution_role_only_by_exact_arn():
    _, edge = synth(
        ApiServiceConfig(
            image_tag="v0.1.0",
            cognito_region="eu-west-1",
            user_pool_id="eu-west-1_TestPool1",
            app_client_id="testclientid1234567890",
            secret_arns=(("EXAMPLE_SETTING", SECRET_ARN),),
        )
    )

    assert container(edge)["Secrets"] == [{"Name": "EXAMPLE_SETTING", "ValueFrom": SECRET_ARN}]
    grants = [
        (policy_name, statement)
        for policy_name, policy in resources(edge, "AWS::IAM::Policy").items()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if any(a.startswith("secretsmanager:") for a in as_list(statement["Action"]))
    ]
    assert len(grants) == 1 and grants[0][0].startswith("ApiServiceExecutionRole")
    assert grants[0][1]["Resource"] == SECRET_ARN
    assert SECRET_ARN not in json.dumps(resources(edge, "AWS::IAM::Role"))
    assert "secretsmanager" not in json.dumps(
        next(p for n, p in resources(edge, "AWS::IAM::Policy").items() if n.startswith("ApiServiceTaskRole"))
    )


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:secretsmanager:us-east-1:851725545966:secret:x-AbCdEf",
        "arn:aws:secretsmanager:eu-west-1:111111111111:secret:x-AbCdEf",
        "arn:aws:secretsmanager:eu-west-1:851725545966:secret:*",
        "not-an-arn",
    ],
)
def test_foreign_or_wildcard_secret_arns_are_rejected(arn):
    with pytest.raises(ConfigError):
        ApiServiceConfig(image_tag="v1", cognito_region="eu-west-1", user_pool_id="p", app_client_id="c", secret_arns=(("X", arn),))


# IAM


def test_two_separate_roles_trusted_by_ecs_tasks_for_this_account_only(templates):
    _, edge = templates
    roles = resources(edge, "AWS::IAM::Role")

    assert sorted(n[:-8] for n in roles) == ["ApiServiceExecutionRole", "ApiServiceTaskRole"]
    for role in roles.values():
        assert role["Properties"]["AssumeRolePolicyDocument"]["Statement"] == [
            {
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            }
        ]
        assert "ManagedPolicyArns" not in role["Properties"]  # no AWS managed policies
        assert "Policies" not in role["Properties"]
    task_definition = only(edge, "AWS::ECS::TaskDefinition")["Properties"]
    assert task_definition["TaskRoleArn"] != task_definition["ExecutionRoleArn"]


def policy_for(template, prefix):
    matches = [p for n, p in resources(template, "AWS::IAM::Policy").items() if n.startswith(prefix)]
    assert len(matches) == 1
    return matches[0]["Properties"]["PolicyDocument"]["Statement"]


def test_execution_role_can_only_pull_the_image_and_write_its_logs(templates):
    _, edge = templates
    statements = policy_for(edge, "ApiServiceExecutionRole")

    by_action = {tuple(as_list(s["Action"])): s for s in statements}
    assert set(by_action) == {
        ("ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"),
        ("ecr:GetAuthorizationToken",),
        ("logs:CreateLogStream", "logs:PutLogEvents"),
    }
    # ecr:GetAuthorizationToken does not support resource scoping; every other resource is exact.
    assert by_action[("ecr:GetAuthorizationToken",)]["Resource"] == "*"
    assert by_action[("logs:CreateLogStream", "logs:PutLogEvents")]["Resource"] != "*"


TABLE_ARN_IMPORT = "IrishEnergyRunHistoryStack:ExportsOutputFnGetAttRunHistoryTableFD4DA44DArn6CF8D0FC"


def test_application_role_has_exactly_the_gold_read_and_run_history_permissions(templates):
    _, edge = templates
    statements = policy_for(edge, "ApiServiceTaskRole")

    assert statements == [
        {
            "Sid": "ListGoldPrefixOnly",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::irish-energy-data-platform-851725545966",
            "Condition": {"StringLike": {"s3:prefix": ["gold/eirgrid/daily_demand/*"]}},
        },
        {
            "Sid": "ReadGoldObjectsOnly",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::irish-energy-data-platform-851725545966/gold/eirgrid/daily_demand/*",
        },
        {
            "Sid": "RunHistoryItems",
            "Effect": "Allow",
            "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
            "Resource": {"Fn::ImportValue": TABLE_ARN_IMPORT},
        },
        {
            "Sid": "RunHistoryListIndex",
            "Effect": "Allow",
            "Action": "dynamodb:Query",
            "Resource": {"Fn::Join": ["", [{"Fn::ImportValue": TABLE_ARN_IMPORT}, "/index/gsi1"]]},
        },
        {
            "Sid": "StartAndInspectOneStateMachine",
            "Effect": "Allow",
            "Action": "states:StartExecution",
            "Resource": STATE_MACHINE_ARN,
        },
        {
            "Sid": "ReconcileOwnExecutions",
            "Effect": "Allow",
            "Action": "states:DescribeExecution",
            "Resource": STATE_MACHINE_ARN.replace(":stateMachine:", ":execution:") + ":*",
        },
    ]


def test_application_role_has_none_of_the_permissions_it_must_not_have(templates):
    _, edge = templates
    statements = policy_for(edge, "ApiServiceTaskRole")
    flat = json.dumps(statements).lower()

    for forbidden in (
        "states:*", "states:stop", "states:delete", "states:create", "states:update", "states:send",
        "states:list", "states:start_sync", "lambda:", "glue:", "athena:", "iam:", "cognito", "secretsmanager", "kms:", "s3:put", "s3:delete",
        "dynamodb:scan", "dynamodb:batch", "dynamodb:createtable", "dynamodb:deletetable", "dynamodb:updatetable",
        "dynamodb:describe", "dynamodb:*", "dynamodb:transact",
    ):
        assert forbidden not in flat
    # every DynamoDB resource is the one table or its one index, never a wildcard
    for statement in statements:
        if statement["Sid"].startswith("RunHistory"):
            assert "*" not in json.dumps(statement["Resource"])
    assert resources(edge, "AWS::IAM::ManagedPolicy") == {}


def dynamo_operations_in_store():
    source = (REPO / "src" / "dynamodb_run_store.py").read_text()
    calls = set(re.findall(r'(?:_call|_conditional)\(\s*"(\w+)"', source))
    assert '"Update": {' not in source and '"Delete": {' not in source and '"ConditionCheck"' not in source
    return calls


def test_granted_actions_are_exactly_what_the_run_store_calls(templates):
    _, edge = templates
    iam_action = {
        "get_item": "dynamodb:GetItem",
        "put_item": "dynamodb:PutItem",
        "update_item": "dynamodb:UpdateItem",
        "delete_item": "dynamodb:DeleteItem",
        "query": "dynamodb:Query",
        "transact_write_items": "dynamodb:PutItem",  # the transaction only contains Put items
    }
    needed = {iam_action[call] for call in dynamo_operations_in_store()}
    granted = {
        action
        for statement in policy_for(edge, "ApiServiceTaskRole")
        if statement["Sid"].startswith("RunHistory")
        for action in as_list(statement["Action"])
    }

    assert needed == granted


def test_every_operation_the_store_performs_in_practice_is_permitted(templates):
    from src.dynamodb_run_store import DynamoRunStore
    from tests.fake_dynamodb import FakeDynamoDB
    from tests.test_run_store_contract import Clock

    clock, fake = Clock(), FakeDynamoDB()
    store = DynamoRunStore("t", fake, clock=clock)
    run = store.create_run(2, requested_by="u", idempotency_key="idem-key-0001")
    store.create_run(2, requested_by="u", idempotency_key="idem-key-0001")
    store.update_run(run["run_id"], status="running", stage="bronze")
    store.update_run(run["run_id"], status="succeeded")
    store.get_run(run["run_id"])
    store.list_runs()
    clock.advance(10_000)
    store.recover_interrupted()
    iam_action = {
        "get_item": "dynamodb:GetItem", "update_item": "dynamodb:UpdateItem", "delete_item": "dynamodb:DeleteItem",
        "query": "dynamodb:Query", "transact_write_items": "dynamodb:PutItem", "put_item": "dynamodb:PutItem",
    }
    _, edge = templates
    granted = {
        a
        for s in policy_for(edge, "ApiServiceTaskRole")
        if s["Sid"].startswith("RunHistory")
        for a in as_list(s["Action"])
    }

    assert {iam_action[call] for call in set(fake.calls)} <= granted


def test_the_cdk_permissions_are_a_subset_of_the_phase_6_specification(templates):
    from infrastructure.run_control import spec

    policy = spec.api_task_policy(bucket="b", table="t", state_machine_arn="arn:aws:states:eu-west-1:851725545966:stateMachine:m")
    spec_actions = {a for s in policy["Statement"] for a in s["Action"]}
    _, edge = templates
    cdk_actions = {a for s in policy_for(edge, "ApiServiceTaskRole") for a in as_list(s["Action"])}

    assert cdk_actions <= spec_actions
    assert {a for a in cdk_actions if a.startswith("states:")} == {"states:StartExecution", "states:DescribeExecution"}


def test_gold_prefix_matches_what_the_analytics_code_reads():
    from src import analytics

    assert analytics.GOLD_PREFIX == "gold/eirgrid/daily_demand/"
    from energy_config import GOLD_PREFIX

    assert GOLD_PREFIX == analytics.GOLD_PREFIX


def test_no_action_wildcards_and_the_only_star_resource_is_the_ecr_token(templates):
    for template in templates:
        for statement in all_statements(template):
            for action in as_list(statement["Action"]):
                assert "*" not in action
            if "*" in as_list(statement["Resource"]):
                assert as_list(statement["Action"]) == ["ecr:GetAuthorizationToken"]


# Networking


def sg_resources(template):
    groups = resources(template, "AWS::EC2::SecurityGroup")
    task = next(n for n in groups if n.startswith("ApiServiceTaskSecurityGroup"))
    alb = next(n for n in groups if n.startswith("AlbSecurityGroup"))
    return task, alb


def test_task_security_group_accepts_only_the_alb_on_the_container_port(templates):
    _, edge = templates
    task, alb = sg_resources(edge)

    assert "SecurityGroupIngress" not in resources(edge, "AWS::EC2::SecurityGroup")[task]["Properties"]
    ingress = [
        r["Properties"]
        for r in resources(edge, "AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"]["GroupId"] == {"Fn::GetAtt": [task, "GroupId"]}
    ]
    assert len(ingress) == 1
    assert ingress[0]["SourceSecurityGroupId"] == {"Fn::GetAtt": [alb, "GroupId"]}
    assert (ingress[0]["FromPort"], ingress[0]["ToPort"], ingress[0]["IpProtocol"]) == (8000, 8000, "tcp")
    assert "CidrIp" not in ingress[0] and "CidrIpv6" not in ingress[0]


def test_task_security_group_allows_only_outbound_https(templates):
    _, edge = templates
    task, _ = sg_resources(edge)

    egress = resources(edge, "AWS::EC2::SecurityGroup")[task]["Properties"]["SecurityGroupEgress"]
    assert [(e["IpProtocol"], e["FromPort"], e["ToPort"], e["CidrIp"]) for e in egress] == [
        ("tcp", 443, 443, "0.0.0.0/0")
    ]


def test_alb_may_reach_the_task_port_and_nothing_else(templates):
    _, edge = templates
    task, alb = sg_resources(edge)

    outgoing = [
        r["Properties"]
        for r in resources(edge, "AWS::EC2::SecurityGroupEgress").values()
        if r["Properties"]["GroupId"] == {"Fn::GetAtt": [alb, "GroupId"]}
    ]
    assert len(outgoing) == 1
    assert outgoing[0]["DestinationSecurityGroupId"] == {"Fn::GetAtt": [task, "GroupId"]}
    assert (outgoing[0]["FromPort"], outgoing[0]["ToPort"]) == (8000, 8000)
    # No catch-all rule: the only outbound path is the explicit rule above.
    assert "SecurityGroupEgress" not in resources(edge, "AWS::EC2::SecurityGroup")[alb]["Properties"]


def test_no_nat_no_interface_endpoints_and_two_free_gateway_endpoints(templates):
    _, edge = templates
    edge.resource_count_is("AWS::EC2::NatGateway", 0)
    edge.resource_count_is("AWS::EC2::EIP", 0)
    endpoints = resources(edge, "AWS::EC2::VPCEndpoint").values()

    assert [e["Properties"]["VpcEndpointType"] for e in endpoints] == ["Gateway", "Gateway"]
    names = sorted(json.dumps(e["Properties"]["ServiceName"]) for e in endpoints)
    assert names[0].endswith('.dynamodb"]]}') and names[1].endswith('.s3"]]}')
    subnets = resources(edge, "AWS::EC2::Subnet").values()
    assert all(s["Properties"]["MapPublicIpOnLaunch"] for s in subnets)


# ALB target group and listener integration


def test_target_group_targets_task_ips_with_the_health_check(templates):
    _, edge = templates

    edge.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "TargetType": "ip",
            "Port": 8000,
            "Protocol": "HTTP",
            "HealthCheckPath": "/health",
            "HealthCheckIntervalSeconds": 30,
            "HealthCheckTimeoutSeconds": 5,
            "HealthyThresholdCount": 2,
            "UnhealthyThresholdCount": 3,
            "Matcher": {"HttpCode": "200"},
        },
    )


def test_only_the_energy_host_is_forwarded_and_the_placeholder_503_is_gone(templates):
    _, edge = templates
    rules = resources(edge, "AWS::ElasticLoadBalancingV2::ListenerRule").values()

    assert len(rules) == 1
    rule = next(iter(rules))["Properties"]
    assert rule["Priority"] == 10
    assert rule["Conditions"] == [{"Field": "host-header", "HostHeaderConfig": {"Values": [API_HOSTNAME]}}]
    assert [a["Type"] for a in rule["Actions"]] == ["forward"]
    assert "Service not deployed yet" not in json.dumps(edge.to_json())
    edge.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Port": 443, "DefaultActions": [Match.object_like({"Type": "fixed-response"})]},
    )


# Isolation from the game


def test_nothing_mentions_the_game_and_names_are_energy_specific(templates):
    for template in templates:
        text = json.dumps(template.to_json()).lower()
        for host in GAME_HOSTS:
            assert not re.search(rf"(?<![\w-]){re.escape(host)}", text)
        assert "bluff" not in text and "believe" not in text
    assert only(templates[0], "AWS::ECR::Repository")["Properties"]["RepositoryName"].startswith("irish-energy")


def test_service_repository_and_table_must_be_given_together():
    app = cdk.App()
    registry = EnergyApiRegistryStack(app, "R", env=ENV)
    history = EnergyRunHistoryStack(app, "H", env=ENV)
    with pytest.raises(ValueError):
        EnergyApiEdgeStack(app, "E1", config=config(), service=SERVICE, env=ENV)
    with pytest.raises(ValueError):
        EnergyApiEdgeStack(app, "E2", config=config(), repository=registry.repository, env=ENV)
    with pytest.raises(ValueError):
        EnergyApiEdgeStack(app, "E3", config=config(), service=SERVICE, repository=registry.repository, env=ENV)
    with pytest.raises(ValueError):
        EnergyApiEdgeStack(app, "E4", config=config(), table=history.table, env=ENV)


# Configuration loading


def ctx(**values):
    return lambda key: values.get(key)


def test_defaults_are_the_documented_values():
    assert (SERVICE.container_port, SERVICE.cpu, SERVICE.memory_mib, SERVICE.desired_count) == (8000, 256, 1024, 1)
    assert SERVICE.health_check_path == "/health" and SERVICE.log_retention_days == 30
    assert SERVICE.bucket == "irish-energy-data-platform-851725545966"
    assert SERVICE.secret_arns == ()


def test_service_inputs_are_optional_but_all_or_nothing():
    assert load_service_config(ctx()) is None
    for partial in (
        {"apiImageTag": "v1"},
        {"cognitoUserPoolId": "eu-west-1_TestPool1"},
        {"apiImageTag": "v1", "cognitoUserPoolId": "eu-west-1_TestPool1"},
    ):
        with pytest.raises(ConfigError, match="all of"):
            load_service_config(ctx(**partial))


@pytest.mark.parametrize(
    "overrides",
    [
        {"apiImageTag": "latest"},
        {"apiImageTag": "bad tag"},
        {"apiImageTag": ""},
        {"cognitoUserPoolId": "us-east-1_TestPool1"},
        {"cognitoUserPoolId": "nonsense"},
        {"cognitoAppClientId": "short"},
    ],
)
def test_invalid_service_inputs_are_rejected(overrides):
    values = {"apiImageTag": "v1", "cognitoUserPoolId": "eu-west-1_TestPool1", "cognitoAppClientId": "testclientid1234567890"}
    values.update(overrides)

    with pytest.raises(ConfigError):
        load_service_config(ctx(**values))


def test_valid_service_inputs_load():
    loaded = load_service_config(
        ctx(apiImageTag="2026.10.30-1", cognitoUserPoolId="eu-west-1_TestPool1", cognitoAppClientId="testclientid1234567890")
    )

    assert loaded.image_tag == "2026.10.30-1" and loaded.cognito_region == "eu-west-1"


# Whole app


CERTS = {"apiCertificateArn": API_CERT, "frontendCertificateArn": FRONTEND_CERT}
SERVICE_CTX = {"apiImageTag": "v0.1.0", "cognitoUserPoolId": "eu-west-1_TestPool1", "cognitoAppClientId": "testclientid1234567890"}


def test_app_with_service_inputs_synthesizes_the_service(tmp_path):
    result, names = run_app(tmp_path, {**CERTS, **SERVICE_CTX})

    assert result.returncode == 0, result.stderr
    assert "IrishEnergyApiRegistryStack.template.json" in names
    template = json.loads((tmp_path / "IrishEnergyApiEdgeStack.template.json").read_text())
    kinds = {r["Type"] for r in template["Resources"].values()}
    assert {"AWS::ECS::Service", "AWS::ECS::TaskDefinition", "AWS::ECS::Cluster"} <= kinds


def test_app_without_service_inputs_keeps_the_placeholder_and_says_so(tmp_path):
    result, _ = run_app(tmp_path, CERTS)

    assert result.returncode == 0, result.stderr
    assert "service skipped" in result.stdout
    template = json.loads((tmp_path / "IrishEnergyApiEdgeStack.template.json").read_text())
    assert not {r["Type"] for r in template["Resources"].values()} & {"AWS::ECS::Service"}


@pytest.mark.parametrize(
    "ctx_values,message",
    [
        ({**CERTS, "apiImageTag": "v1"}, "all of"),
        ({**CERTS, **SERVICE_CTX, "apiImageTag": "latest"}, "not 'latest'"),
        (SERVICE_CTX, "certificate"),
    ],
)
def test_app_stops_on_unsafe_service_configuration(tmp_path, ctx_values, message):
    result, names = run_app(tmp_path, ctx_values)

    assert result.returncode != 0
    assert message in result.stderr
    assert names == []
