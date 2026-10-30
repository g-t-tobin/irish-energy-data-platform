"""Shared run history in DynamoDB (single table, on-demand).

Items (see run_history_schema.py):
- run:         pk=RUN#<run_id>   one per run; gsi1pk=RUNS, gsi1sk=<created>#<id> for listing
- lock:        pk=LOCK#pipeline  at most one; held while a run is active, expires by lease
- idempotency: pk=IDEM#<hash>    maps a repeated request to its run; DynamoDB TTL cleans up

All multi-item changes use one transaction so concurrent API tasks cannot both
start a run. Lock release after a run is best effort: if it is missed, the
lease expiry frees it.
"""

import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src import run_history_schema as schema
from src.run_store import (
    ACTIVE_STATUSES,
    INTERRUPTED_MESSAGE,
    TERMINAL_STATUSES,
    UPDATABLE_FIELDS,
    ActiveRunError,
    RunStoreError,
    validate_create_args,
)

UNAVAILABLE = "Run history storage is unavailable."
RECOVERY_SCAN_LIMIT = 50
MAX_LIST_LIMIT = 200
_RUN_ATTRIBUTES = (
    "run_id",
    "status",
    "stage",
    "days_back",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "bronze_key",
    "silver_key",
    "gold_key",
    "error",
    "error_code",
    "requested_by",
)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int | float) -> dict[str, str]:
    return {"N": str(value)}


def _decode(item: dict[str, Any]) -> dict[str, Any]:
    run: dict[str, Any] = {}
    for name in _RUN_ATTRIBUTES:
        value = item.get(name)
        if value is None:
            continue
        run[name] = int(value["N"]) if "N" in value else value["S"]
    return run


def _code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


