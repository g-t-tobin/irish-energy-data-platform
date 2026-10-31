"""Ingest Irish electricity demand data from EirGrid into S3 Bronze."""

import json
import os
from datetime import UTC, datetime, timedelta

import boto3
import requests

from src.run_history_schema import RUN_ID_PATTERN

EIRGRID_URL = "https://www.smartgriddashboard.com/DashboardService.svc/data"
DEFAULT_TIMEOUT_SECONDS = 30
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class TransientEirGridError(requests.RequestException):
    """EirGrid was temporarily unreachable or overloaded; the same request may succeed later.

    The state machine retries this error by name. Everything else (other HTTP errors, bad
    input, unexpected response shapes) is permanent for the run and is never retried.
    """

    error_code = "UPSTREAM_UNAVAILABLE"


def fetch_demand_data(days_back: int = 1) -> dict:
    """Fetch ROI electricity demand data from EirGrid."""
    if days_back < 1:
        raise ValueError("days_back must be at least 1")

    today = datetime.now(UTC).date()
    params = {
        "area": "demandActual",
        "region": "ROI",
        "datefrom": (today - timedelta(days=days_back)).strftime("%d-%b-%Y"),
        "dateto": (today + timedelta(days=1)).strftime("%d-%b-%Y"),
    }

    try:
        response = requests.get(
            EIRGRID_URL,
            params=params,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.SSLError:
        raise  # a certificate problem is configuration, not a passing outage
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TransientEirGridError("EirGrid could not be reached (timeout or connection error)") from exc
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in TRANSIENT_HTTP_STATUSES:
            raise TransientEirGridError(f"EirGrid returned HTTP {status}") from exc
        raise
    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Unexpected EirGrid response: expected a JSON object")  # noqa: TRY004 - malformed upstream response

    if payload.get("Status") == "Error" or payload.get("ErrorMessage"):
        raise ValueError(
            f"EirGrid API error: {payload.get('ErrorMessage', 'Unknown error')}"
        )

    if not isinstance(payload.get("Rows"), list):
        raise ValueError("Unexpected EirGrid response: expected a Rows list")  # noqa: TRY004 - malformed upstream response

    return payload


def save_to_s3(payload: dict, bucket: str, run_id: str | None = None) -> str:
    """Save the original API response as JSON in S3 Bronze.

    With a run_id the object name is that run's own (a retry rewrites it, another run can
    never collide with it); without one it is timestamped as before.
    """
    if not bucket:
        raise ValueError("An S3 bucket name is required")
    if run_id is not None and not RUN_ID_PATTERN.match(run_id):
        raise ValueError("run_id must be a UUID")

    now = datetime.now(UTC)
    name = run_id or f"{now:%Y%m%dT%H%M%SZ}"
    key = (
        f"bronze/eirgrid/demand_actual/"
        f"ingestion_date={now:%Y-%m-%d}/"
        f"demand_actual_{name}.json"
    )

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    return key


def main() -> None:
    """Run the ingestion pipeline."""
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise SystemExit("Set the S3_BUCKET environment variable before running.")

    days_back = int(os.environ.get("EIRGRID_DAYS_BACK", "2"))
    payload = fetch_demand_data(days_back=days_back)
    key = save_to_s3(payload, bucket)

    print(f"Fetched {len(payload['Rows'])} demand records from EirGrid.")
    print(f"Saved raw response to s3://{bucket}/{key}")


if __name__ == "__main__":
    main()
