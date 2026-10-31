"""Design-only specification for run history and Step Functions (plan Phase 6).

Pure data: nothing here imports AWS SDKs, is wired into app.py, or can create a
resource. Phase 7 turns these into CDK constructs after an administrator has
confirmed the permissions. Account, region, bucket and function identifiers are
always passed in; none is hardcoded.
"""

from typing import Any

from src import run_history_schema as schema

GOLD_PREFIX = "gold/eirgrid/daily_demand/"
STAGES = ("bronze", "silver", "gold", "glue_registration")
# Lambda service and invocation failures only. A function's own errors (StageInputError,
# Sandbox.Timedout, ...) are permanent for this run and go straight to Catch.
LAMBDA_TRANSIENT_ERRORS = [
    "Lambda.ClientExecutionTimeoutException",
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
]
# Raised by src/ingestion/eirgrid.py (timeouts, connection errors, HTTP 429/500/502/503/504); only
# the ingestion stage can raise it. Retried 3 times at 10 s, 20 s and 40 s, then handled as a failure.
TRANSIENT_SOURCE_ERRORS = ["TransientEirGridError"]
PROGRESS_CONDITION = "attribute_exists(pk) AND #st IN (:q, :r)"
TERMINAL_CONDITION = "attribute_exists(pk) AND #st IN (:q, :r, :i)"
_STATUS_VALUES = {
    ":q": {"S": "queued"},
    ":r": {"S": "running"},
    ":i": {"S": "interrupted"},
}