class DynamoRunStore:
    def __init__(
        self,
        table_name: str,
        client: Any,
        *,
        clock: Callable[[], float] = time.time,
        lease_seconds: int = schema.LOCK_LEASE_SECONDS,
    ) -> None:
        self._table = table_name
        self._client = client
        self._clock = clock
        self._lease = lease_seconds

    # -- helpers ---------------------------------------------------------

    def _iso(self, now: float) -> str:
        return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="microseconds")

    def _run_key(self, run_id: str) -> dict[str, Any]:
        return {schema.PARTITION_KEY: _s(schema.RUN_PK_PREFIX + run_id), schema.SORT_KEY: _s(schema.RUN_SK)}

    def _lock_key(self) -> dict[str, Any]:
        return {schema.PARTITION_KEY: _s(schema.LOCK_PK), schema.SORT_KEY: _s(schema.LOCK_SK)}

    def _idem_key(self, requested_by: str | None, key: str) -> dict[str, Any]:
        digest = hashlib.sha256(f"{requested_by or ''}|{key}".encode()).hexdigest()
        return {
            schema.PARTITION_KEY: _s(schema.IDEMPOTENCY_PK_PREFIX + digest),
            schema.SORT_KEY: _s(schema.IDEMPOTENCY_SK),
        }

    def _call(self, operation: str, **kwargs: Any) -> Any:
        try:
            return getattr(self._client, operation)(**kwargs)
        except BotoCoreError as exc:
            raise RunStoreError(UNAVAILABLE) from exc

    def _conditional(self, operation: str, **kwargs: Any) -> bool:
        """Run a conditional write; False when the condition did not hold."""
        try:
            self._call(operation, **kwargs)
            return True
        except ClientError as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return False
            raise RunStoreError(UNAVAILABLE) from exc

    def _find_idempotent(self, requested_by: str | None, key: str, now: float) -> dict[str, Any] | None:
        try:
            response = self._call(
                "get_item",
                TableName=self._table,
                Key=self._idem_key(requested_by, key),
                ConsistentRead=True,
            )
        except ClientError as exc:
            raise RunStoreError(UNAVAILABLE) from exc
        item = response.get("Item")
        if not item or float(item[schema.TTL_ATTRIBUTE]["N"]) < now:
            return None
        run = self.get_run(item["run_id"]["S"])
        return {**run, "duplicate": True} if run else None

    # -- RunHistory interface -------------------------------------------

    def create_run(
        self,
        days_back: int,
        *,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Atomically take the active-run lock and record a queued run."""
        validate_create_args(days_back, idempotency_key)
        now = self._clock()
        if idempotency_key:
            existing = self._find_idempotent(requested_by, idempotency_key, now)
            if existing:
                return existing

        run_id = str(uuid.uuid4())
        created = self._iso(now)
        run_item: dict[str, Any] = {
            **self._run_key(run_id),
            "run_id": _s(run_id),
            "status": _s("queued"),
            "stage": _s("queued"),
            "days_back": _n(days_back),
            "created_at": _s(created),
            "updated_at": _s(created),
            schema.LIST_INDEX_PARTITION_KEY: _s(schema.RUN_LIST_PARTITION),
            schema.LIST_INDEX_SORT_KEY: _s(f"{created}#{run_id}"),
        }
        if requested_by:
            run_item["requested_by"] = _s(requested_by)
        items: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **self._lock_key(),
                        "run_id": _s(run_id),
                        "lease_expires_at": _n(int(now + self._lease)),
                    },
                    "ConditionExpression": "attribute_not_exists(pk) OR lease_expires_at < :now",
                    "ExpressionAttributeValues": {":now": _n(int(now))},
                }
            },
            {
                "Put": {
                    "TableName": self._table,
                    "Item": run_item,
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
        if idempotency_key:
            items.append(
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **self._idem_key(requested_by, idempotency_key),
                            "run_id": _s(run_id),
                            schema.TTL_ATTRIBUTE: _n(int(now + schema.IDEMPOTENCY_TTL_SECONDS)),
                        },
                        "ConditionExpression": "attribute_not_exists(pk) OR expires_at < :now",
                        "ExpressionAttributeValues": {":now": _n(int(now))},
                    }
                }
            )

        try:
            self._call("transact_write_items", TransactItems=items)
        except ClientError as exc:
            if _code(exc) != "TransactionCanceledException":
                raise RunStoreError(UNAVAILABLE) from exc
            reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
            # A lost idempotency race returns the winner's run, not an error.
            if idempotency_key and len(reasons) > 2 and reasons[2] == "ConditionalCheckFailed":
                existing = self._find_idempotent(requested_by, idempotency_key, self._clock())
                if existing:
                    return existing
            if reasons and reasons[0] == "ConditionalCheckFailed":
                raise ActiveRunError("A pipeline run is already active.") from None
            raise RunStoreError(UNAVAILABLE) from exc

        return {
            "run_id": run_id,
            "status": "queued",
            "stage": "queued",
            "days_back": days_back,
            "created_at": created,
            "updated_at": created,
        }

    def update_run(self, run_id: str, **fields: Any) -> bool:
        """Apply whitelisted fields; False if the run is missing or the change is not allowed.

        Active runs only accept stage/status progress; a terminal status is
        accepted from an active or interrupted run (the real outcome wins over
        a recovery guess) and releases the lock. Late progress updates cannot
        revive a finished run.
        """
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Cannot update fields: {sorted(unknown)}")
        now = self._clock()
        applied = {name: value for name, value in fields.items() if value is not None}
        status = applied.get("status")
        terminal = status in TERMINAL_STATUSES
        if terminal:
            applied.setdefault("finished_at", self._iso(now))
        applied["updated_at"] = self._iso(now)

        # Every attribute is aliased: several names (status, error) are reserved words.
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        sets = []
        for index, (name, value) in enumerate(applied.items()):
            names[f"#f{index}"] = name
            sets.append(f"#f{index} = :v{index}")
            values[f":v{index}"] = _n(value) if isinstance(value, int) else _s(value)
        if terminal:
            names["#st"] = "status"
            values.update({":queued": _s("queued"), ":running": _s("running"), ":interrupted": _s("interrupted")})
            condition = "attribute_exists(pk) AND #st IN (:queued, :running, :interrupted)"
        elif status is not None or "stage" in fields:
            names["#st"] = "status"
            values.update({":queued": _s("queued"), ":running": _s("running")})
            condition = "attribute_exists(pk) AND #st IN (:queued, :running)"
        else:
            condition = "attribute_exists(pk)"

        arguments: dict[str, Any] = {
            "TableName": self._table,
            "Key": self._run_key(run_id),
            "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if not self._conditional("update_item", **arguments):
            return False

        if terminal:
            self._conditional(
                "delete_item",
                TableName=self._table,
                Key=self._lock_key(),
                ConditionExpression="run_id = :rid",
                ExpressionAttributeValues={":rid": _s(run_id)},
            )
        elif status is not None or "stage" in fields:
            # Progress doubles as the heartbeat that keeps the lease alive.
            self._conditional(
                "update_item",
                TableName=self._table,
                Key=self._lock_key(),
                UpdateExpression="SET lease_expires_at = :lease",
                ConditionExpression="run_id = :rid",
                ExpressionAttributeValues={":lease": _n(int(now + self._lease)), ":rid": _s(run_id)},
            )
        return True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        try:
            response = self._call(
                "get_item", TableName=self._table, Key=self._run_key(run_id), ConsistentRead=True
            )
        except ClientError as exc:
            raise RunStoreError(UNAVAILABLE) from exc
        item = response.get("Item")
        return _decode(item) if item else None

    def list_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        """Newest first, from the listing index (one small partition; see ADR)."""
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        try:
            response = self._call(
                "query",
                TableName=self._table,
                IndexName=schema.LIST_INDEX,
                KeyConditionExpression="gsi1pk = :pk",
                ExpressionAttributeValues={":pk": _s(schema.RUN_LIST_PARTITION)},
                ScanIndexForward=False,
                Limit=limit,
            )
        except ClientError as exc:
            raise RunStoreError(UNAVAILABLE) from exc
        return [_decode(item) for item in response.get("Items", [])]

    def get_lock(self) -> dict[str, Any] | None:
        try:
            item = self._call(
                "get_item", TableName=self._table, Key=self._lock_key(), ConsistentRead=True
            ).get("Item")
        except ClientError as exc:
            raise RunStoreError(UNAVAILABLE) from exc
        if not item:
            return None
        return {"run_id": item["run_id"]["S"], "lease_expires_at": int(float(item["lease_expires_at"]["N"]))}

    def renew_lock(self, run_id: str) -> bool:
        """Extend the lease from now; only the owning run can, so another run's lock is never touched."""
        return self._conditional(
            "update_item",
            TableName=self._table,
            Key=self._lock_key(),
            UpdateExpression="SET lease_expires_at = :lease",
            ConditionExpression="run_id = :rid",
            ExpressionAttributeValues={":lease": _n(int(self._clock() + self._lease)), ":rid": _s(run_id)},
        )

    def release_lock(self, run_id: str) -> bool:
        return self._conditional(
            "delete_item",
            TableName=self._table,
            Key=self._lock_key(),
            ConditionExpression="run_id = :rid",
            ExpressionAttributeValues={":rid": _s(run_id)},
        )

    def recover_interrupted(self) -> int:
        """Mark active runs whose lease has expired as interrupted.

        Safe with several API tasks: a run holding a live lease is left alone.
        """
        now = self._clock()
        try:
            lock = self._call(
                "get_item", TableName=self._table, Key=self._lock_key(), ConsistentRead=True
            ).get("Item")
        except ClientError as exc:
            raise RunStoreError(UNAVAILABLE) from exc
        live_run = (
            lock["run_id"]["S"] if lock and float(lock["lease_expires_at"]["N"]) >= now else None
        )

        recovered = 0
        stamp = self._iso(now)
        for run in self.list_runs(RECOVERY_SCAN_LIMIT):
            if run.get("status") not in ACTIVE_STATUSES or run["run_id"] == live_run:
                continue
            marked = self._conditional(
                "update_item",
                TableName=self._table,
                Key=self._run_key(run["run_id"]),
                UpdateExpression=(
                    "SET #st = :interrupted, #err = :msg, #code = :code,"
                    " #fin = :ts, #upd = :ts"
                ),
                ConditionExpression="#st IN (:queued, :running)",
                ExpressionAttributeNames={
                    "#st": "status",
                    "#err": "error",
                    "#code": "error_code",
                    "#fin": "finished_at",
                    "#upd": "updated_at",
                },
                ExpressionAttributeValues={
                    ":interrupted": _s("interrupted"),
                    ":msg": _s(INTERRUPTED_MESSAGE),
                    ":code": _s("INTERRUPTED"),
                    ":ts": _s(stamp),
                    ":queued": _s("queued"),
                    ":running": _s("running"),
                },
            )
            recovered += int(marked)

        if lock and live_run is None:
            self._conditional(
                "delete_item",
                TableName=self._table,
                Key=self._lock_key(),
                ConditionExpression="run_id = :rid AND lease_expires_at < :now",
                ExpressionAttributeValues={":rid": lock["run_id"], ":now": _n(int(now))},
            )
        return recovered
