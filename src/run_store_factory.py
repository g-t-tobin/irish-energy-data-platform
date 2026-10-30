"""Chooses the run-history backend from settings."""

from typing import Any

from src.run_store import RunHistory, RunStore
from src.settings import RunHistorySettings


def build_run_store(settings: RunHistorySettings, client: Any = None) -> RunHistory:
    if settings.backend == "dynamodb":
        import boto3

        from src.dynamodb_run_store import DynamoRunStore

        return DynamoRunStore(
            settings.table or "",
            client or boto3.client("dynamodb", region_name=settings.region),
        )
    return RunStore()
