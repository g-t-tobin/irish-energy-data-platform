"""IAM permissions must cover exactly what the run store does, including its write transaction.

AWS authorises TransactWriteItems through the permissions of the operations inside it
(Put -> dynamodb:PutItem, Update -> UpdateItem, Delete -> DeleteItem, ConditionCheck ->
dynamodb:ConditionCheckItem). There is no dynamodb:TransactWriteItems IAM action:
https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html
"""

import json
import re
from pathlib import Path

import pytest

from src.dynamodb_run_store import DynamoRunStore
from src.run_history_schema import LOCK_LEASE_SECONDS
from tests.fake_dynamodb import FakeDynamoDB
from tests.test_energy_api_service_infrastructure import as_list, policy_for, synth
from tests.test_run_store_contract import Clock

SOURCE = (Path(__file__).resolve().parents[1] / "src" / "dynamodb_run_store.py").read_text()
DIRECT = {
    "get_item": "dynamodb:GetItem",
    "put_item": "dynamodb:PutItem",
    "update_item": "dynamodb:UpdateItem",
    "delete_item": "dynamodb:DeleteItem",
    "query": "dynamodb:Query",
}
TRANSACTION_ITEMS = {
    "Put": "dynamodb:PutItem",
    "Update": "dynamodb:UpdateItem",
    "Delete": "dynamodb:DeleteItem",
    "ConditionCheck": "dynamodb:ConditionCheckItem",
    "Get": "dynamodb:GetItem",
}


def exercise_store():
    """Run every store code path against the double and record what it called."""
    clock, fake = Clock(), FakeDynamoDB()
    store = DynamoRunStore("t", fake, clock=clock)
    run = store.create_run(2, requested_by="u", idempotency_key="idem-key-0001")
    store.create_run(2, requested_by="u", idempotency_key="idem-key-0001")
    store.update_run(run["run_id"], status="running", stage="bronze")
    store.update_run(run["run_id"], finished_at="2026-10-30T10:00:00+00:00")
    store.update_run(run["run_id"], status="succeeded", stage="completed")
    store.get_run(run["run_id"])
    store.list_runs()
    store.create_run(2)
    clock.advance(LOCK_LEASE_SECONDS + 1)
    store.recover_interrupted()
    return fake


def operations_named_in_source():
    return set(re.findall(r'(?:_call|_conditional)\(\s*"(\w+)"', SOURCE))


def required_iam_actions():
    fake = exercise_store()
    observed = set(fake.calls)
    # The scenario must have reached every operation the source can call.
    assert observed >= operations_named_in_source()
    actions = set()
    for call in observed - {"transact_write_items"}:
        actions.add(DIRECT[call])  # an unexpected operation (Scan, Batch...) raises KeyError here
    if "transact_write_items" in observed:
        assert fake.transaction_item_types, "transaction contents were not recorded"
        actions |= {TRANSACTION_ITEMS[kind] for kind in fake.transaction_item_types}
    return actions


@pytest.fixture(scope="module")
def edge():
    return synth()[1]


def endpoint_statements(edge):
    for resource in edge.find_resources("AWS::EC2::VPCEndpoint").values():
        if json.dumps(resource["Properties"]["ServiceName"]).endswith('.dynamodb"]]}'):
            return resource["Properties"]["PolicyDocument"]["Statement"]
    raise AssertionError("no DynamoDB endpoint")


def granted(statements):
    return {a for s in statements for a in as_list(s["Action"])}


def test_the_store_uses_a_write_transaction_containing_only_put_items():
    fake = exercise_store()

    assert "transact_write_items" in fake.calls
    assert fake.transaction_item_types == {"Put"}
    assert '"Update": {' not in SOURCE and '"Delete": {' not in SOURCE and '"ConditionCheck"' not in SOURCE
    assert operations_named_in_source() <= set(DIRECT) | {"transact_write_items"}  # no scan, batch or transact_get_items


def test_the_exact_iam_actions_the_store_needs():
    assert required_iam_actions() == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",  # direct puts and the Put items of TransactWriteItems
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
    }


def test_the_task_role_grants_every_needed_action_and_nothing_more(edge):
    statements = [s for s in policy_for(edge, "ApiServiceTaskRole") if s["Sid"].startswith("RunHistory")]

    assert granted(statements) == required_iam_actions()


def test_the_endpoint_policy_permits_every_needed_action_including_the_transaction(edge):
    statements = endpoint_statements(edge)

    assert granted(statements) == required_iam_actions()
    assert "dynamodb:PutItem" in granted(statements)  # what authorises the Put items in the transaction


def test_transaction_put_permission_is_scoped_to_the_table_arn_only(edge):
    table_arn = {"Fn::ImportValue": "IrishEnergyRunHistoryStack:ExportsOutputFnGetAttRunHistoryTableFD4DA44DArn6CF8D0FC"}
    role = {s["Sid"]: s for s in policy_for(edge, "ApiServiceTaskRole")}
    endpoint = {s["Sid"]: s for s in endpoint_statements(edge)}

    for statement in (role["RunHistoryItems"], endpoint["RunHistoryItemOperations"]):
        assert "dynamodb:PutItem" in as_list(statement["Action"])
        assert as_list(statement["Resource"]) == [table_arn]  # never the gsi1 index
    for statement in (role["RunHistoryListIndex"], endpoint["RunHistoryListIndexQuery"]):
        assert statement["Action"] == "dynamodb:Query"
        assert "/index/gsi1" in json.dumps(statement["Resource"])


def test_no_nonexistent_transaction_actions_scan_batch_or_management_are_granted(edge):
    everything = granted(endpoint_statements(edge)) | granted(
        s for s in policy_for(edge, "ApiServiceTaskRole") if s["Sid"].startswith("RunHistory")
    )

    assert everything == required_iam_actions()
    for forbidden in (
        "dynamodb:TransactWriteItems", "dynamodb:TransactGetItems", "dynamodb:ConditionCheckItem",
        "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem", "dynamodb:CreateTable",
        "dynamodb:DeleteTable", "dynamodb:UpdateTable", "dynamodb:DescribeTable",
    ):
        assert forbidden not in everything


def test_adding_other_transaction_item_kinds_would_require_new_permissions():
    """Guard: if the store ever adds Update/Delete/ConditionCheck items, this mapping flags the IAM change."""
    assert TRANSACTION_ITEMS["ConditionCheck"] == "dynamodb:ConditionCheckItem"
    assert TRANSACTION_ITEMS["Put"] == "dynamodb:PutItem"