def table_definition(table_name: str) -> dict[str, Any]:
    return {
        "TableName": table_name,
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [
            {"AttributeName": schema.PARTITION_KEY, "KeyType": "HASH"},
            {"AttributeName": schema.SORT_KEY, "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": schema.PARTITION_KEY, "AttributeType": "S"},
            {"AttributeName": schema.SORT_KEY, "AttributeType": "S"},
            {"AttributeName": schema.LIST_INDEX_PARTITION_KEY, "AttributeType": "S"},
            {"AttributeName": schema.LIST_INDEX_SORT_KEY, "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": schema.LIST_INDEX,
                "KeySchema": [
                    {
                        "AttributeName": schema.LIST_INDEX_PARTITION_KEY,
                        "KeyType": "HASH",
                    },
                    {"AttributeName": schema.LIST_INDEX_SORT_KEY, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        "TimeToLiveSpecification": {
            "AttributeName": schema.TTL_ATTRIBUTE,
            "Enabled": True,
        },
        "PointInTimeRecoveryEnabled": True,
        "DeletionProtectionEnabled": True,
        "RemovalPolicy": "RETAIN",
        "SSESpecification": {"Enabled": True},
    }


def table_arn(region: str, account: str, table_name: str) -> str:
    return f"arn:aws:dynamodb:{region}:{account}:table/{table_name}"


def execution_arn_prefix(state_machine_arn: str) -> str:
    return state_machine_arn.replace(":stateMachine:", ":execution:") + ":*"


def api_task_policy(
    *,
    bucket: str,
    table: str,
    state_machine_arn: str,
    pipeline_execution_enabled: bool = False,
) -> dict[str, Any]:
    """Runtime role of the API; pipeline execution is disabled unless explicitly enabled."""
    statements = [
        {
            "Sid": "ListGoldPrefixOnly",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [f"arn:aws:s3:::{bucket}"],
            "Condition": {"StringLike": {"s3:prefix": [GOLD_PREFIX + "*"]}},
        },
        {
            "Sid": "ReadGoldObjectsOnly",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket}/{GOLD_PREFIX}*"],
        },
        {
            "Sid": "RunHistoryTable",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
            ],
            "Resource": [table, f"{table}/index/{schema.LIST_INDEX}"],
        },
        {
            "Sid": "ReconcileOwnExecutions",
            "Effect": "Allow",
            "Action": ["states:DescribeExecution"],
            "Resource": [execution_arn_prefix(state_machine_arn)],
        },
    ]
    if pipeline_execution_enabled:
        statements.append(
            {
                "Sid": "StartAndInspectOneStateMachine",
                "Effect": "Allow",
                "Action": ["states:StartExecution"],
                "Resource": [state_machine_arn],
            }
        )
    return {
        "Version": "2012-10-17",
        "Statement": statements,
    }


def state_machine_role_policy(
    *, table: str, stage_function_arns: list[str]
) -> dict[str, Any]:
    """The state machine records progress itself and invokes only the listed stage functions."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RecordProgress",
                "Effect": "Allow",
                "Action": ["dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                "Resource": [table],
            },
            {
                "Sid": "InvokeStageFunctions",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": list(stage_function_arns),
            },
        ],
    }


def pipeline_stage_role_policy(
    *, bucket: str, region: str, account: str, glue_database: str, glue_table: str
) -> dict[str, Any]:
    """Role of the stage functions: the lake prefixes and one Glue table, nothing else."""
    catalog = f"arn:aws:glue:{region}:{account}:catalog"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadBronzeAndSilver",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/bronze/*",
                    f"arn:aws:s3:::{bucket}/silver/*",
                ],
            },
            {
                "Sid": "WriteLakeLayers",
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/bronze/*",
                    f"arn:aws:s3:::{bucket}/silver/*",
                    f"arn:aws:s3:::{bucket}/gold/*",
                ],
            },
            {
                "Sid": "ReadGoldForRegistration",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/{GOLD_PREFIX}*"],
            },
            {
                "Sid": "RegisterGoldPartitions",
                "Effect": "Allow",
                "Action": ["glue:GetTable", "glue:CreatePartition"],
                "Resource": [
                    catalog,
                    f"arn:aws:glue:{region}:{account}:database/{glue_database}",
                    f"arn:aws:glue:{region}:{account}:table/{glue_database}/{glue_table}",
                ],
            },
        ],
    }


def _run_key() -> dict[str, Any]:
    return {
        "pk": {"S.$": "States.Format('RUN#{}', $.run_id)"},
        "sk": {"S": schema.RUN_SK},
    }


def _update(
    table: str,
    sets: dict[str, dict[str, Any]],
    condition: str,
    next_state: str,
    on_error: str | None = None,
) -> dict[str, Any]:
    names = {f"#f{i}": name for i, name in enumerate(sets)}
    values: dict[str, Any] = {}
    expression = []
    for i, (name, value) in enumerate(sets.items()):
        expression.append(f"#f{i} = :v{i}")
        values[f":v{i}"] = value
    state_values = {k: v for k, v in _STATUS_VALUES.items() if k in condition}
    state: dict[str, Any] = {
        "Type": "Task",
        "Resource": "arn:aws:states:::dynamodb:updateItem",
        "Parameters": {
            "TableName": table,
            "Key": _run_key(),
            "UpdateExpression": "SET " + ", ".join(expression),
            "ConditionExpression": condition,
            "ExpressionAttributeNames": {**names, "#st": "status"},
            "ExpressionAttributeValues": {**values, **state_values},
        },
        "ResultPath": None,
        "Next": next_state,
    }
    if on_error:
        state["Catch"] = [
            {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": on_error}
        ]
    return state


def _release_lock(table: str, next_state: str) -> dict[str, Any]:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::dynamodb:deleteItem",
        "Parameters": {
            "TableName": table,
            "Key": {
                "pk": {"S": schema.LOCK_PK},
                "sk": {"S": schema.LOCK_SK},
            },
            "ConditionExpression": "run_id = :rid",
            "ExpressionAttributeValues": {":rid": {"S.$": "$.run_id"}},
        },
        "ResultPath": None,
        # A missed release is harmless: the lease expires on its own.
        "Catch": [
            {"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": next_state}
        ],
        "Next": next_state,
    }


def build_state_machine_definition(
    *, table: str, stage_function_arns: dict[str, str]
) -> dict[str, Any]:
    """Amazon States Language for one run. Input: {"run_id": ..., "days_back": ...}."""
    missing = set(STAGES) - set(stage_function_arns)
    if missing:
        raise ValueError(f"missing stage functions: {sorted(missing)}")

    now = {"S.$": "$$.State.EnteredTime"}
    states: dict[str, Any] = {
        "MarkRunning": _update(
            table,
            {
                "status": {"S": "running"},
                "stage": {"S": "bronze"},
                "started_at": now,
                "updated_at": now,
            },
            PROGRESS_CONDITION,
            "Bronze",
            "MarkFailure",
        )
    }
    keys = {"bronze": "bronze_key", "silver": "silver_key", "gold": "gold_key"}
    following = {"bronze": "silver", "silver": "gold", "gold": "glue_registration"}

    for stage in STAGES:
        task_next = f"Record_{stage}" if stage in keys else "MarkSucceeded"
        failure = (
            "MarkPartialFailure" if stage == "glue_registration" else "MarkFailure"
        )
        states[
            stage.capitalize() if stage != "glue_registration" else "GlueRegistration"
        ] = {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {
                "FunctionName": stage_function_arns[stage],
                "Payload.$": "$",
            },
            "ResultSelector": {"output.$": "$.Payload"},
            "ResultPath": f"$.{stage}",
            "TimeoutSeconds": 600,
            "Retry": [
                {
                    "ErrorEquals": list(LAMBDA_TRANSIENT_ERRORS),
                    "IntervalSeconds": 5,
                    "MaxAttempts": 2,
                    "BackoffRate": 2.0,
                },
                *(
                    [
                        {
                            "ErrorEquals": list(TRANSIENT_SOURCE_ERRORS),
                            "IntervalSeconds": 10,
                            "MaxAttempts": 3,
                            "BackoffRate": 2.0,
                        }
                    ]
                    if stage == "bronze"
                    else []
                ),
            ],
            "Catch": [
                {
                    "ErrorEquals": ["States.ALL"],
                    "ResultPath": "$.error",
                    "Next": failure,
                }
            ],
            "Next": task_next,
        }
        if stage in keys:
            nxt = following[stage]
            states[f"Record_{stage}"] = _update(
                table,
                {
                    keys[stage]: {"S.$": f"$.{stage}.output.key"},
                    "stage": {"S": nxt},
                    "updated_at": now,
                },
                PROGRESS_CONDITION,
                "GlueRegistration" if nxt == "glue_registration" else nxt.capitalize(),
                # Once Gold exists, a failed bookkeeping step is a partial failure.
                "MarkPartialFailure" if stage == "gold" else "MarkFailure",
            )

    states["MarkSucceeded"] = _update(
        table,
        {
            "status": {"S": "succeeded"},
            "stage": {"S": "completed"},
            "finished_at": now,
            "updated_at": now,
        },
        TERMINAL_CONDITION,
        "ReleaseLockOnSuccess",
    )
    states["ReleaseLockOnSuccess"] = _release_lock(table, "Succeeded")
    for name, status, release in (
        ("MarkFailure", "failed", "ReleaseLockOnFailure"),
        ("MarkPartialFailure", "partial_failure", "ReleaseLockOnFailure"),
    ):
        states[name] = _update(
            table,
            {
                "status": {"S": status},
                "error": {"S": "A pipeline stage failed."},
                "error_code": {"S": "INTERNAL_ERROR"},
                "finished_at": now,
                "updated_at": now,
            },
            TERMINAL_CONDITION,
            release,
        )
    states["ReleaseLockOnFailure"] = _release_lock(table, "Failed")
    states["Succeeded"] = {"Type": "Succeed"}
    states["Failed"] = {
        "Type": "Fail",
        "Error": "PipelineFailed",
        "Cause": "See the run record.",
    }

    return {
        "Comment": "Bronze, Silver, Gold and Glue registration for one run.",
        "StartAt": "MarkRunning",
        "TimeoutSeconds": schema.STATE_MACHINE_TIMEOUT_SECONDS,
        "States": states,
    }
