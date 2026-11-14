"""Exercise the stateless API against the shared synthetic demo fixture."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.api import feasibility
from app.api.feasibility import DEMO_REQUEST
from app.main import app


@pytest.fixture
def request_body():
    return deepcopy(DEMO_REQUEST)


@pytest.fixture
def client():
    return TestClient(app)


def test_preview_demo_calculates_and_keeps_nulls(client, request_body):
    response = client.post("/api/v1/feasibility/preview", json=request_body)
    assert response.status_code == 200
    result = response.json()
    assert (result["totalNow"], result["totalAfter"], result["difference"]) == (
        -387500, 595000, 982500
    )
    assert result["status"] == "conditional"
    assert [d["route"] for d in result["decisions"]] == ["reuse", "sell", "pyrolysis", "bundle"]
    assert result["decisions"][3]["status"] == "conditional"
    assert result["carbonNowT"] is None and result["carbonAfterT"] is None
    assert result["dataMode"] == "synthetic_demo" and result["assumptions"]
    assert all(d["carbonAfterT"] is None for d in result["decisions"])
    assert all(d["ladder"] and d["alternatives"] for d in result["decisions"])


def test_override_recalculates_and_preserves_ai_route(client, request_body):
    stream_id = request_body["streams"][0]["id"]
    request_body["overrides"] = {stream_id: "sell"}
    result = client.post("/api/v1/feasibility/preview", json=request_body).json()
    first = result["decisions"][0]
    assert (first["route"], first["aiRoute"], first["monthlyAfter"]) == ("sell", "reuse", 180000)
    assert result["totalAfter"] == 265000


def test_pending_and_fractional_amounts(client, request_body):
    request_body["streams"][0]["monthlyKg"] = 0
    request_body["streams"][1]["monthlyKgConfirmed"] = False
    request_body["streams"][2]["classify"]["material"] = "unknown"
    result = client.post("/api/v1/feasibility/preview", json=request_body).json()
    assert result["status"] == "needs_information" and result["totalAfter"] is None
    assert [d["reasonCode"] for d in result["decisions"][:3]] == [
        "zero_quantity", "unconfirmed_quantity", "unsupported_combination"
    ]
    assert result["decisions"][0]["monthlyAfter"] is None
    request_body["streams"] = [deepcopy(DEMO_REQUEST["streams"][0])]
    request_body["streams"][0]["monthlyKg"] = 0.125
    result = client.post("/api/v1/feasibility/preview", json=request_body).json()
    assert (result["totalNow"], result["totalAfter"]) == (-31.25, -29887.5)


@pytest.mark.parametrize("case", ["empty", "duplicate", "unknown_id", "bad_route", "missing", "extra"])
def test_bad_requests_return_422(client, request_body, case):
    if case == "empty":
        request_body["streams"] = []
    elif case == "duplicate":
        request_body["streams"].append(deepcopy(request_body["streams"][0]))
    elif case == "unknown_id":
        request_body["overrides"] = {"unknown": "sell"}
    elif case == "bad_route":
        request_body["overrides"] = {request_body["streams"][0]["id"]: "invalid"}
    elif case == "missing":
        del request_body["streams"][0]["classify"]
    else:
        request_body["config"] = {"sale": 0}
    assert client.post("/api/v1/feasibility/preview", json=request_body).status_code == 422


def test_unexpected_errors_are_not_422(monkeypatch, request_body):
    def fail(*args):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(feasibility, "evaluate_streams", fail)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/v1/feasibility/preview", json=request_body).status_code == 500


def test_openapi_demo_is_actual_fixture_and_existing_apis_work(client):
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/v1/feasibility/preview"]["post"]["requestBody"]
    example = body["content"]["application/json"]["examples"]["synthetic_factory"]["value"]
    session = client.get("/api/v1/contract/session-example").json()
    assert example["streams"] == session["streams"]
    assert client.post("/api/v1/feasibility/preview", json=example).status_code == 200
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert client.get("/api/v1/contract/collections-example").status_code == 200
