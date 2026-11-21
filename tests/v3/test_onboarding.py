"""업체 온보딩 대화: 공공 명부로 허가 품목·차고지 초안, 담당자 말로 차량·요일 초안, 담당자 확인 뒤에만 저장."""

from __future__ import annotations

import pytest

from app.v3 import service
from app.v3.agents.llm import LLMError, ScriptedLLM
from app.v3.judge import permits as permit_rules
from app.v3.onboarding import agent as onboarding
from app.v3.onboarding import tools as T
from tests.v3.runtime_fixtures import HANBIT, make_app

NEW = "org_woori"
NAME = "㈜우리환경산업"  # 공공 명부에 같은 상호가 한 곳뿐인 부산 업체(pv_04140b9944)
REGISTRY_ID = "pv_04140b9944"


def _app(responses=None, *, model=True):
    llm = ScriptedLLM(list(responses or []), provider="ollama", model="qwen2.5:32b")

    def factory(role, org_id, picked):
        return llm if (model and role == "vendor" and org_id == NEW) else None

    app = make_app(llm_factory=factory)
    app.store.put_org({"id": NEW, "name": NAME, "role": "vendor", "demo": False})
    app.store.put_vendor_profile(
        {"orgId": NEW, "permits": [], "criteria": [], "fleet": {}, "guardrails": {}}
    )
    return app, llm


def _said(registry=None, fields=(), skipped=(), ask=None):
    """모델이 내는 JSON(EXTRACT_SCHEMA)."""
    return {
        "registry": registry
        or {"action": "none", "registryId": None, "query": None, "region": None},
        "fields": [{"key": k, "value": v, "quote": q} for k, v, q in fields],
        "skipped": list(skipped),
        "ask": ask,
    }


CONFIRM = {"action": "confirm", "registryId": REGISTRY_ID, "query": None, "region": None}


def test_greeting_finds_the_company_in_the_public_registry_without_a_model():
    app, llm = _app()
    state = onboarding.get_onboarding(app, NEW, NEW)
    assert [m["id"] for m in state["messages"]] == ["greeting"]
    assert "금정구" in state["messages"][0]["text"]
    assert state["discoverable"] is False
    assert {c["key"]: c["done"] for c in state["checks"]}["permits"] is False
    assert state["model"] == {"provider": "ollama", "name": "qwen2.5:32b"}
    assert llm.calls == []  # 인사는 모델을 부르지 않는다


