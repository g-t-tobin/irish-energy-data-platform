"""CDK resources for the Irish Energy Analytics data platform."""

from aws_cdk import Stack
from aws_cdk import aws_glue as glue
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_athena as athena
from constructs import Construct


class DataLakeStack(Stack):
    """Reference the existing data lake and configure Glue and Athena."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket.from_bucket_name(
            self,
            "ExistingDataLakeBucket",
            bucket_name,
        )

        database_name = "irish_energy_analytics"
        table_name = "daily_electricity_demand"

        database = glue.CfnDatabase(
            self,
            "EnergyAnalyticsDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=database_name,
                description="Irish electricity demand analytics",
            ),
        )

        table = glue.CfnTable(
            self,
            "DailyElectricityDemandTable",
            catalog_id=self.account,
            database_name=database_name,
            table_input=glue.CfnTable.TableInputProperty(
                name=table_name,
                description="Daily demand metrics derived from EirGrid data",
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "parquet",
                    "EXTERNAL": "TRUE",
                    "parquet.compression": "SNAPPY",
                },
                partition_keys=[
                    glue.CfnTable.ColumnProperty(
                        name="run_date",
                        type="string",
                    )
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=(
                        f"s3://{bucket_name}/gold/eirgrid/daily_demand/"
                    ),
                    input_format=(
                        "org.apache.hadoop.hive.ql.io.parquet."
                        "MapredParquetInputFormat"
                    ),
                    output_format=(
                        "org.apache.hadoop.hive.ql.io.parquet."
                        "MapredParquetOutputFormat"
                    ),
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library=(
                            "org.apache.hadoop.hive.ql.io.parquet.serde."
                            "ParquetHiveSerDe"
                        ),
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(
                            name="date", type="string"
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="region", type="string"
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="average_demand_mw", type="double"
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="minimum_demand_mw", type="double"
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="maximum_demand_mw", type="double"
                        ),
                        glue.CfnTable.ColumnProperty(
                            name="observations", type="bigint"
                        ),
                    ],
                ),
            ),
        )
        table.add_dependency(database)

        athena.CfnWorkGroup(
            self,
            "EnergyAnalyticsWorkGroup",
            name="irish-energy-analytics",
            description="Cost-controlled queries for Irish energy analytics",
            state="ENABLED",
            work_group_configuration=(
                athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                    enforce_work_group_configuration=True,
                    publish_cloud_watch_metrics_enabled=True,
                    bytes_scanned_cutoff_per_query=10 * 1024 * 1024,
                    result_configuration=(
                        athena.CfnWorkGroup.ResultConfigurationProperty(
                            output_location=(
                                f"s3://{bucket_name}/athena-results/"
                            ),
                        )
                    ),
                )
            ),
        )
