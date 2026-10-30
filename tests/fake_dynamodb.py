"""In-memory stand-in for the boto3 DynamoDB client (only what DynamoRunStore uses).

Evaluates the small expression grammar the store emits and, like the real
service, rejects unused expression names/values and unknown attributes' syntax.
"""

import re
import threading
from typing import Any

from botocore.exceptions import ClientError


def _error(code: str, operation: str, **extra: Any) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}, **extra}, operation)


def _value(raw: dict[str, str]) -> Any:
    return float(raw["N"]) if "N" in raw else raw["S"]


class FakeDynamoDB:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_with: ClientError | None = None
        self._lock = threading.RLock()

    # -- expression evaluation ------------------------------------------

    def _check_usage(self, expressions: list[str], names: dict, values: dict, operation: str) -> None:
        text = " ".join(expressions)
        for key in names:
            if key not in text:
                raise _error("ValidationException", operation)
        for key in values:
            if key not in text:
                raise _error("ValidationException", operation)

    def _attr(self, token: str, names: dict) -> str:
        return names.get(token, token)

    def _clause(self, clause: str, item: dict | None, names: dict, values: dict) -> bool:
        clause = clause.strip()
        if match := re.fullmatch(r"attribute_not_exists\((\w+)\)", clause):
            return item is None or match[1] not in item
        if match := re.fullmatch(r"attribute_exists\((\w+)\)", clause):
            return item is not None and match[1] in item
        if match := re.fullmatch(r"(#?\w+) IN \(([^)]*)\)", clause):
            if item is None:
                return False
            current = item.get(self._attr(match[1], names))
            allowed = [values[t.strip()] for t in match[2].split(",")]
            return current in allowed
        if match := re.fullmatch(r"(#?\w+) (<|=) (:\w+)", clause):
            if item is None:
                return False
            current = item.get(self._attr(match[1], names))
            if current is None:
                return False
            left, right = _value(current), _value(values[match[3]])
            return left < right if match[2] == "<" else left == right
        raise AssertionError(f"unsupported condition clause: {clause}")

    def _condition(self, expression: str | None, item: dict | None, names: dict, values: dict) -> bool:
        if not expression:
            return True
        return any(
            all(self._clause(part, item, names, values) for part in alternative.split(" AND "))
            for alternative in expression.split(" OR ")
        )

    # -- API -------------------------------------------------------------

    def _maybe_fail(self, operation: str) -> None:
        self.calls.append(operation)
        if self.fail_with is not None:
            raise self.fail_with

    def _key(self, key: dict) -> tuple[str, str]:
        return key["pk"]["S"], key["sk"]["S"]

    def get_item(self, *, TableName, Key, ConsistentRead=False):
        self._maybe_fail("get_item")
        with self._lock:
            item = self.items.get(self._key(Key))
            return {"Item": dict(item)} if item else {}

    def put_item(self, *, TableName, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        self._maybe_fail("put_item")
        with self._lock:
            current = self.items.get(self._key(Item))
            if not self._condition(ConditionExpression, current, {}, ExpressionAttributeValues or {}):
                raise _error("ConditionalCheckFailedException", "PutItem")
            self.items[self._key(Item)] = dict(Item)

    def update_item(
        self,
        *,
        TableName,
        Key,
        UpdateExpression,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        self._maybe_fail("update_item")
        names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        self._check_usage([UpdateExpression, ConditionExpression or ""], names, values, "UpdateItem")
        with self._lock:
            current = self.items.get(self._key(Key))
            if not self._condition(ConditionExpression, current, names, values):
                raise _error("ConditionalCheckFailedException", "UpdateItem")
            item = dict(current) if current else dict(Key)
            assert UpdateExpression.startswith("SET ")
            for assignment in UpdateExpression[4:].split(","):
                target, token = (part.strip() for part in assignment.split("="))
                item[self._attr(target, names)] = values[token]
            self.items[self._key(Key)] = item

    def delete_item(
        self,
        *,
        TableName,
        Key,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        self._maybe_fail("delete_item")
        names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        self._check_usage([ConditionExpression or ""], names, values, "DeleteItem")
        with self._lock:
            current = self.items.get(self._key(Key))
            if not self._condition(ConditionExpression, current, names, values):
                raise _error("ConditionalCheckFailedException", "DeleteItem")
            self.items.pop(self._key(Key), None)

    def query(
        self,
        *,
        TableName,
        IndexName,
        KeyConditionExpression,
        ExpressionAttributeValues,
        ScanIndexForward=True,
        Limit=None,
    ):
        self._maybe_fail("query")
        assert IndexName == "gsi1" and KeyConditionExpression == "gsi1pk = :pk"
        partition = ExpressionAttributeValues[":pk"]["S"]
        with self._lock:
            matches = [
                dict(item)
                for item in self.items.values()
                if item.get("gsi1pk", {}).get("S") == partition
            ]
        matches.sort(key=lambda item: item["gsi1sk"]["S"], reverse=not ScanIndexForward)
        return {"Items": matches[:Limit] if Limit else matches}

    def transact_write_items(self, *, TransactItems):
        self._maybe_fail("transact_write_items")
        with self._lock:
            reasons, ok = [], True
            for entry in TransactItems:
                put = entry["Put"]
                current = self.items.get(self._key(put["Item"]))
                values = put.get("ExpressionAttributeValues") or {}
                allowed = self._condition(put.get("ConditionExpression"), current, {}, values)
                reasons.append({"Code": "None" if allowed else "ConditionalCheckFailed"})
                ok &= allowed
            if not ok:
                raise _error(
                    "TransactionCanceledException",
                    "TransactWriteItems",
                    CancellationReasons=reasons,
                )
            for entry in TransactItems:
                put = entry["Put"]
                self.items[self._key(put["Item"])] = dict(put["Item"])