def test_registry_and_spoken_values_become_a_draft_and_only_confirmed_keys_are_saved():
    app, llm = _app(
        [
            _said(CONFIRM),
            _said(
                fields=[
                    ("fleet.capacityKg", "5000", "5톤 트럭 2대"),
                    ("fleet.vehicleCount", "2", "5톤 트럭 2대"),
                    ("guardrails.days", "화, 목", "화요일, 목요일에 돌아요"),
                    ("fleet.maxPickupKm", "30", "5톤 트럭 2대"),  # 담당자가 말하지 않은 값
                ]
            ),
        ]
    )
    state = onboarding.post_message(app, NEW, {"text": "네 금정구 동천로 그곳이 맞아요"}, NEW)
    assert llm.calls[0]["response_schema"] is onboarding.EXTRACT_SCHEMA
    assert REGISTRY_ID in llm.calls[0]["messages"][-1]["content"]  # 후보 ID는 상태로 준다
    keys = [d["key"] for d in state["draft"]]
    assert keys == ["permits", "fleet.depot"]
    permits = state["draft"][0]
    assert permits["value"] == [
        {"wasteItem": "폐목재", "materials": []},
        {"wasteItem": "폐플라스틱", "materials": []},
    ]
    assert permits["source"]["kind"] == "registry"
    last = state["messages"][-1]
    assert last["tools"][0]["summary"].startswith("공공 허가 명부")
    assert "허가 품목(폐목재(모든 재질), 폐플라스틱(모든 재질))" in last["text"]
    assert "수거 차량은 몇 대인가요?" in last["text"]  # 다음 질문은 코드가 고른다

    state = onboarding.post_message(
        app, NEW, {"text": "5톤 트럭 2대 있고 수거는 화요일, 목요일에 돌아요"}, NEW
    )
    drafted = {d["key"]: d for d in state["draft"]}
    assert drafted["fleet.capacityKg"]["value"] == 5000
    assert drafted["fleet.capacityKg"]["source"] == {
        "kind": "conversation",
        "label": "담당자 답변",
        "quote": "5톤 트럭 2대",
    }
    assert drafted["guardrails.days"]["value"] == ["화", "목"]
    assert "fleet.maxPickupKm" not in drafted  # 인용 안에 30이 없어 거절
    last = state["messages"][-1]
    assert "뺀 것 1개" in last["tools"][0]["summary"]
    assert "최대 수거 거리는 말씀과 맞는지 확인하지 못해" in last["text"]

    # 저장 전에는 업체 정보가 비어 있다
    assert app.store.get_vendor_profile(NEW)["permits"] == []

    out = onboarding.post_confirm(
        app, NEW, {"keys": ["permits", "fleet.depot", "fleet.capacityKg", "guardrails.days"]}, NEW
    )
    profile = app.store.get_vendor_profile(NEW)
    assert {p["wasteItem"] for p in profile["permits"]} == {"폐목재", "폐플라스틱"}
    assert profile["fleet"]["capacityKg"] == 5000
    assert profile["fleet"]["depot"] == [129.12468, 35.229955]
    assert profile["guardrails"]["days"] == ["화", "목"]
    assert out["onboarding"]["discoverable"] is True
    assert [d["key"] for d in out["onboarding"]["draft"]] == ["fleet.vehicleCount"]
    # 상호와 차고지가 명부와 맞아 허가 확인이 공공 명부로 이어진다
    assert permit_rules.registry_records(NAME, profile["fleet"]["depot"])


def test_the_model_cannot_claim_what_it_did_not_do():
    app, _ = _app([_said(ask="허가 품목을 저장했습니다.")])
    state = onboarding.post_message(app, NEW, {"text": "안녕하세요"}, NEW)
    text = state["messages"][-1]["text"]
    assert "저장했" not in text and "초안에 넣었" not in text
    assert state["draft"] == []


def test_search_then_confirm_uses_the_new_candidates():
    search = {"action": "search", "registryId": None, "query": "우리환경", "region": "금정구"}
    app, llm = _app([_said(search), _said(CONFIRM)])
    state = onboarding.post_message(app, NEW, {"text": "상호는 우리환경이고 금정구예요"}, NEW)
    assert "금정구 동천로" in state["messages"][-1]["text"]
    state = onboarding.post_message(app, NEW, {"text": "첫 번째가 맞아요"}, NEW)
    assert REGISTRY_ID in llm.calls[1]["messages"][-1]["content"]
    assert [d["key"] for d in state["draft"]] == ["permits", "fleet.depot"]


def test_skipped_items_are_not_asked_again():
    app, _ = _app([_said(CONFIRM), _said(skipped=["fleet.vehicleCount", "fleet.capacityKg"])])
    onboarding.post_message(app, NEW, {"text": "맞아요"}, NEW)
    state = onboarding.post_message(app, NEW, {"text": "차량은 아직 몰라요"}, NEW)
    text = state["messages"][-1]["text"]
    assert "몇 대" not in text and "어느 지역까지" in text


