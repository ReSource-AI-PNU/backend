"""저장 없는 업체 문의 초안 API 계약을 확인한다."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.api.c_vendor_inquiry import VendorInquiryRequest, draft
from app.main import app

URL = "/api/v1/c/vendor-inquiry/draft"
client = TestClient(app)


def test_pp_draft_and_input_unchanged():
    body = {"state": {
        "material": {"status": "value", "value": "PP"},
        "materialBasis": {"status": "value", "value": "원자재 표기"},
        "form": {"status": "value", "value": "압축"},
        "quantity": {"status": "value", "value": {
            "amount": 500, "unit": "kg", "period": "월", "basis": "estimated",
        }},
    }}
    before = deepcopy(body)
    response = client.post(URL, json=body)
    assert response.status_code == 200
    assert set(response.json()) == {"markdown"}
    text = response.json()["markdown"]
    assert text.startswith("# 자원 처리·매입 조건 문의 초안\n")
    assert "재질: PP" in text and "500.0 kg / 월 (기준: 추정)" in text
    assert "실제로 발송되지 않았습니다" in text
    assert body == before
    request = VendorInquiryRequest.model_validate(body)
    state_before = request.state.model_dump()
    assert draft(request).markdown == text
    assert request.state.model_dump() == state_before


def test_unknown_unanswered_and_conflict_remain_distinct():
    response = client.post(URL, json={"state": {
        "material": {"status": "unknown"},
        "region": {"status": "conflict", "value": "부산"},
    }})
    assert response.status_code == 200
    text = response.json()["markdown"]
    assert "재질: 모름" in text and "형태: 미응답" in text
    assert "지역: 충돌 (제공된 값: 부산)" in text
    assert "PP" not in text


def test_non_pp_gets_general_inquiry():
    response = client.post(URL, json={"state": {"material": {"status": "value", "value": "PC"}}})
    assert response.status_code == 200
    assert "재질: PC" in response.json()["markdown"]
    assert "PP" not in response.json()["markdown"]


@pytest.mark.parametrize("body", [{}, {"state": {"material": {"status": "value"}}},
                                  {"state": {}, "extra": True}])
def test_invalid_request_returns_422(body):
    assert client.post(URL, json=body).status_code == 422
