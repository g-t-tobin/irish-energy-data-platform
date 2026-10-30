"""AWS Lambda handlers for the four pipeline stages.

Each handler runs exactly one stage with the existing processing code and returns
`{"key": ...}`; none calls another stage. Step Functions passes the whole execution state as
the event (`run_id`, `days_back` and earlier results as `<stage>.output.key`) and chains them.
Errors are never caught here, so a failed stage fails the invocation.
"""

import logging
import os
import re
from typing import Any

from src.gold import process_silver_object
from src.ingestion.eirgrid import fetch_demand_data, save_to_s3
from src.pipeline import GOLD_KEY_PATTERN, register_gold_partition
from src.transformation import process_bronze_object

logger = logging.getLogger(__name__)

MAX_DAYS_BACK = 30  # same bound as the API request model
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_BRONZE_KEY_RE = re.compile(
    r"^bronze/eirgrid/demand_actual/ingestion_date=\d{4}-\d{2}-\d{2}/[^/]+\.json$"
)
_SILVER_KEY_RE = re.compile(
    r"^silver/eirgrid/demand_actual/ingestion_date=\d{4}-\d{2}-\d{2}/[^/]+\.parquet$"
)


class StageInputError(ValueError):
    """The event does not match the stage's input contract."""


class StageConfigurationError(RuntimeError):
    """The function environment is incomplete."""


def _bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise StageConfigurationError("S3_BUCKET is not set")
    return bucket


def _run_id(event: Any) -> str:
    if not isinstance(event, dict):
        raise StageInputError("event must be an object")
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise StageInputError("run_id must be a UUID")
    return run_id


def _days_back(event: dict) -> int:
    days_back = event.get("days_back")
    if isinstance(days_back, bool) or not isinstance(days_back, int):
        raise StageInputError("days_back must be an integer")
    if not 1 <= days_back <= MAX_DAYS_BACK:
        raise StageInputError(f"days_back must be between 1 and {MAX_DAYS_BACK}")
    return days_back


def _upstream_key(event: dict, stage: str, pattern: re.Pattern[str]) -> str:
    result = event.get(stage)
    output = result.get("output") if isinstance(result, dict) else None
    key = output.get("key") if isinstance(output, dict) else None
    if not isinstance(key, str) or not pattern.match(key):
        raise StageInputError(f"{stage}.output.key is missing or not a valid object key")
    return key


def ingest_handler(event: Any, context: Any = None) -> dict[str, Any]:
    run_id = _run_id(event)
    days_back = _days_back(event)
    bucket = _bucket()
    payload = fetch_demand_data(days_back=days_back)
    key = save_to_s3(payload, bucket)
    logger.info("stage=bronze run_id=%s records=%d key=%s", run_id, len(payload["Rows"]), key)
    return {"key": key, "records": len(payload["Rows"])}


def silver_handler(event: Any, context: Any = None) -> dict[str, Any]:
    run_id = _run_id(event)
    bronze_key = _upstream_key(event, "bronze", _BRONZE_KEY_RE)
    key = process_bronze_object(_bucket(), bronze_key)
    logger.info("stage=silver run_id=%s input=%s key=%s", run_id, bronze_key, key)
    return {"key": key}


def gold_handler(event: Any, context: Any = None) -> dict[str, Any]:
    run_id = _run_id(event)
    silver_key = _upstream_key(event, "silver", _SILVER_KEY_RE)
    key = process_silver_object(_bucket(), silver_key)
    logger.info("stage=gold run_id=%s input=%s key=%s", run_id, silver_key, key)
    return {"key": key}


def glue_handler(event: Any, context: Any = None) -> dict[str, Any]:
    run_id = _run_id(event)
    gold_key = _upstream_key(event, "gold", GOLD_KEY_PATTERN)
    created = register_gold_partition(_bucket(), gold_key)
    logger.info("stage=glue_registration run_id=%s key=%s created=%s", run_id, gold_key, created)
    return {"key": gold_key, "partition_created": created}