def test_i_dont_know_is_read_by_code_when_the_model_misses_it():
    app, _ = _app([_said(CONFIRM), _said(), _said()])
    onboarding.post_message(app, NEW, {"text": "맞아요"}, NEW)  # 차량 수·적재 중량을 묻는다
    state = onboarding.post_message(app, NEW, {"text": "최대 거리는 잘 모르겠어요"}, NEW)
    assert "최대 수거 거리는 비워 두고" in state["messages"][-1]["text"]
    state = onboarding.post_message(app, NEW, {"text": "그건 잘 몰라요"}, NEW)
    text = state["messages"][-1]["text"]
    assert "차량 수, 차량 한 대 적재 중량은 비워 두고" in text
    assert "몇 대" not in text and "어느 지역까지" in text


def test_model_failure_keeps_the_message_and_draft():
    app, _ = _app([LLMError("down")])
    state = onboarding.post_message(app, NEW, {"text": "차량은 3대예요"}, NEW)
    roles = [m["role"] for m in state["messages"]]
    assert roles == ["assistant", "user", "assistant"]
    assert "답하지 못했습니다" in state["messages"][-1]["text"]


def test_non_json_answer_is_a_failure_not_a_reply():
    app, _ = _app(["네 넣었습니다"])
    state = onboarding.post_message(app, NEW, {"text": "차량은 3대예요"}, NEW)
    assert "답하지 못했습니다" in state["messages"][-1]["text"]


def test_without_a_model_it_says_so():
    app, _ = _app(model=False)
    state = onboarding.post_message(app, NEW, {"text": "차량은 3대예요"}, NEW)
    assert state["model"]["provider"] == "rule"
    assert "연결되어 있지 않아" in state["messages"][-1]["text"]


def test_quote_must_be_what_the_person_said():
    app, _ = _app()
    ctx = T.Context(app=app, org_id=NEW, draft={}, said=["차량은 3대예요"])
    out = T.propose_fields(
        ctx, {"fields": [{"key": "fleet.vehicleCount", "value": 4, "quote": "차량은 4대예요"}]}
    )
    assert out["written"] == [] and "말에 없습니다" in out["rejected"][0]["reason"]
    out = T.propose_fields(
        ctx, {"fields": [{"key": "fleet.vehicleCount", "value": 3, "quote": "차량은 3대"}]}
    )
    assert out["written"] == ["fleet.vehicleCount"]
    # 명부에서만 오는 값은 대화로 넣지 못한다
    out = T.propose_fields(
        ctx, {"fields": [{"key": "permits", "value": "폐플라스틱", "quote": "차량은 3대"}]}
    )
    assert out["written"] == []


def test_decline_conditions_may_be_shortened_but_not_invented():
    app, _ = _app()
    ctx = T.Context(app=app, org_id=NEW, draft={}, said=["음식물 섞인 건 안 받아요"])
    out = T.propose_fields(
        ctx,
        {
            "fields": [
                {
                    "key": "guardrails.autoDecline",
                    "value": ["음식물 섞인 것"],
                    "quote": "음식물 섞인 건 안 받아요",
                }
            ]
        },
    )
    assert out["written"] == ["guardrails.autoDecline"]
    out = T.propose_fields(
        ctx,
        {
            "fields": [
                {
                    "key": "guardrails.autoDecline",
                    "value": ["PVC 혼입"],
                    "quote": "음식물 섞인 건 안 받아요",
                }
            ]
        },
    )
    assert out["written"] == []


def test_registry_is_not_handled_again_once_permits_are_drafted():
    app, _ = _app([_said(CONFIRM), _said(CONFIRM, fields=[("fleet.vehicleCount", "2대", "2대")])])
    onboarding.post_message(app, NEW, {"text": "맞아요"}, NEW)
    state = onboarding.post_message(app, NEW, {"text": "차량 2대요"}, NEW)
    last = state["messages"][-1]
    assert [t["name"] for t in last["tools"]] == ["propose_fields"]
    assert "명부 줄을 찾지 못했습니다" not in last["text"]


