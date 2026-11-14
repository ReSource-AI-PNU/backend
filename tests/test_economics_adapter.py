import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents.economics_adapter import evaluate_streams
from app.demo.synthetic import CONFIG
from app.main import app
from app.schemas import Session


@pytest.fixture
def streams():
    path = Path(__file__).resolve().parent.parent / "app/examples/demo_session.json"
    return Session.model_validate_json(path.read_text(encoding="utf-8")).streams


def test_fixture_totals_and_action(streams):
    result = evaluate_streams(streams, CONFIG)
    assert result.status == "conditional"
    assert [(d.monthly_now, d.monthly_after) for d in result.decisions] == [
        (-150000, 510000), (-100000, 90000), (-62500, -20000), (-75000, 15000)
    ]
    assert (result.total_now, result.total_after, result.difference) == (-387500, 595000, 982500)
    reuse, sell, pyro, bundle = result.decisions
    assert (reuse.cash_sale_revenue, reuse.purchase_avoidance_net) == (0, 540000)
    assert (sell.cash_sale_revenue, sell.purchase_avoidance_net) == (120000, 0)
    assert (reuse.action.labor_hours, reuse.action.labor_cost) == (2, reuse.labor_cost)
    assert pyro.action is None
    assert bundle.status == "conditional" and bundle.transport_cost == 15000
    body = result.model_dump(mode="json", by_alias=True)
    assert body["carbonNowT"] is None
    assert all(d["carbonAfterT"] is None for d in body["decisions"])
    assert body["decisions"][0]["ladder"] == [
        {"label": "현재(혼합 소각)", "perKg": -250},
        {"label": "분리해 매각", "perKg": 300},
        {"label": "분쇄 재투입", "perKg": 900},
    ]
    assert [(a["route"], a["perKg"]) for a in body["decisions"][3]["alternatives"]] == [
        ("sell", -40), ("pyrolysis", -80), ("incinerate", -250)
    ]
    assert all(d["dataMode"] == "synthetic_demo" for d in body["decisions"])
    assert result.total_now == sum(d.monthly_now for d in result.decisions)


def test_missing_and_unconfirmed_are_held_without_losing_other_streams(streams):
    streams[0].monthly_kg = 0
    streams[1].monthly_kg_confirmed = False
    result = evaluate_streams(streams, CONFIG)
    assert result.status == "needs_information"
    assert (result.total_now, result.total_after, result.difference) == (None, None, None)
    assert [d.reason_code for d in result.decisions[:2]] == ["zero_quantity", "unconfirmed_quantity"]
    assert all(d.monthly_after is None for d in result.decisions[:2])
    assert result.decisions[2].monthly_after == -20000


def test_contaminant_only_missing_ratio_and_removed(streams):
    # Item detection has no ratio and must not prevent calculation.
    assert evaluate_streams([streams[0]], CONFIG).status == "calculated"
    contaminant = streams[1].classify.detections[1]
    contaminant.ratio = None
    result = evaluate_streams([streams[1]], CONFIG)
    assert (result.status, result.decisions[0].reason_code) == ("needs_information", "missing_information")
    contaminant.removed = True
    assert evaluate_streams([streams[1]], CONFIG).status == "calculated"
    contaminant.removed = None
    assert evaluate_streams([streams[1]], CONFIG).status == "needs_information"


def test_override_and_unsupported(streams):
    stream = streams[0]
    result = evaluate_streams([stream], CONFIG, {stream.id: "sell"})
    assert (result.decisions[0].route, result.decisions[0].ai_route) == ("sell", "reuse")
    stream.classify.material = "HDPE"
    result = evaluate_streams([stream], CONFIG)
    assert (result.status, result.decisions[0].reason_code) == ("needs_information", "unsupported_combination")
    assert result.decisions[0].after_per_kg is None


def test_invalid_configuration_is_not_treated_as_a_stream_input_error(streams):
    with pytest.raises(ValueError):
        evaluate_streams([streams[0]], replace(CONFIG, labor_per_hour=-1))


def test_fractional_values_and_finite_validation(streams):
    streams[0].monthly_kg = 0.125
    result = evaluate_streams([streams[0]], CONFIG)
    assert result.total_now == -31.25
    assert result.total_after == -29887.5
    raw = result.model_dump(mode="json", by_alias=True)
    assert raw["totalNow"] == -31.25
    assert result.model_validate(json.loads(json.dumps(raw))) == result
    for value in (float("inf"), float("-inf"), float("nan")):
        changed = dict(raw, totalNow=value)
        with pytest.raises(ValidationError):
            result.model_validate(changed)
        changed = json.loads(json.dumps(raw))
        changed["decisions"][0]["cashSaleRevenue"] = value
        with pytest.raises(ValidationError):
            result.model_validate(changed)


def test_calculated_empty_lists_are_distinct_from_uncomputed(streams):
    pending = streams[0].model_copy(update={"monthly_kg_confirmed": False})
    held = evaluate_streams([pending], CONFIG).decisions[0]
    assert held.ladder is None and held.alternatives is None
    result = evaluate_streams([streams[0]], CONFIG)
    assert result.decisions[0].ladder and result.decisions[0].alternatives
    result.decisions[0].ladder = []
    assert result.model_dump(mode="json", by_alias=True)["decisions"][0]["ladder"] == []


def test_contract_api_example_and_identification_compatibility(streams):
    response = TestClient(app).get("/api/v1/contract/session-example")
    assert response.status_code == 200
    body = response.json()
    assert body["streams"][0]["classify"]["material"] == "PP"
    assert body["feasibility"]["carbonNowT"] is None
    assert body["feasibility"]["decisions"][0]["ladder"][-1] == {
        "label": "분쇄 재투입", "perKg": 900
    }
    assert Session.model_validate(body).streams[0].classify == streams[0].classify
