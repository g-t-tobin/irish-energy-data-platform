# syntax=docker/dockerfile:1
# Production image for the Irish Energy Analytics API. Build from the repository root:
#   docker build --platform linux/amd64 -t irish-energy-api:local .

FROM python:3.13-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements-api.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements-api.txt

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    RUN_HISTORY_DB_PATH=/tmp/run_history.sqlite3
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY src ./src
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]
# Configuration (API_ENV, COGNITO_*, S3_BUCKET, ...) comes from the environment at run time.
# Behind a proxy set FORWARDED_ALLOW_IPS to the proxy network so client addresses are real.
CMD ["sh", "-c", "exec uvicorn src.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --no-server-header --timeout-graceful-shutdown 20"]
