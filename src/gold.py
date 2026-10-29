"""Build daily electricity-demand analytics from Silver Parquet."""

import io
import os
from datetime import datetime, timezone

import boto3
import pandas as pd


def build_daily_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate valid Silver measurements into daily demand metrics."""
    required = {"effective_time_local", "region", "demand_mw"}
    missing = required - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    if df.empty:
        raise ValueError("Silver dataset is empty")

    data = df.copy()
    data["effective_time_local"] = pd.to_datetime(
        data["effective_time_local"], errors="raise"
    )
    data["demand_mw"] = pd.to_numeric(data["demand_mw"], errors="raise")

    if data["effective_time_local"].isna().any():
        raise ValueError("Timestamps must not be null")

    if data["demand_mw"].isna().any():
        raise ValueError("Demand values must not be null")

    if not data["demand_mw"].map(
        lambda value: pd.notna(value) and abs(value) != float("inf")
    ).all():
        raise ValueError("Demand values must be finite")

    data["date"] = data["effective_time_local"].dt.strftime("%Y-%m-%d")

    result = (
        data.groupby(["date", "region"], as_index=False)
        .agg(
            average_demand_mw=("demand_mw", "mean"),
            minimum_demand_mw=("demand_mw", "min"),
            maximum_demand_mw=("demand_mw", "max"),
            observations=("demand_mw", "count"),
        )
        .sort_values(["date", "region"])
        .reset_index(drop=True)
    )

    return result


def main() -> None:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise SystemExit("Set the S3_BUCKET environment variable.")

    s3 = boto3.client("s3")
    response = s3.list_objects_v2(
        Bucket=bucket,
        Prefix="silver/eirgrid/demand_actual/",
    )

    objects = [
        obj for obj in response.get("Contents", [])
        if obj["Key"].endswith(".parquet")
    ]
    if not objects:
        raise SystemExit("No Silver Parquet files found.")

    latest = max(objects, key=lambda obj: obj["LastModified"])
    body = s3.get_object(Bucket=bucket, Key=latest["Key"])["Body"].read()
    df = pd.read_parquet(io.BytesIO(body))

    metrics = build_daily_metrics(df)

    now = datetime.now(timezone.utc)
    key = (
        "gold/eirgrid/daily_demand/"
        f"run_date={now:%Y-%m-%d}/"
        f"daily_demand_{now:%Y%m%dT%H%M%SZ}.parquet"
    )

    output = io.BytesIO()
    metrics.to_parquet(
        output, index=False, engine="pyarrow", compression="snappy"
    )

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=output.getvalue(),
        ContentType="application/vnd.apache.parquet",
    )

    print(f"Source: s3://{bucket}/{latest['Key']}")
    print(f"Daily metric rows: {len(metrics)}")
    print(f"Saved: s3://{bucket}/{key}")
    print(metrics.to_string(index=False))


if __name__ == "__main__":
    main()
