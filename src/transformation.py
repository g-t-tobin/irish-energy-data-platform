"""Transform EirGrid Bronze JSON into Silver Parquet."""

import io
import json
import math
import os
from datetime import datetime, timezone

import boto3
import pandas as pd


REQUIRED_COLUMNS = {"EffectiveTime", "FieldName", "Region", "Value"}


def transform_demand_payload(payload: dict) -> pd.DataFrame:
    """Validate and clean an EirGrid demand response."""
    rows = payload.get("Rows")

    if not isinstance(rows, list):
        raise ValueError("Payload must contain a Rows list")

    if not rows:
        raise ValueError("Payload contains no demand records")

    df = pd.DataFrame(rows)

    missing_columns = REQUIRED_COLUMNS - set(df.columns)
    if missing_columns:
        raise ValueError(
            f"Missing required columns: {sorted(missing_columns)}"
        )

    df = df[
        ["EffectiveTime", "FieldName", "Region", "Value"]
    ].copy()

    # Invalid timestamps are rejected, not silently discarded.
    timestamps = pd.to_datetime(
        df.pop("EffectiveTime"),
        format="%d-%b-%Y %H:%M:%S",
        errors="raise",
    )

    if timestamps.isna().any():
        raise ValueError("Timestamps must not be null")

    # Non-numeric values raise an error. Genuine missing values are
    # counted and excluded rather than being replaced with zero.
    demand = pd.to_numeric(df.pop("Value"), errors="raise")
    missing_demand = int(demand.isna().sum())

    valid = demand.notna()
    df = df.loc[valid].copy()
    timestamps = timestamps.loc[valid]
    demand = demand.loc[valid]

    if df.empty:
        raise ValueError("No records with valid demand values remain")

    if not demand.map(math.isfinite).all():
        raise ValueError("Demand values must be finite")

    df["effective_time_local"] = timestamps
    df["demand_mw"] = demand.astype("float64")
    df["field_name"] = df.pop("FieldName").astype("string")
    df["region"] = df.pop("Region").astype("string")
    df["source_timezone"] = "Europe/Dublin"

    df = df[
        [
            "effective_time_local",
            "source_timezone",
            "field_name",
            "region",
            "demand_mw",
        ]
    ]

    df = df.sort_values("effective_time_local").reset_index(drop=True)
    df.attrs["excluded_missing_demand"] = missing_demand

    return df


def save_silver_to_s3(df: pd.DataFrame, bucket: str) -> str:
    """Write the transformed DataFrame to S3 as compressed Parquet."""
    if not bucket:
        raise ValueError("An S3 bucket name is required")

    now = datetime.now(timezone.utc)
    key = (
        "silver/eirgrid/demand_actual/"
        f"ingestion_date={now:%Y-%m-%d}/"
        f"demand_actual_{now:%Y%m%dT%H%M%SZ}.parquet"
    )

    buffer = io.BytesIO()
    df.to_parquet(
        buffer,
        index=False,
        engine="pyarrow",
        compression="snappy",
    )

    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=buffer.getvalue(),
        ContentType="application/vnd.apache.parquet",
    )

    return key


def main() -> None:
    """Read the latest Bronze object and create a Silver dataset."""
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise SystemExit("Set the S3_BUCKET environment variable.")

    s3 = boto3.client("s3")
    response = s3.list_objects_v2(
        Bucket=bucket,
        Prefix="bronze/eirgrid/demand_actual/",
    )

    objects = response.get("Contents", [])
    if not objects:
        raise SystemExit("No Bronze objects found.")

    latest = max(objects, key=lambda obj: obj["LastModified"])
    raw = s3.get_object(
        Bucket=bucket,
        Key=latest["Key"],
    )["Body"].read()

    payload = json.loads(raw)
    df = transform_demand_payload(payload)
    excluded = df.attrs["excluded_missing_demand"]
    key = save_silver_to_s3(df, bucket)

    print(f"Bronze object: {latest['Key']}")
    print(f"Silver records written: {len(df)}")
    print(f"Records excluded for missing demand: {excluded}")
    print(f"Saved Parquet to s3://{bucket}/{key}")
    print(df.head().to_string(index=False))


if __name__ == "__main__":
    main()
