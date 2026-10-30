"""Offline checks of the DynamoDB run-history table (no AWS access)."""

import json

import pytest

from tests.test_energy_edge_infrastructure import ACCOUNT, GAME_HOSTS, run_app  # noqa: F401  (adds infrastructure/ to sys.path)

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

from energy_config import RUN_HISTORY_TABLE_NAME  # noqa: E402
from infrastructure.run_control import spec  # noqa: E402
from src import run_history_schema as schema  # noqa: E402
from stacks.energy_run_history_stack import EnergyRunHistoryStack  # noqa: E402


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = EnergyRunHistoryStack(
        app, "IrishEnergyRunHistoryStack", env=cdk.Environment(account=ACCOUNT, region="eu-west-1")
    )
    return Template.from_stack(stack)


def table(template):
    found = template.find_resources("AWS::DynamoDB::Table")
    assert len(found) == 1
    return next(iter(found.values()))


def test_stack_contains_only_the_table_and_no_kms_key_or_iam(template):
    kinds = {r["Type"] for r in template.to_json()["Resources"].values()}

    assert kinds == {"AWS::DynamoDB::Table"}


def test_keys_billing_and_name(template):
    props = table(template)["Properties"]

    assert props["TableName"] == RUN_HISTORY_TABLE_NAME == "irish-energy-run-history"
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert "ProvisionedThroughput" not in props
    assert props["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    declared = {(a["AttributeName"], a["AttributeType"]) for a in props["AttributeDefinitions"]}
    assert declared == {("pk", "S"), ("sk", "S"), ("gsi1pk", "S"), ("gsi1sk", "S")}


def test_listing_index_is_the_one_the_store_queries(template):
    indexes = table(template)["Properties"]["GlobalSecondaryIndexes"]

    assert indexes == [
        {
            "IndexName": schema.LIST_INDEX,
            "KeySchema": [
                {"AttributeName": schema.LIST_INDEX_PARTITION_KEY, "KeyType": "HASH"},
                {"AttributeName": schema.LIST_INDEX_SORT_KEY, "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]
    source = open("src/dynamodb_run_store.py").read()
    assert 'IndexName=schema.LIST_INDEX' in source and 'KeyConditionExpression="gsi1pk = :pk"' in source


def test_ttl_is_enabled_on_the_attribute_the_store_writes(template):
    props = table(template)["Properties"]

    assert props["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}
    assert schema.TTL_ATTRIBUTE == "expires_at"
    assert "expires_at" in open("src/dynamodb_run_store.py").read()


def test_encryption_at_rest_uses_the_aws_managed_key(template):
    props = table(template)["Properties"]

    assert props["SSESpecification"] == {"SSEEnabled": True}  # no KMSMasterKeyId: alias/aws/dynamodb
    assert "KMSMasterKeyId" not in json.dumps(props)


def test_table_is_protected_and_retained(template):
    resource = table(template)

    assert resource["Properties"]["DeletionProtectionEnabled"] is True
    assert resource["Properties"]["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert resource["DeletionPolicy"] == "Retain" and resource["UpdateReplacePolicy"] == "Retain"
    assert "StreamSpecification" not in resource["Properties"]


def test_the_template_matches_the_phase_6_specification(template):
    definition = spec.table_definition(RUN_HISTORY_TABLE_NAME)
    props = table(template)["Properties"]

    assert props["TableName"] == definition["TableName"]
    assert props["BillingMode"] == definition["BillingMode"]
    assert props["KeySchema"] == definition["KeySchema"]
    assert sorted(props["AttributeDefinitions"], key=str) == sorted(definition["AttributeDefinitions"], key=str)
    assert props["GlobalSecondaryIndexes"] == definition["GlobalSecondaryIndexes"]
    assert props["TimeToLiveSpecification"] == definition["TimeToLiveSpecification"]
    assert props["DeletionProtectionEnabled"] == definition["DeletionProtectionEnabled"]
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] == definition["PointInTimeRecoveryEnabled"]
    assert props["SSESpecification"]["SSEEnabled"] == definition["SSESpecification"]["Enabled"]


def test_exports_the_name_and_arn_for_the_api_stack(template):
    template.has_output("RunHistoryTableName", {})
    template.has_output("RunHistoryTableArn", {})


def test_nothing_mentions_the_game(template):
    text = json.dumps(template.to_json()).lower()

    for host in GAME_HOSTS:
        assert host not in text
    assert "bluff" not in text and "believe" not in text


def test_app_synthesizes_the_table_without_any_certificates(tmp_path):
    result, names = run_app(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "IrishEnergyRunHistoryStack.template.json" in names


def test_table_name_is_valid_for_the_apps_settings():
    from src.settings import load_settings

    settings = load_settings(
        {
            "API_ENV": "local",
            "RUN_HISTORY_BACKEND": "dynamodb",
            "RUN_HISTORY_TABLE": RUN_HISTORY_TABLE_NAME,
        }
    )

    assert settings.run_history.table == RUN_HISTORY_TABLE_NAME
