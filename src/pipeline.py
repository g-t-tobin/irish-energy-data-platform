"""Orchestrate the EirGrid Bronze, Silver and Gold pipeline."""

import argparse
import copy
import os
import re
from datetime import datetime

import boto3

from src.gold import process_silver_object
from src.ingestion.eirgrid import fetch_demand_data, save_to_s3
from src.transformation import process_bronze_object

AWS_REGION = "eu-west-1"
GLUE_DATABASE = "irish_energy_analytics"
GLUE_TABLE = "daily_electricity_demand"
REQUIRED_DESCRIPTOR_FIELDS = (
    "Columns",
    "InputFormat",
    "OutputFormat",
    "SerdeInfo",
)
GOLD_KEY_PATTERN = re.compile(
    r"^gold/eirgrid/daily_demand/run_date=(\d{4}-\d{2}-\d{2})/[^/]+\.parquet$"
)


def register_gold_partition(bucket: str, gold_key: str) -> bool:
    """Register the Gold run_date partition; return False if it already exists."""
    match = GOLD_KEY_PATTERN.match(gold_key)
    if not match:
        raise ValueError(f"Unexpected Gold key: {gold_key}")
    run_date = match.group(1)
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid run_date in Gold key: {run_date}") from None

    glue = boto3.client("glue", region_name=AWS_REGION)
    table = glue.get_table(DatabaseName=GLUE_DATABASE, Name=GLUE_TABLE)["Table"]

    descriptor = copy.deepcopy(table.get("StorageDescriptor") or {})
    missing = [f for f in REQUIRED_DESCRIPTOR_FIELDS if not descriptor.get(f)]
    if missing:
        raise ValueError(
            f"Glue table storage descriptor is missing: {missing}"
        )
    descriptor["Location"] = (
        f"s3://{bucket}/gold/eirgrid/daily_demand/run_date={run_date}/"
    )

    try:
        glue.create_partition(
            DatabaseName=GLUE_DATABASE,
            TableName=GLUE_TABLE,
            PartitionInput={
                "Values": [run_date],
                "StorageDescriptor": descriptor,
            },
        )
    except glue.exceptions.AlreadyExistsException:
        return False
    return True


def run_pipeline() -> dict[str, str]:
    """Run Bronze, Silver and Gold, then register the Gold partition."""
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError("Set the S3_BUCKET environment variable.")

    days_back = int(os.environ.get("EIRGRID_DAYS_BACK", "2"))

    payload = fetch_demand_data(days_back=days_back)
    bronze_key = save_to_s3(payload, bucket)
    print(f"Bronze data saved: s3://{bucket}/{bronze_key}")

    silver_key = process_bronze_object(bucket, bronze_key)
    gold_key = process_silver_object(bucket, silver_key)

    created = register_gold_partition(bucket, gold_key)
    print(
        "Glue partition "
        + ("registered" if created else "already registered")
        + f" for {gold_key}"
    )

    return {
        "bronze_key": bronze_key,
        "silver_key": silver_key,
        "gold_key": gold_key,
    }


def register_existing_gold_partition(gold_key: str) -> bool:
    """Retry Glue registration for an existing Gold object; no pipeline stages run."""
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError("Set the S3_BUCKET environment variable.")

    created = register_gold_partition(bucket, gold_key)
    print(
        "Glue partition "
        + ("registered" if created else "already registered")
        + f" for {gold_key}"
    )
    return created


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m src.pipeline")
    commands = parser.add_subparsers(dest="command")
    register = commands.add_parser(
        "register-partition",
        help="Register the Glue partition for an existing Gold object",
    )
    register.add_argument("--gold-key", required=True)
    args = parser.parse_args(argv)

    if args.command == "register-partition":
        register_existing_gold_partition(args.gold_key)
        return

    result = run_pipeline()
    print(f"Pipeline completed: {result}")


if __name__ == "__main__":
    main()
