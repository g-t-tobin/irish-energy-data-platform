"""Run-history interface and its local SQLite implementation.

SQLite suits a single local API process only. A SQLite file on an ECS task's
ephemeral filesystem is lost on task replacement and cannot be shared between
tasks; deployments use `DynamoRunStore` (src/dynamodb_run_store.py), which
implements the same `RunHistory` interface.
"""

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / ".local" / "run_history.sqlite3"
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "partial_failure", "interrupted")
IDEMPOTENCY_WINDOW_SECONDS = 24 * 3600
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
INTERRUPTED_MESSAGE = (
    "The API process stopped before this run finished; its outcome is unknown. "
    "Check the S3 data layers before retrying."
)
MAX_ERROR_LENGTH = 300

UPDATABLE_FIELDS = {
    "error_code",
    "status",
    "stage",
    "started_at",
    "finished_at",
    "bronze_key",
    "silver_key",
    "gold_key",
    "error",
}
_COLUMNS = (
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
)
_SECRET_PATTERNS = [
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[redacted-key]"),
    (
        re.compile(r"(?i)(secret|token|password|session)[\w\-]*\s*[=:]\s*\S+"),
        "[redacted]",
    ),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    days_back   INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    bronze_key  TEXT,
    silver_key  TEXT,
    gold_key    TEXT,
    error       TEXT,
    error_code  TEXT,
    requested_by    TEXT,
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at DESC);
"""


class RunHistory(Protocol):
    """What the API needs from a run-history backend (SQLite or DynamoDB)."""

    def create_run(
        self,
        days_back: int,
        *,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    def update_run(self, run_id: str, **fields: Any) -> bool: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def list_runs(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def recover_interrupted(self) -> int: ...


def validate_create_args(days_back: Any, idempotency_key: str | None) -> None:
    if isinstance(days_back, bool) or not isinstance(days_back, int):
        raise ValueError("days_back must be an integer")
    if not 1 <= days_back <= 30:
        raise ValueError("days_back must be between 1 and 30")
    if idempotency_key is not None and not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise ValueError("idempotency_key must be 8-64 letters, digits, '-' or '_'")


class ActiveRunError(Exception):
    """A queued or running run already exists."""


class RunStoreError(Exception):
    """Storage failed; the message is safe to return to API callers."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error_message(exc: BaseException) -> str:
    """Short, redacted description of an exception; never a traceback."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    if code:
        return f"{type(exc).__name__} ({code})"
    text = f"{type(exc).__name__}: {exc}"
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:MAX_ERROR_LENGTH]


_ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "ExpiredToken",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
}
PUBLIC_ERROR_MESSAGES = {
    "ACCESS_DENIED": "The pipeline was denied access to a required AWS service.",
    "UPSTREAM_UNAVAILABLE": "A data source or AWS service was unavailable.",
    "VALIDATION_FAILED": "The source data failed validation.",
    "STORAGE_ERROR": "Run history storage failed.",
    "INTERRUPTED": INTERRUPTED_MESSAGE,
    "INTERNAL_ERROR": "The pipeline run failed.",
}
PUBLIC_RUN_FIELDS = (
    "run_id",
    "status",
    "stage",
    "days_back",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
)


def classify_error(exc: BaseException) -> str:
    """Coarse, client-safe category for an exception."""
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    module = type(exc).__module__
    if code in _ACCESS_DENIED_CODES or isinstance(exc, PermissionError):
        return "ACCESS_DENIED"
    if code or module.startswith(("botocore", "requests", "urllib3")):
        return "UPSTREAM_UNAVAILABLE"
    if isinstance(exc, (RunStoreError, sqlite3.Error)):
        return "STORAGE_ERROR"
    if isinstance(exc, ValueError):
        return "VALIDATION_FAILED"
    return "INTERNAL_ERROR"


def to_public_run(run: dict[str, Any]) -> dict[str, Any]:
    """Whitelist client-visible fields; S3 keys and raw errors stay internal."""
    public = {name: run[name] for name in PUBLIC_RUN_FIELDS if name in run}
    if run.get("error") or run.get("error_code"):
        code = run.get("error_code")
        if code not in PUBLIC_ERROR_MESSAGES:
            code = "INTERNAL_ERROR"
        public["error_code"] = code
        public["error"] = PUBLIC_ERROR_MESSAGES[code]
    return public


def default_db_path() -> Path:
    return Path(os.environ.get("RUN_HISTORY_DB_PATH") or DEFAULT_DB_PATH)


def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if row[key] is not None}


class RunStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
                for added in ("error_code", "requested_by", "idempotency_key"):
                    if added not in columns:
                        conn.execute(f"ALTER TABLE runs ADD COLUMN {added} TEXT")
        except (OSError, sqlite3.Error) as exc:
            raise RunStoreError("Run history storage is unavailable.") from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Autocommit mode; write paths open explicit BEGIN IMMEDIATE transactions.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise RunStoreError("Run history storage is unavailable.") from exc

    def create_run(
        self,
        days_back: int,
        *,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Insert a queued run unless another run is already active.

        A repeated idempotency key from the same requester within 24 hours
        returns the original run (with `duplicate: True`) and creates nothing.
        """
        validate_create_args(days_back, idempotency_key)

        run_id = str(uuid.uuid4())
        now = utc_now()
        with self._write() as conn:
            if idempotency_key:
                cutoff = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() - IDEMPOTENCY_WINDOW_SECONDS,
                    timezone.utc,
                ).isoformat()
                existing = conn.execute(
                    "SELECT * FROM runs WHERE idempotency_key = ?"
                    " AND requested_by IS ? AND created_at >= ? LIMIT 1",
                    (idempotency_key, requested_by, cutoff),
                ).fetchone()
                if existing:
                    return {**_to_dict(existing), "duplicate": True}
            active = conn.execute(
                "SELECT 1 FROM runs WHERE status IN (?, ?) LIMIT 1",
                ACTIVE_STATUSES,
            ).fetchone()
            if active:
                raise ActiveRunError("A pipeline run is already active.")
            conn.execute(
                "INSERT INTO runs (run_id, status, stage, days_back, created_at,"
                " updated_at, requested_by, idempotency_key)"
                " VALUES (?, 'queued', 'queued', ?, ?, ?, ?, ?)",
                (run_id, days_back, now, now, requested_by, idempotency_key),
            )
        return {
            "run_id": run_id,
            "status": "queued",
            "stage": "queued",
            "days_back": days_back,
            "created_at": now,
            "updated_at": now,
        }

    def update_run(self, run_id: str, **fields: Any) -> bool:
        """Update whitelisted fields; return False if the run does not exist."""
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Cannot update fields: {sorted(unknown)}")
        if not fields:
            return self.get_run(run_id) is not None

        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._write() as conn:
            cursor = conn.execute(
                f"UPDATE runs SET {assignments}, updated_at = ? WHERE run_id = ?",
                (*fields.values(), utc_now(), run_id),
            )
            return cursor.rowcount == 1

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise RunStoreError("Run history storage is unavailable.") from exc
        return _to_dict(row) if row else None

    def list_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return runs newest first."""
        limit = max(1, min(int(limit), 1000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RunStoreError("Run history storage is unavailable.") from exc
        return [_to_dict(row) for row in rows]

    def recover_interrupted(self) -> int:
        """Mark runs left queued/running by a previous process as interrupted.

        Assumes a single API process owns this database. They are never
        reported as succeeded because their real outcome is unknown.
        """
        now = utc_now()
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'interrupted', error = ?,"
                " error_code = 'INTERRUPTED', finished_at = ?, updated_at = ?"
                " WHERE status IN (?, ?)",
                (INTERRUPTED_MESSAGE, now, now, *ACTIVE_STATUSES),
            )
            return cursor.rowcount
