"""Which EirGrid failures are retried by the workflow, and that the retry is bounded."""

import pytest
import requests

from infrastructure.run_control import spec
from src import run_history_schema as schema
from src.ingestion.eirgrid import (
    DEFAULT_TIMEOUT_SECONDS,
    TRANSIENT_HTTP_STATUSES,
    TransientEirGridError,
    fetch_demand_data,
)
from src.run_store import classify_error, safe_error_message
from tests.pipeline_harness import demand_payload, make_pipeline_fixture

pipeline = make_pipeline_fixture()

TRANSIENT = [
    requests.Timeout("read timed out"),
    requests.ConnectTimeout("connect timed out"),
    requests.ConnectionError("connection reset"),
    *sorted(TRANSIENT_HTTP_STATUSES),
]
PERMANENT = [400, 401, 403, 404, 410, 422, requests.exceptions.SSLError("bad certificate")]
BRONZE_WAITS = [10.0, 20.0, 40.0]


def label(step):
    return step if isinstance(step, int) else type(step).__name__


def test_the_transient_http_statuses_are_exactly_the_agreed_set():
    assert TRANSIENT_HTTP_STATUSES == {429, 500, 502, 503, 504}


@pytest.mark.parametrize("step", TRANSIENT, ids=label)
def test_transient_failures_are_classified_as_transient(pipeline, step):
    pipeline.eirgrid.script = [step]

    with pytest.raises(TransientEirGridError) as caught:
        fetch_demand_data(2)

    assert isinstance(caught.value.__cause__, requests.RequestException)  # the original error is kept
    assert classify_error(caught.value) == "UPSTREAM_UNAVAILABLE"


@pytest.mark.parametrize("step", [*TRANSIENT, *PERMANENT], ids=label)
def test_failure_text_for_any_source_error_can_be_recorded_without_crashing(pipeline, step):
    """safe_error_message used to raise AttributeError for requests errors (response is not a dict)."""
    pipeline.eirgrid.script = [step]

    with pytest.raises(requests.RequestException) as caught:
        fetch_demand_data(2)

    text = safe_error_message(caught.value)
    assert text and len(text) <= 300


@pytest.mark.parametrize("step", PERMANENT, ids=label)
def test_permanent_failures_are_not_classified_as_transient(pipeline, step):
    pipeline.eirgrid.script = [step]

    with pytest.raises(requests.RequestException) as caught:
        fetch_demand_data(2)

    assert not isinstance(caught.value, TransientEirGridError)


@pytest.mark.parametrize(
    "body",
    [["not", "an", "object"], {"Status": "Error", "ErrorMessage": "bad"}, {"Rows": "x"}, {"NoRows": []}],
)
def test_unsupported_response_structures_are_permanent_validation_errors(pipeline, body):
    pipeline.eirgrid.script = [body]

    with pytest.raises(ValueError) as caught:
        fetch_demand_data(2)

    assert not isinstance(caught.value, TransientEirGridError) and classify_error(caught.value) == "VALIDATION_FAILED"


@pytest.mark.parametrize("days_back", [0, -3])
def test_invalid_input_is_permanent_and_makes_no_request(pipeline, days_back):
    with pytest.raises(ValueError):
        fetch_demand_data(days_back)

    assert pipeline.eirgrid.requests == []


def test_the_workflow_retries_exactly_the_class_the_source_raises():
    assert TransientEirGridError.__name__ in spec.TRANSIENT_SOURCE_ERRORS
    assert spec.TRANSIENT_SOURCE_ERRORS == ["TransientEirGridError"]


def test_a_transient_failure_is_retried_with_exponential_backoff_then_succeeds(pipeline):
    pipeline.eirgrid.script = [requests.Timeout("slow"), 503, demand_payload()]
    run_id = pipeline.new_run()

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "SUCCEEDED"
    assert execution.invocations["Bronze"] == 3 and len(pipeline.eirgrid.requests) == 3
    assert execution.waits["Bronze"] == BRONZE_WAITS[:2]  # 10 s then 20 s
    assert len(pipeline.s3.object_keys("bronze/")) == 1 and pipeline.store.get_run(run_id)["status"] == "succeeded"


def test_retries_stop_after_three_and_the_run_is_failed_not_stuck(pipeline):
    pipeline.eirgrid.script = [503, 503, 429, 500, 200]  # the fifth request must never be made
    run_id = pipeline.new_run()

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "FAILED" and result["error"] == "PipelineFailed"
    assert execution.invocations["Bronze"] == 4 and len(pipeline.eirgrid.requests) == 4
    assert execution.waits["Bronze"] == BRONZE_WAITS
    assert result["output"]["error"]["Error"] == "TransientEirGridError"
    run = pipeline.store.get_run(run_id)
    assert (run["status"], run["stage"]) == ("failed", "bronze") and pipeline.lock() is None
    assert pipeline.s3.object_keys() == []


@pytest.mark.parametrize("step", [400, 401, 404, requests.exceptions.SSLError("bad certificate"), ["list"]], ids=label)
def test_permanent_failures_are_not_retried(pipeline, step):
    pipeline.eirgrid.script = [step, demand_payload()]
    run_id = pipeline.new_run()

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "FAILED"
    assert execution.invocations["Bronze"] == 1 and len(pipeline.eirgrid.requests) == 1
    assert "Bronze" in execution.visited and execution.waits == {}
    assert pipeline.store.get_run(run_id)["status"] == "failed"


def test_lambda_service_errors_and_source_errors_have_separate_budgets(pipeline):
    pipeline.service_error("bronze", times=2)
    pipeline.eirgrid.script = [503, 503, 503]
    run_id = pipeline.new_run()

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "SUCCEEDED" and execution.invocations["Bronze"] == 6
    assert execution.waits["Bronze"] == [5.0, 10.0, 10.0, 20.0, 40.0]


def test_only_ingestion_retries_source_errors(pipeline):
    pipeline.fail("silver", TransientEirGridError("not a source error here"))
    run_id = pipeline.new_run()

    result, execution = pipeline.execute(run_id)

    assert result["status"] == "FAILED" and execution.invocations["Silver"] == 1


def test_the_worst_case_ingestion_time_fits_inside_the_workflow_and_the_lease():
    attempts = 1 + 3
    ingestion = attempts * DEFAULT_TIMEOUT_SECONDS + sum(BRONZE_WAITS)
    other_stages = 300 + 300 + 60  # Silver, Gold, Glue function timeouts (energy_config.PIPELINE_STAGES)

    assert ingestion + other_stages < schema.STATE_MACHINE_TIMEOUT_SECONDS < schema.LOCK_LEASE_SECONDS
