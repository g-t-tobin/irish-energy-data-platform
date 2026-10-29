
from aws_cdk import Stack
from aws_cdk import aws_s3 as s3
from constructs import Construct


class DataLakeStack(Stack):
    """Reference the existing private, versioned S3 data lake."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.data_lake_bucket = s3.Bucket.from_bucket_name(
            self,
            "ExistingDataLakeBucket",
            bucket_name,
        )
