"""DynamoDB gateway VPC endpoint, checked on the synthesized CloudFormation (no AWS access)."""

import json

import pytest

from tests.test_energy_api_service_infrastructure import (  # noqa: F401
    SERVICE,
    config,
    ENV,
    only,
    policy_for,
    resources,
    synth,
)

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

from stacks.energy_edge_stacks import EnergyApiEdgeStack  # noqa: E402

TABLE_ARN_IMPORT = "IrishEnergyRunHistoryStack:ExportsOutputFnGetAttRunHistoryTableFD4DA44DArn6CF8D0FC"
ALLOWED_ACTIONS = {
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Query",
}


@pytest.fixture(scope="module")
def edge():
    return synth()[1]


def endpoints(template):
    found = {}
    for name, resource in resources(template, "AWS::EC2::VPCEndpoint").items():
        service = json.dumps(resource["Properties"]["ServiceName"])
        found["dynamodb" if service.endswith('.dynamodb"]]}') else "s3"] = (name, resource["Properties"])
    return found


def route_tables_of_ecs_subnets(template):
    service = only(template, "AWS::ECS::Service")["Properties"]
    subnets = service["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"]
    wanted = {s["Ref"] for s in subnets}
    tables = set()
    for association in resources(template, "AWS::EC2::SubnetRouteTableAssociation").values():
        props = association["Properties"]
        if props["SubnetId"]["Ref"] in wanted:
            tables.add(props["RouteTableId"]["Ref"])
    assert len(wanted) == 2 and len(tables) == 2
    return tables


def test_a_dynamodb_gateway_endpoint_exists_in_the_ecs_vpc(edge):
    name, props = endpoints(edge)["dynamodb"]

    assert props["VpcEndpointType"] == "Gateway"
    (vpc_name,) = resources(edge, "AWS::EC2::VPC")
    assert props["VpcId"] == {"Ref": vpc_name}
    # the same VPC the ECS target group lives in
    target_group = only(edge, "AWS::ElasticLoadBalancingV2::TargetGroup")["Properties"]
    assert target_group["VpcId"] == {"Ref": vpc_name}


def test_service_is_dynamodb_in_the_stack_region(edge):
    _, props = endpoints(edge)["dynamodb"]

    assert props["ServiceName"] == {"Fn::Join": ["", ["com.amazonaws.", {"Ref": "AWS::Region"}, ".dynamodb"]]}
    stack_region = cdk.Stack.of(only_stack()).region
    assert stack_region == "eu-west-1"


def only_stack():
    app = cdk.App()
    from stacks.energy_api_registry_stack import EnergyApiRegistryStack
    from stacks.energy_run_history_stack import EnergyRunHistoryStack

    registry = EnergyApiRegistryStack(app, "R", env=ENV)
    history = EnergyRunHistoryStack(app, "H", env=ENV)
    return EnergyApiEdgeStack(
        app, "E", config=config(), service=SERVICE, repository=registry.repository, table=history.table, env=ENV
    )


def test_it_serves_exactly_the_route_tables_the_ecs_tasks_use(edge):
    _, props = endpoints(edge)["dynamodb"]

    assert {r["Ref"] for r in props["RouteTableIds"]} == route_tables_of_ecs_subnets(edge)


def test_it_serves_the_same_route_tables_as_the_existing_s3_endpoint(edge):
    ends = endpoints(edge)

    assert ends["dynamodb"][1]["RouteTableIds"] == ends["s3"][1]["RouteTableIds"]


def statements(edge):
    document = endpoints(edge)["dynamodb"][1]["PolicyDocument"]
    assert len(document["Statement"]) == 2
    return {s["Sid"]: s for s in document["Statement"]}


def endpoint_actions(edge):
    return {a for s in statements(edge).values() for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}


TABLE = {"Fn::ImportValue": TABLE_ARN_IMPORT}
INDEX = {"Fn::Join": ["", [TABLE, "/index/gsi1"]]}


def test_endpoint_policy_allows_only_the_run_history_operations(edge):
    found = statements(edge)

    assert set(found) == {"RunHistoryItemOperations", "RunHistoryListIndexQuery"}
    for statement in found.values():
        assert statement["Effect"] == "Allow"
        assert statement["Principal"] == {"AWS": "*"}  # endpoint policies require a wildcard principal
        assert not {"Condition", "NotAction", "NotResource"} & set(statement)
    assert found["RunHistoryItemOperations"]["Action"] == [
        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
    ]
    assert found["RunHistoryListIndexQuery"]["Action"] == "dynamodb:Query"
    assert endpoint_actions(edge) == ALLOWED_ACTIONS


def test_item_operations_including_the_transaction_puts_are_table_only_and_query_is_index_only(edge):
    found = statements(edge)

    # No item or transaction permission is granted against the index; Query is index-only.
    assert found["RunHistoryItemOperations"]["Resource"] == TABLE
    assert found["RunHistoryListIndexQuery"]["Resource"] == INDEX
    assert "*" not in json.dumps([s["Resource"] for s in found.values()])