def test_price_sign_must_match_the_words():
    app, _ = _app()
    ctx = T.Context(app=app, org_id=NEW, draft={}, said=["처리비는 kg당 80원 밑으로는 안 받아요"])
    quote = "처리비는 kg당 80원 밑으로는 안 받아요"
    out = T.propose_fields(
        ctx, {"fields": [{"key": "guardrails.ceilingPriceKrwPerKg", "value": 80, "quote": quote}]}
    )
    assert out["written"] == [] and "처리비" in out["rejected"][0]["reason"]
    out = T.propose_fields(
        ctx, {"fields": [{"key": "guardrails.ceilingPriceKrwPerKg", "value": -80, "quote": quote}]}
    )
    assert out["written"] == ["guardrails.ceilingPriceKrwPerKg"]
    assert ctx.draft["guardrails.ceilingPriceKrwPerKg"]["text"] == "kg당 −80원 (처리비 80원)"


def test_values_repeated_from_earlier_messages_are_dropped_quietly():
    app, _ = _app(
        [
            _said(fields=[("fleet.vehicleCount", "2", "차량 2대")]),
            _said(
                fields=[
                    ("fleet.vehicleCount", "2", "차량 2대"),
                    ("guardrails.days", "월", "월요일"),
                ]
            ),
        ]
    )
    onboarding.post_message(app, NEW, {"text": "차량 2대"}, NEW)
    state = onboarding.post_message(app, NEW, {"text": "월요일만 가요"}, NEW)
    text = state["messages"][-1]["text"]
    assert "초안에 넣었습니다: 수거 가능 요일 월." in text and "차량 수" not in text


def test_prices_follow_the_guardrail_rules():
    app, _ = _app()
    ctx = T.Context(app=app, org_id=NEW, draft={}, said=["최저 150원, 최고 100원"])
    out = T.propose_fields(
        ctx,
        {
            "fields": [
                {"key": "guardrails.floorPriceKrwPerKg", "value": 150, "quote": "최저 150원"},
                {"key": "guardrails.ceilingPriceKrwPerKg", "value": 100, "quote": "최고 100원"},
            ]
        },
    )
    assert out["written"] == ["guardrails.floorPriceKrwPerKg"]
    assert "최저 단가" in out["rejected"][0]["reason"]


def test_coerce_reads_model_text():
    assert onboarding._coerce("fleet.capacityKg", "5,000") == 5000
    assert onboarding._coerce("guardrails.floorPriceKrwPerKg", "−80") == -80
    assert onboarding._coerce("guardrails.days", "월, 수、금") == ["월", "수", "금"]
    assert onboarding._coerce("fleet.vehicleCount", "두 대") == "두 대"  # 도구가 거절한다
    assert onboarding._coerce("fleet.vehicleCount", "2대") == 2
    assert onboarding._coerce("fleet.capacityKg", "5톤") == 5000
    assert (
        onboarding._coerce("fleet.capacityKg", "5톤 2대") == "5톤 2대"
    )  # 숫자가 둘이면 읽지 않는다


def test_only_the_vendor_itself_can_use_it():
    app, _ = _app()
    with pytest.raises(service.ServiceError):
        onboarding.get_onboarding(app, NEW, HANBIT)
    with pytest.raises(service.ServiceError):
        onboarding.post_confirm(app, NEW, {"keys": []}, NEW)


def test_reset_clears_the_conversation():
    app, _ = _app([_said()])
    onboarding.post_message(app, NEW, {"text": "안녕하세요"}, NEW)
    state = onboarding.delete_onboarding(app, NEW, NEW)
    assert [m["id"] for m in state["messages"]] == ["greeting"]


def test_registry_codes_map_to_permit_items():
    permits, left = T.permits_from_codes(
        ["51-03-01", "51-03-03", "51-03-07", "51-29-02", "40-02-06"]
    )
    assert permits == [
        {"wasteItem": "금속 스크랩", "materials": []},
        {"wasteItem": "폐플라스틱", "materials": []},
        {"wasteItem": "포장재", "materials": []},
    ]
    assert left == ["40-02-06"]
