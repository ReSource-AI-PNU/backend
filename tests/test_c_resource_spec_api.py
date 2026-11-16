"""저장 없는 자원 명세 API 계약을 확인한다."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.api.c_resource_spec import ResourceSpecRequest, draft
from app.main import app

URL = "/api/v1/c/resource-spec/draft"
client = TestClient(app)


def test_pp_draft_uses_camel_case_state_and_preserves_input():
    body = {"state": {
        "material": {"status": "value", "value": "PP"},
        "materialBasis": {"status": "value", "value": "원자재 표기"},
        "form": {"status": "value", "value": "압축"},
        "quantity": {"status": "value", "value": {
            "amount": 500, "unit": "kg", "period": "월", "basis": "estimated"
        }},
    }}
    original = deepcopy(body)
    response = client.post(URL, json=body)
    assert response.status_code == 200
    assert set(response.json()) == {"markdown"}
    markdown = response.json()["markdown"]
    assert markdown.startswith("# 자원 명세 초안\n")
    assert "재질 판단 근거: 원자재 표기" in markdown
    assert "500.0 kg / 월 (기준: 추정)" in markdown
    assert "한국환경공단" in markdown and "PDF 21쪽 / 인쇄 17쪽" in markdown
    assert body == original
    request = ResourceSpecRequest.model_validate(body)
    before = request.state.model_dump()
    assert draft(request).markdown == markdown
    assert request.state.model_dump() == before


def test_unknown_and_conflict_are_preserved():
    response = client.post(URL, json={"state": {
        "material": {"status": "unknown"},
        "region": {"status": "conflict", "value": "부산"},
    }})
    assert response.status_code == 200
    text = response.json()["markdown"]
    assert "재질: 모름" in text
    assert "지역: 충돌 (제공된 값: 부산)" in text
    assert "형태: 미응답" in text
    assert "한국환경공단" not in text


def test_pc_returns_partial_draft_without_pp_evidence():
    response = client.post(URL, json={"state": {"material": {"status": "value", "value": "PC"}}})
    assert response.status_code == 200
    text = response.json()["markdown"]
    assert "재질: PC" in text and "PP 공식 자료를 제시하지 않습니다" in text
    assert "한국환경공단" not in text and "[원문]" not in text


@pytest.mark.parametrize("body", [{}, {"state": {"material": {"status": "value"}}},
                                  {"state": {}, "extra": True}])
def test_invalid_request_returns_422(body):
    assert client.post(URL, json=body).status_code == 422