@pytest.mark.parametrize(
    "forbidden",
    [
        "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:CreateTable",
        "dynamodb:DeleteTable", "dynamodb:UpdateTable", "dynamodb:DescribeTable", "dynamodb:*", "dynamodb:Describe",
        "dynamodb:List", "dynamodb:TransactGetItems", "dynamodb:TransactWriteItems", "dynamodb:ImportTable",
        "dynamodb:ExportTableToPointInTime", "dynamodb:RestoreTableFromBackup", "dynamodb:UpdateTimeToLive",
        "dynamodb:ConditionCheckItem",
    ],
)
def test_no_scan_batch_table_management_or_unused_permissions(edge, forbidden):
    assert forbidden not in json.dumps(list(statements(edge).values()))


def test_every_action_in_the_policy_is_one_the_run_store_performs(edge):
    from tests.test_dynamodb_transaction_permissions import required_iam_actions

    assert required_iam_actions() == endpoint_actions(edge)


def test_the_endpoint_policy_matches_the_task_role_and_does_not_replace_it(edge):
    role = {s["Sid"]: s for s in policy_for(edge, "ApiServiceTaskRole")}
    mine = statements(edge)

    assert role["RunHistoryItems"]["Action"] == mine["RunHistoryItemOperations"]["Action"]
    assert role["RunHistoryItems"]["Resource"] == mine["RunHistoryItemOperations"]["Resource"]
    assert role["RunHistoryListIndex"]["Action"] == mine["RunHistoryListIndexQuery"]["Action"]
    assert role["RunHistoryListIndex"]["Resource"] == mine["RunHistoryListIndexQuery"]["Resource"]


def test_the_ecs_task_role_run_history_and_gold_permissions_are_unchanged(edge):
    statements = policy_for(edge, "ApiServiceTaskRole")

    assert [s["Sid"] for s in statements] == [
        "ListGoldPrefixOnly",
        "ReadGoldObjectsOnly",
        "RunHistoryItems",
        "RunHistoryListIndex",
        "StartAndInspectOneStateMachine",  # Phase 9.5, dormant
        "ReconcileOwnExecutions",
    ]
    assert statements[2]["Action"] == ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    assert statements[3]["Action"] == "dynamodb:Query"
    assert "Principal" not in json.dumps(statements)  # identity policies, not the endpoint policy


def test_the_s3_endpoint_is_unchanged(edge):
    _, props = endpoints(edge)["s3"]

    assert props["VpcEndpointType"] == "Gateway"
    assert props["ServiceName"] == {"Fn::Join": ["", ["com.amazonaws.", {"Ref": "AWS::Region"}, ".s3"]]}
    assert "PolicyDocument" not in props  # still the default full-access policy
    assert len(props["RouteTableIds"]) == 2
    assert not {"SecurityGroupIds", "SubnetIds", "PrivateDnsEnabled"} & set(props)


def test_no_nat_no_extra_vpc_no_interface_endpoints_and_no_public_ip_change(edge):
    edge.resource_count_is("AWS::EC2::VPC", 1)
    edge.resource_count_is("AWS::EC2::NatGateway", 0)
    edge.resource_count_is("AWS::EC2::EIP", 0)
    edge.resource_count_is("AWS::EC2::VPCEndpoint", 2)
    assert {p["VpcEndpointType"] for p in (e[1] for e in endpoints(edge).values())} == {"Gateway"}
    service = only(edge, "AWS::ECS::Service")["Properties"]
    assert service["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == "ENABLED"
    assert all(s["Properties"]["MapPublicIpOnLaunch"] for s in resources(edge, "AWS::EC2::Subnet").values())


def test_endpoint_policy_is_the_only_resource_policy_with_a_wildcard_principal(edge):
    wildcard = [
        name
        for name, r in edge.to_json()["Resources"].items()
        if '"Principal": {"AWS": "*"}' in json.dumps(r)
    ]

    assert len(wildcard) == 1 and wildcard[0] in resources(edge, "AWS::EC2::VPCEndpoint")


def test_without_the_service_there_are_no_endpoints():
    app = cdk.App()
    stack = EnergyApiEdgeStack(app, "E", config=config(), env=ENV)

    Template.from_stack(stack).resource_count_is("AWS::EC2::VPCEndpoint", 0)


def test_the_endpoint_lives_only_in_the_api_edge_stack(tmp_path):
    from tests.test_energy_edge_infrastructure import run_app
    from tests.test_energy_api_service_infrastructure import CERTS, SERVICE_CTX

    result, names = run_app(tmp_path, {**CERTS, **SERVICE_CTX})

    assert result.returncode == 0, result.stderr
    holders = [
        n for n in names if "AWS::EC2::VPCEndpoint" in (tmp_path / n).read_text()
    ]
    assert holders == ["IrishEnergyApiEdgeStack.template.json"]
