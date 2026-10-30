"""Shared DynamoDB layout for run history; the store and infrastructure spec both use it."""

PARTITION_KEY = "pk"
SORT_KEY = "sk"
LIST_INDEX = "gsi1"
LIST_INDEX_PARTITION_KEY = "gsi1pk"
LIST_INDEX_SORT_KEY = "gsi1sk"
TTL_ATTRIBUTE = "expires_at"

RUN_PK_PREFIX = "RUN#"
IDEMPOTENCY_PK_PREFIX = "IDEM#"
LOCK_PK = "LOCK#pipeline"
RUN_SK = "RUN"
LOCK_SK = "LOCK"
IDEMPOTENCY_SK = "IDEM"
RUN_LIST_PARTITION = "RUNS"

# Longer than the state machine timeout so a healthy run never loses the lock.
LOCK_LEASE_SECONDS = 45 * 60
STATE_MACHINE_TIMEOUT_SECONDS = 30 * 60
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
