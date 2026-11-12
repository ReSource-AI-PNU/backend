import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.contract import DemoCollections
from app.main import app
from app.schemas import Correction, Detection, Session, Stream

EXAMPLES = Path(__file__).resolve().parent.parent / "app" / "examples"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def strip_none(v):
    """계약에서 null과 키 없음은 같은 뜻이다. API 응답은 null을 빼고 내보낸다."""
    if isinstance(v, dict):
        return {k: strip_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [strip_none(x) for x in v]
    return v


def test_demo_session_round_trip() -> None:
    """프런트 시연 앱이 만든 세션을 그대로 읽고, 다시 내보내면 같은 JSON이 된다."""
    raw = load("demo_session.json")
    session = Session.model_validate(raw)
    assert session.model_dump(mode="json", by_alias=True, exclude_unset=True) == raw


def test_demo_collections_round_trip() -> None:
    raw = load("demo_collections.json")
    collections = DemoCollections.model_validate(raw)
    assert collections.model_dump(mode="json", by_alias=True, exclude_none=True) == strip_none(raw)


def test_demo_numbers_add_up() -> None:
    """설계안 v2 6.1절의 시연 숫자와, 흐름 합계와 공장 합계가 맞는지 확인한다."""
    s = Session.model_validate(load("demo_session.json"))
    f = s.feasibility
    assert f is not None
    assert (f.total_now, f.total_after) == (-387_500, 595_000)
    assert f.total_now == sum(d.monthly_now for d in f.decisions)
    assert f.total_after == sum(d.monthly_after for d in f.decisions)

    by_id = {r.id: r for r in s.cost_carbon.routes}
    assert by_id["A"].monthly_krw == f.total_after
    assert by_id["C"].monthly_krw == f.total_now
    assert by_id["A"].monthly_krw - by_id["C"].monthly_krw == 982_500

    bundle = s.matching.bundles[0]
    assert bundle.total_kg == sum(m.kg for m in bundle.group) == 1_230
    assert bundle.fill_rate == pytest.approx(bundle.total_kg / bundle.truck_kg, abs=0.01)
    assert sum(c.share_pct for c in bundle.cost_share) == pytest.approx(100, abs=0.2)


def test_unknown_field_is_rejected() -> None:
    raw = load("demo_session.json")["streams"][0]
    with pytest.raises(ValidationError):
        Stream.model_validate({**raw, "grade": "A"})


def test_box_must_be_ratio() -> None:
    with pytest.raises(ValidationError):
        Detection.model_validate(
            {
                "id": "d1",
                "label": "라벨",
                "kind": "contaminant",
                "confidence": 0.8,
                "box": [0.1, 0.2, 1.4, 0.3],
                "source": "ai",
            }
        )


def test_snake_case_and_camel_case_both_accepted() -> None:
    by_alias = Correction.model_validate(
        {
            "id": "c1",
            "at": "2026-11-10T00:00:00Z",
            "by": "김담당",
            "step": "classify",
            "streamId": "st_1",
            "field": "출처",
            "before": "PIR",
            "after": "PCR",
        }
    )
    by_name = Correction(
        id="c1",
        at="2026-11-10T00:00:00Z",
        by="김담당",
        step="classify",
        stream_id="st_1",
        field="출처",
        before="PIR",
        after="PCR",
    )
    assert by_alias == by_name
    assert by_name.model_dump(by_alias=True, exclude_none=True)["streamId"] == "st_1"


def test_openapi_exposes_contract_models() -> None:
    schemas = TestClient(app).get("/openapi.json").json()["components"]["schemas"]
    expected = {
        "Session",
        "Stream",
        "ClassifyResult",
        "Question",
        "FeasibilityResult",
        "StreamDecision",
        "RegulationResult",
        "MatchingResult",
        "BundleGroup",
        "CostCarbonResult",
        "Route",
        "ProposalResult",
        "DocumentDraft",
        "Outcome",
        "KnowledgeCard",
        "LabelTask",
    }
    assert expected <= set(schemas)
    assert "monthlyKgConfirmed" in schemas["Stream"]["properties"]


def test_session_example_endpoint() -> None:
    res = TestClient(app).get("/api/v1/contract/session-example")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "completed"
    assert body["streams"][0]["monthlyKgConfirmed"] is True
    assert body == load("demo_session.json")
    assert body["feasibility"]["carbonNowT"] is None
