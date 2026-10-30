"""A small Amazon States Language interpreter for the subset the pipeline workflow uses.

It runs the synthesised definition against in-process doubles (real stage handlers, the DynamoDB
test double), so tests can check the exact data each state passes on. It is not a general ASL
implementation: unsupported features raise instead of being guessed.

Modelled: Task (Lambda invoke, DynamoDB updateItem and deleteItem), Succeed, Fail, Parameters,
ResultSelector, ResultPath, Retry (counted per retrier, backoff recorded, never slept) and Catch.
An unresolvable path is a States.Runtime error, which States.ALL does not catch (AWS docs).
Not modelled: wall-clock time, TimeoutSeconds (a test can raise TaskError("States.Timeout")),
the 256 KiB payload limit, redrive and any state type the pipeline definition does not use.
"""

import copy
import json
import re
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

ENTERED_TIME = "2026-10-30T12:00:00.000Z"


class TaskError(Exception):
    """An error raised by a task, with the name Retry and Catch match on."""

    def __init__(self, name: str, cause: str = "") -> None:
        super().__init__(f"{name}: {cause}")
        self.name, self.cause = name, cause


def lookup(path: str, data: Any, context: dict) -> Any:
    if path.startswith("$$."):
        node: Any = context
        parts = path[3:].split(".")
    elif path == "$":
        return data
    elif path.startswith("$."):
        node, parts = data, path[2:].split(".")
    else:
        raise ValueError(f"unsupported path {path!r}")
    try:
        for part in parts:
            node = node[part]
    except (KeyError, TypeError, IndexError) as exc:
        raise TaskError("States.Runtime", f"path {path!r} could not be resolved") from exc
    return node


def evaluate(expression: str, data: Any, context: dict) -> Any:
    format_call = re.fullmatch(r"States\.Format\('([^']*)', (\$[^)]*)\)", expression)
    if format_call:
        template, argument = format_call.groups()
        assert template.count("{}") == 1
        return template.replace("{}", str(lookup(argument, data, context)))
    return lookup(expression, data, context)


def resolve(parameters: Any, data: Any, context: dict) -> Any:
    if isinstance(parameters, dict):
        resolved = {}
        for key, value in parameters.items():
            if key.endswith(".$"):
                resolved[key[:-2]] = evaluate(value, data, context)
            else:
                resolved[key] = resolve(value, data, context)
        return resolved
    if isinstance(parameters, list):
        return [resolve(item, data, context) for item in parameters]
    return parameters


def with_result(state: dict, result_path: str | None, result: Any) -> dict:
    if result_path is None:
        return state
    if result_path == "$":
        return result
    assert result_path.startswith("$.") and "." not in result_path[2:], "only top-level ResultPath"
    return {**state, result_path[2:]: result}


class Execution:
    def __init__(
        self,
        definition: dict,
        *,
        invoke_lambda: Callable[[str, dict], Any],
        dynamodb: Any,
        resolve_function: Callable[[Any], str] = lambda name: name,
    ) -> None:
        self.definition = definition
        self.invoke_lambda = invoke_lambda
        self.dynamodb = dynamodb
        self.resolve_function = resolve_function
        self.visited: list[str] = []
        self.lambda_events: list[tuple[str, dict]] = []
        self.invocations: dict[str, int] = {}
        self.waits: dict[str, list[float]] = {}  # recorded retry delays in seconds, not slept

    def run(self, execution_input: dict) -> dict:
        state_data = copy.deepcopy(execution_input)
        name = self.definition["StartAt"]
        while True:
            self.visited.append(name)
            state = self.definition["States"][name]
            kind = state["Type"]
            if kind == "Succeed":
                return {"status": "SUCCEEDED", "output": state_data, "last_state": name}
            if kind == "Fail":
                return {
                    "status": "FAILED", "error": state["Error"], "cause": state["Cause"],
                    "output": state_data, "last_state": name,
                }
            assert kind == "Task", f"unsupported state type {kind}"
            try:
                state_data, name = self._task(name, state, state_data)
            except TaskError as error:  # no Catch: the execution itself fails with this error
                return {"status": "FAILED", "error": error.name, "cause": error.cause,
                        "output": state_data, "last_state": name}

    def _task(self, name: str, state: dict, data: dict) -> tuple[dict, str]:
        context = {"State": {"Name": name, "EnteredTime": ENTERED_TIME}}
        attempts: dict[int, int] = {}
        while True:
            try:
                result = self._call(name, state, data, context)
            except TaskError as error:
                retry = self._matching_retry(name, state, error, attempts)
                if retry:
                    continue
                catch = next(
                    (c for c in state.get("Catch", []) if self._matches(c["ErrorEquals"], error.name)), None
                )
                if catch is None:
                    raise
                failure = {"Error": error.name, "Cause": error.cause}
                return with_result(data, catch["ResultPath"], failure), catch["Next"]
            return with_result(data, state.get("ResultPath", "$"), result), state["Next"]

    @staticmethod
    def _matches(error_equals: list[str], name: str) -> bool:
        return ("States.ALL" in error_equals and name != "States.Runtime") or name in error_equals

    def _matching_retry(self, state_name: str, state: dict, error: TaskError, attempts: dict[int, int]) -> bool:
        for index, retry in enumerate(state.get("Retry", [])):
            if self._matches(retry["ErrorEquals"], error.name):
                attempts[index] = attempts.get(index, 0) + 1
                if attempts[index] > retry["MaxAttempts"]:
                    return False
                wait = retry["IntervalSeconds"] * retry["BackoffRate"] ** (attempts[index] - 1)
                self.waits.setdefault(state_name, []).append(wait)
                return True
        return False

    def _call(self, name: str, state: dict, data: dict, context: dict) -> Any:
        resource = state["Resource"]
        parameters = resolve(state["Parameters"], data, context)
        if resource == "arn:aws:states:::lambda:invoke":
            function = self.resolve_function(parameters["FunctionName"])
            payload = json.loads(json.dumps(parameters["Payload"]))
            self.invocations[name] = self.invocations.get(name, 0) + 1
            self.lambda_events.append((name, copy.deepcopy(payload)))
            try:
                output = self.invoke_lambda(function, payload)
            except TaskError:
                raise
            except Exception as exc:  # a function error is reported by its error type
                raise TaskError(type(exc).__name__, str(exc)) from exc
            envelope = {
                "Payload": json.loads(json.dumps(output)),
                "StatusCode": 200,
                "ExecutedVersion": "$LATEST",
                "SdkHttpMetadata": {"HttpStatusCode": 200},
            }
            return resolve(state["ResultSelector"], envelope, context)
        operations = {
            "arn:aws:states:::dynamodb:updateItem": self.dynamodb.update_item,
            "arn:aws:states:::dynamodb:deleteItem": self.dynamodb.delete_item,
        }
        try:
            operations[resource](**parameters)
        except ClientError as exc:
            raise TaskError("DynamoDb." + exc.response["Error"]["Code"], str(exc)) from exc
        return {}
