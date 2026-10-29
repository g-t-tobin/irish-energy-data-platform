import json
from unittest.mock import Mock, patch

import pytest
import requests

from src.ingestion.eirgrid import fetch_demand_data, save_to_s3


def test_fetch_demand_data_returns_rows():
    payload = {"Rows": [{"Value": 4200.0}]}
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None

    with patch("src.ingestion.eirgrid.requests.get", return_value=response) as get:
        result = fetch_demand_data(days_back=1)

    assert result == payload
    assert get.call_args.kwargs["params"]["area"] == "demandActual"
    assert get.call_args.kwargs["params"]["region"] == "ROI"
    assert get.call_args.kwargs["timeout"] == 30


def test_fetch_rejects_invalid_days_back():
    with pytest.raises(ValueError, match="days_back"):
        fetch_demand_data(days_back=0)


def test_fetch_propagates_http_error():
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("Server error")

    with patch("src.ingestion.eirgrid.requests.get", return_value=response):
        with pytest.raises(requests.HTTPError):
            fetch_demand_data()


def test_fetch_rejects_unexpected_response():
    response = Mock()
    response.json.return_value = {"unexpected": []}
    response.raise_for_status.return_value = None

    with patch("src.ingestion.eirgrid.requests.get", return_value=response):
        with pytest.raises(ValueError, match="Unexpected EirGrid response"):
            fetch_demand_data()


def test_save_to_s3_uploads_json():
    payload = {"Rows": [{"Value": 4200.0}]}
    s3_client = Mock()

    with patch("src.ingestion.eirgrid.boto3.client", return_value=s3_client):
        key = save_to_s3(payload, "test-bucket")

    kwargs = s3_client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "test-bucket"
    assert key.startswith("bronze/eirgrid/demand_actual/ingestion_date=")
    assert key.endswith(".json")
    assert kwargs["ContentType"] == "application/json"
    assert json.loads(kwargs["Body"].decode("utf-8")) == payload


def test_save_to_s3_requires_bucket():
    with pytest.raises(ValueError, match="bucket"):
        save_to_s3({"Rows": []}, "")


def test_fetch_rejects_api_error_even_with_http_200():
    response = Mock()
    response.json.return_value = {
        "Status": "Error",
        "ErrorMessage": "Invalid Date From",
        "Rows": [],
    }
    response.raise_for_status.return_value = None

    with patch("src.ingestion.eirgrid.requests.get", return_value=response):
        with pytest.raises(ValueError, match="EirGrid API error"):
            fetch_demand_data()
