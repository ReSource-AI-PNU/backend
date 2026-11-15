"""PP 근거 비교 API 계약과 순수 비교 결과를 확인한다."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app

URL = "/api/v1/c/evidence/compare"
client = TestClient(app)


def value(text):
    return {"status": "value", "value": text}


def test_unprocessed_pp_is_repeatable_and_preserves_references():
    body = {"state": {"material": value("PP"), "form": value("가공 전"), "region": value("부산")}}
    original = deepcopy(body)
    response = client.post(URL, json=body)
    assert response.status_code == 200
    result = response.json()
    assert result == client.post(URL, json=body).json()
    assert body == original
    assert result["status"] == "compared"
    assert result["inputSummary"]["materialBasis"]["status"] == "unanswered"
    assert all(
        c["form"]["status"] == "different" and c["region"]["status"] == "match"
        for c in result["comparisons"]
    )
    source = result["comparisons"][0]["evidence"]
    assert source["referenceMonth"] == "2026-09"
    assert source["criteria"]["pdfPage"] == 21 and source["criteria"]["printedPage"] == 17
    assert source["criteria"]["url"] and source["vat"] == "포함" and source["logistics"] == "제외"
    assert result["limitations"] and result["followUp"]


def test_matching_form_does_not_match_quality_or_trade():
    response = client.post(URL, json={"state": {"material": value("PP"), "form": value("펠렛")}})
    assert response.status_code == 200
    result = response.json()
    pellet = result["comparisons"][2]
    assert pellet["form"]["status"] == "match"
    assert pellet["quality"]["status"] == pellet["tradeStage"]["status"] == "unconfirmed"
    assert pellet["form"]["reason"] and pellet["evidence"]["priceTable"]["pdfPage"] == 44


def test_insufficient_information_preserves_status():
    response = client.post(
        URL, json={"state": {"material": {"status": "unknown"}, "form": {"status": "conflict"}}}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "insufficient_information"
    assert result["inputSummary"]["material"]["status"] == "unknown"
    assert result["inputSummary"]["form"]["status"] == "conflict"
    assert all(
        c["material"]["status"] == c["form"]["status"] == "unconfirmed"
        for c in result["comparisons"]
    )


def test_non_pp_is_normal_unsupported_result():
    response = client.post(URL, json={"state": {"material": value("PE")}})
    assert response.status_code == 200
    assert response.json()["status"] == "unsupported"
    assert response.json()["comparisons"] == []
    assert response.json()["inputSummary"]["material"]["value"] == "PE"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"state": {"form": value("분쇄만")}},
        {"state": {"material": {"status": "value"}}},
        {"state": {}, "extra": True},
    ],
)
def test_invalid_request_is_422(body):
    assert client.post(URL, json=body).status_code == 422
