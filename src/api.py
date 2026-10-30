
"""Local API for controlling and monitoring the energy data pipeline."""

import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.auth import (
    JwksKeyProvider,
    KeyProvider,
    Principal,
    TokenVerifier,
    require_operator,
    require_viewer,
)
from src.executor import (
    DisabledExecutor,
    ExecutionDisabledError,
    ExecutionStartError,
    PipelineExecutor,
)
from src.logging_config import configure_logging, request_id_var
from src.ratelimit import RateLimiter, rate_limit
from src.run_store import (
    ActiveRunError,
    RunHistory,
    RunStoreError,
    classify_error,
    safe_error_message,
    to_public_run,
    utc_now,
)
from src.run_store_factory import build_run_store
from src.settings import Settings, load_run_history_settings, load_settings

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 4096  # the only body is {"days_back": n}
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_store: RunHistory | None = None
_store_lock = threading.Lock()


def get_store() -> RunHistory:
    """Create the run store on first use (and never at import time)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = build_run_store(load_run_history_settings(os.environ))
        return _store


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    if getattr(app.state, "verifier", None) is None:
        logger.error(
            "Authentication is not configured; protected routes will return 503."
        )
    # Runs left active by a previous process cannot be trusted as finished.
    try:
        recovered = get_store().recover_interrupted()
        if recovered:
            logger.warning("Marked %d interrupted run(s) from a previous process", recovered)
    except RunStoreError:
        logger.exception("Run history storage unavailable at startup")
    yield


router = APIRouter()
_executor: PipelineExecutor = DisabledExecutor()


def get_executor() -> PipelineExecutor:
    return _executor


class RunRequest(BaseModel):
    days_back: int = Field(default=2, ge=1, le=30)


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503, detail="Run history storage is unavailable."
    )


def execute_pipeline(run_id: str, days_back: int) -> None:
    """Run the existing pipeline and record its outcome."""
    store = get_store()
    stage = "starting"
    gold_key: str | None = None

    try:
        store.update_run(
            run_id, status="running", stage=stage, started_at=utc_now()
        )

        # Import here so the API health endpoint can start even if a pipeline
        # dependency is unavailable.
        from src.gold import process_silver_object
        from src.ingestion.eirgrid import fetch_demand_data, save_to_s3
        from src.pipeline import register_gold_partition
        from src.transformation import process_bronze_object

        bucket = os.environ.get("S3_BUCKET")
        if not bucket:
            raise RuntimeError("S3_BUCKET environment variable is not set.")

        os.environ["EIRGRID_DAYS_BACK"] = str(days_back)

        stage = "bronze"
        store.update_run(run_id, stage=stage)
        payload = fetch_demand_data(days_back=days_back)
        bronze_key = save_to_s3(payload, bucket)

        stage = "silver"
        store.update_run(run_id, bronze_key=bronze_key, stage=stage)
        silver_key = process_bronze_object(bucket, bronze_key)

        stage = "gold"
        store.update_run(run_id, silver_key=silver_key, stage=stage)
        gold_key = process_silver_object(bucket, silver_key)

        stage = "glue_registration"
        store.update_run(run_id, gold_key=gold_key, stage=stage)
        register_gold_partition(bucket, gold_key)

        store.update_run(run_id, status="succeeded", stage="completed")

    except Exception as exc:
        # The traceback goes to the server log only, never to the database.
        logger.exception("Pipeline run %s failed", run_id)
        try:
            store.update_run(
                run_id,
                status="partial_failure" if gold_key else "failed",
                stage=stage,
                error=safe_error_message(exc),
                error_code=classify_error(exc),
            )
        except RunStoreError:
            logger.exception("Could not record failure for run %s", run_id)

    finally:
        try:
            store.update_run(run_id, finished_at=utc_now())
        except RunStoreError:
            logger.exception("Could not record finish time for run %s", run_id)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/api/runs",
    dependencies=[Depends(require_viewer), Depends(rate_limit("read"))],
)
def list_runs() -> dict[str, list[dict[str, Any]]]:
    try:
        return {"runs": [to_public_run(r) for r in get_store().list_runs()]}
    except RunStoreError as exc:
        raise _storage_unavailable() from exc


@router.get(
    "/api/analytics/daily-demand",
    dependencies=[Depends(require_viewer), Depends(rate_limit("analytics"))],
)
def daily_demand(days: int = Query(default=30, ge=1, le=90)) -> dict[str, Any]:
    """Read-only Gold-layer daily demand metrics from S3."""
    from src.analytics import (
        PUBLIC_UNAVAILABLE_MESSAGE,
        AnalyticsUnavailable,
        get_daily_demand,
    )

    try:
        return get_daily_demand(days)
    except AnalyticsUnavailable as exc:
        logger.error("Analytics unavailable: %s", exc, exc_info=exc.__cause__)
        raise HTTPException(
            status_code=503, detail=PUBLIC_UNAVAILABLE_MESSAGE
        ) from None


@router.post(
    "/api/runs",
    status_code=202,
    dependencies=[Depends(require_operator), Depends(rate_limit("write"))],
)
def start_run(
    request: RunRequest,
    executor: PipelineExecutor = Depends(get_executor),
    principal: Principal = Depends(require_operator),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", pattern=r"^[A-Za-z0-9_-]{8,64}$"
    ),
) -> dict[str, Any]:
    try:
        return executor.start(
            request.days_back,
            requested_by=principal.sub,
            idempotency_key=idempotency_key,
        )
    except ExecutionDisabledError as exc:
        raise HTTPException(
            status_code=503, detail="Pipeline execution is disabled."
        ) from exc
    except ActiveRunError as exc:
        raise HTTPException(
            status_code=409, detail="A pipeline run is already active."
        ) from exc
    except ExecutionStartError as exc:
        raise HTTPException(
            status_code=503, detail="The pipeline could not be started."
        ) from exc
    except RunStoreError as exc:
        raise _storage_unavailable() from exc


@router.get(
    "/api/runs/{run_id}",
    dependencies=[Depends(require_viewer), Depends(rate_limit("read"))],
)
def get_run(run_id: str) -> dict[str, Any]:
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=404, detail="Run not found.")
    try:
        run = get_store().get_run(run_id)
    except RunStoreError as exc:
        raise _storage_unavailable() from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return to_public_run(run)


def _reject_oversized(request: Request) -> JSONResponse | None:
    """Refuse large or unsized bodies before any route or authentication work."""
    length = request.headers.get("content-length")
    if "transfer-encoding" in request.headers:
        return JSONResponse({"detail": "Content-Length is required."}, status_code=411)
    if length is not None:
        if not length.isdigit():
            return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
        if int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request body too large."}, status_code=413)
    return None


def create_app(settings: Settings, key_provider: KeyProvider | None = None) -> FastAPI:
    docs = settings.enable_api_docs
    app = FastAPI(
        title="Irish Energy Analytics API",
        version="0.1.0",
        description="Local control API for the EirGrid data pipeline.",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            rejected = _reject_oversized(request)
            try:
                response = rejected or await call_next(request)
            except Exception:
                # Detail and traceback go to the log, never to the client.
                logger.exception("Unhandled error")
                response = JSONResponse(
                    {"detail": "Internal server error", "request_id": request_id},
                    status_code=500,
                )
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            route = request.scope.get("route")
            logger.info(
                "request method=%s route=%s status=%d duration_ms=%d sub=%s",
                request.method,
                getattr(route, "path", "-"),
                response.status_code,
                (time.perf_counter() - started) * 1000,
                getattr(request.state, "principal_sub", "-"),
            )
            return response
        finally:
            request_id_var.reset(token)

    # Added after the middleware above so CORS wraps every response, 500s included.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    cognito = settings.cognito
    app.state.verifier = (
        TokenVerifier(cognito, key_provider or JwksKeyProvider(cognito.jwks_url))
        if cognito
        else None
    )
    limits = settings.rate_limits
    app.state.rate_limiter = RateLimiter(
        {
            "read": (limits.read_per_minute, 60.0),
            "analytics": (limits.analytics_per_minute, 60.0),
            "write": (limits.write_per_hour, 3600.0),
            "auth_failure": (limits.auth_failures_per_minute, 60.0),
        }
    )
    app.include_router(router)
    return app


_app: FastAPI | None = None
_app_lock = threading.Lock()


def __getattr__(name: str) -> Any:
    """Build `app` on first access so invalid production config stops startup."""
    global _app
    if name != "app":
        raise AttributeError(name)
    with _app_lock:
        if _app is None:
            _app = create_app(load_settings())
        return _app
