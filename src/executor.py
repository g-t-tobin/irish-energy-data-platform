"""Pipeline execution gate. Only a disabled executor is wired into the API."""

from typing import Any, Protocol


class ExecutionDisabledError(Exception):
    """Pipeline execution is not enabled."""


class ExecutionStartError(Exception):
    """The execution service rejected or failed the start; detail is internal."""


class PipelineExecutor(Protocol):
    def start(
        self,
        days_back: int,
        *,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...


class DisabledExecutor:
    """Refuses every request; creates no run record and starts nothing."""

    def start(
        self,
        days_back: int,
        *,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        raise ExecutionDisabledError("Pipeline execution is disabled.")
