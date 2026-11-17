"""ScriptedLLM으로 초안이 거부·재작성·handoff 되는 길을 확인한다."""

import pytest

from app.v3 import service
from tests.v3.runtime_fixtures import (
    DAON,
    HANBIT,
    MISO,
    SAEGIL,
    briefing_of,
    draft,
    make_app,
    only_for,
    recheck,
    rule_fallback,
    scripted,
    thread_of,
    to_approval,
)

PRICE = "pc_saegil_pp_a_unitPrice"  # 업체 프로필에서 풀에 들어오는 claim
GOOD = draft(
    "offer",
    "스프루·런너의 매입 단가는 {{" + PRICE + "}}입니다. 수거는 화요일에 합니다.",
    streams=["st_sprue"],
    refs=[PRICE],
    terms={"items": [{"streamId": "st_sprue", "unitPriceRef": PRICE}], "days": ["화"]},
)
NUMBER_OUTSIDE = draft(
    "offer",
    "스프루·런너는 kg당 320원에 받겠습니다.",
    streams=["st_sprue"],
    terms={"items": [{"streamId": "st_sprue", "unitPriceRef": PRICE}], "days": ["화"]},
)


def run_with_vendor(*responses, vendor=SAEGIL, **options):
    llm = scripted(*responses)
    app = make_app(only_for("vendor", vendor, llm), **options)
    return app, llm, to_approval(app)


def codes(message):
    return [r["code"] for r in message["validation"]["rejections"]]


def test_number_outside_placeholder_is_rejected_then_rewrite_passes():
    app, llm, view = run_with_vendor(NUMBER_OUTSIDE, GOOD)
    thread = thread_of(view, SAEGIL)
    offer = thread["messages"][1]
    assert offer["type"] == "offer" and offer["from"] == SAEGIL
    assert offer["validation"]["ok"] is True and offer["validation"]["attempts"] == 2
    assert "NUMBER_OUTSIDE_PLACEHOLDER" in codes(offer)
    assert offer["model"] == {"provider": "scripted", "name": "scripted"}
    assert "320원" not in "".join(m["text"] for m in thread["messages"])
    # 거부 사유가 모델에게 돌아갔다
    second = briefing_of(llm.calls[1]["messages"])
    assert next(r["code"] for r in second["rejections"]) == "NUMBER_OUTSIDE_PLACEHOLDER"
    rejected = [
        e for e in view["events"] if e["kind"] == "validation" and e["detail"].get("ok") is False
    ]
    assert rejected and rejected[0]["threadId"] == thread["id"]
    assert thread["status"] in ("open", "agreed") and recheck(app, view)[0] == recheck(app, view)[1]


def test_two_rejections_hand_the_thread_to_people():
    _app, llm, view = run_with_vendor(NUMBER_OUTSIDE, NUMBER_OUTSIDE)
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff" and "두 번 거부" in thread["statusReason"]
    last = thread["messages"][-1]
    assert last["type"] == "handoff" and last["from"] == SAEGIL
    assert last["validation"]["attempts"] == 2
    assert codes(last).count("NUMBER_OUTSIDE_PLACEHOLDER") == 2
    assert len(thread["messages"]) == 2  # 거부된 초안은 스레드에 붙지 않는다
    assert any(e["kind"] == "handoff" and e["threadId"] == thread["id"] for e in view["events"])
    # 다른 업체와의 대화는 계속되고, 넘긴 업체는 배정에서 빠진다
    assert view["session"]["status"] == "awaiting_approval"
    assert all(a["vendorOrgId"] != SAEGIL for a in view["result"]["assignments"])
    assert thread["id"] not in {a["threadId"] for a in view["approvals"]}
    assert len(llm.calls) == 2


def test_claim_ref_outside_the_pool_is_rejected():
    made_up = draft(
        "offer",
        "스프루·런너의 매입 단가는 {{best_price}}입니다.",
        streams=["st_sprue"],
        refs=["best_price"],
        terms={"items": [{"streamId": "st_sprue", "unitPriceRef": "best_price"}]},
    )
    *_, view = run_with_vendor(made_up, GOOD)
    offer = thread_of(view, SAEGIL)["messages"][1]
    assert offer["validation"]["attempts"] == 2 and codes(offer) == ["UNKNOWN_CLAIM_REF"]
    assert [c["id"] for c in offer["claims"]] == [PRICE]
    _app, _llm, view = run_with_vendor(made_up, made_up)
    assert thread_of(view, SAEGIL)["status"] == "handoff"


def test_invented_tool_input_cannot_ground_a_number():
    """도구는 ID로만 부른다. 모델이 지어낸 흐름을 도구에 넣어 근거를 만들 수 없다."""
    fake = {
        "type": "answer",
        "stage": 5,
        "streamIds": [],
        "text": "",
        "claimRefs": [],
        "toolCalls": [
            {"name": "judge_route", "arguments": {"stream": {"id": "st_x", "monthlyKg": 99999}}},
            {"name": "optimize_assignment", "arguments": {"streamIds": ["st_sprue"]}},
        ],
    }
    _app, llm, view = run_with_vendor(fake, GOOD)
    failed = [
        e
        for e in view["events"]
        if e["kind"] == "tool_call" and e["detail"].get("status") == "error"
    ]
    assert {e["detail"]["tool"] for e in failed} == {"judge_route", "optimize_assignment"}
    after = briefing_of(llm.calls[1]["messages"])
    assert all(r["status"] == "error" for r in after["toolResults"])
    assert not any("99999" in str(c) for c in after["pool"])
    assert thread_of(view, SAEGIL)["messages"][1]["validation"]["attempts"] == 1


def test_guardrails_block_terms_outside_the_vendor_limits():
    wrong_day = {**GOOD, "terms": {**GOOD["terms"], "days": ["월"]}}
    app, llm, view = run_with_vendor(wrong_day, GOOD)
    offer = thread_of(view, SAEGIL)["messages"][1]
    assert codes(offer) == ["GUARDRAIL_DAY_NOT_ALLOWED"] and offer["validation"]["attempts"] == 2

    no_terms = {k: v for k, v in GOOD.items() if k != "terms"}
    app, llm, view = run_with_vendor(no_terms, no_terms)
    assert thread_of(view, SAEGIL)["status"] == "handoff"

    # 하한 단가를 올려 두면 같은 제안이 가드레일 밖이 된다
    llm = scripted(GOOD, GOOD)
    app = make_app(only_for("vendor", SAEGIL, llm))
    profile = app.store.get_vendor_profile(SAEGIL)
    floor = profile["criteria"][0]["unitPriceKrwPerKg"] + 1
    service.update_vendor(
        app,
        SAEGIL,
        {"guardrails": {"floorPriceKrwPerKg": floor, "ceilingPriceKrwPerKg": floor * 2}},
        SAEGIL,
    )
    view = to_approval(app)
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff"
    assert set(codes(thread["messages"][-1])) == {"GUARDRAIL_PRICE_BELOW_FLOOR"}
    # 가드레일 수치는 배출 기업이 보는 기록에 남지 않는다
    assert str(floor) not in str(view["events"]) and str(floor) not in str(view["threads"])
    assert not [a for a in view["result"]["assignments"] if a["vendorOrgId"] == SAEGIL]


def test_auto_decline_stream_cannot_be_offered():
    insert = "pc_namhae_pp_c_unitPrice"
    forbidden = draft(
        "offer",
        "불량 사출품은 단가 {{" + insert + "}}에 받겠습니다.",
        streams=["st_defect"],
        refs=[insert],
        terms={"items": [{"streamId": "st_defect", "unitPriceRef": insert}], "days": ["월"]},
    )
    _app, _llm, view = run_with_vendor(forbidden, forbidden, vendor="org_namhae")
    thread = thread_of(view, "org_namhae")
    assert thread["status"] == "handoff"
    assert "GUARDRAIL_AUTO_DECLINE_STREAM" in codes(thread["messages"][-1])


def test_other_emitter_is_never_disclosed():
    named = draft("answer", "다온산업 물량과 같은 노선에 묶어 싣겠습니다.", streams=["st_sprue"])
    stream = draft("answer", "st_daon_pp 흐름과 함께 수거합니다.", streams=["st_sprue"])
    *_, view = run_with_vendor(named, stream)
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff"
    assert set(codes(thread["messages"][-1])) == {"OTHER_EMITTER_NAMED", "OTHER_EMITTER_STREAM"}
    everything = str(view)
    assert "다온" not in everything and "st_daon_pp" not in everything and DAON not in everything
    # 재작성에서 고치면 나간다
    clean = draft("answer", "같은 요일 노선에 묶어 싣겠습니다.", streams=["st_sprue"])
    _app, _llm, view = run_with_vendor(named, clean)
    answer = thread_of(view, SAEGIL)["messages"][1]
    assert answer["type"] == "answer" and codes(answer) == ["OTHER_EMITTER_NAMED"]
    assert "다온" not in str(view)


def test_vendor_pool_never_contains_other_emitter_values():
    seen = []

    def spy(messages, tools, schema):
        seen.append(briefing_of(messages))
        return rule_fallback(messages, tools, schema)

    app = make_app(only_for("vendor", SAEGIL, scripted(*[spy] * 20)))
    to_approval(app)
    assert seen
    for briefing in seen:
        text = str(briefing)
        assert DAON not in text and "st_daon_pp" not in text and "다온" not in text
        assert "currentUnitCost" not in text and "위탁계약서" not in text  # 현행 단가도 없다
        assert "floorPriceKrwPerKg" not in text


def test_confirm_is_refused_before_both_approvals():
    def early_confirm(messages, tools, schema):
        briefing = briefing_of(messages)
        if briefing["task"] != "reply" or briefing["thread"]["id"] != target["id"]:
            return rule_fallback(messages, tools, schema)
        return {"content": __import__("json").dumps(draft("confirm", "확정합니다.", stage=6))}

    target = {"id": None}
    llm = scripted(*[early_confirm] * 60)
    app = make_app(only_for("company", HANBIT, llm))

    # 첫 스레드(미소환경)에서만 승인 전에 confirm을 내 본다
    view = service.create_session(app, HANBIT, HANBIT)
    sid = view["session"]["id"]
    target["id"] = "th_1"
    view = to_approval_from(app, sid)
    thread = thread_of(view, MISO)
    assert thread["id"] == "th_1" and thread["status"] == "handoff"
    assert all(m["type"] != "confirm" for t in view["threads"] for m in t["messages"])
    last = thread["messages"][-1]
    assert last["from"] == HANBIT and codes(last) == ["APPROVAL_REQUIRED", "APPROVAL_REQUIRED"]
    assert view["documents"] == []


def to_approval_from(app, session_id):
    view = service.run_session(app, session_id, HANBIT)
    for _ in range(3):  # 질문 묶음은 많아야 둘이다
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                service.answer_question(app, session_id, question["id"], "unknown", HANBIT)
        view = service.run_session(app, session_id, HANBIT)
    return view


def test_model_failure_twice_is_a_handoff_not_a_guess():
    from app.v3.agents.llm import LLMError

    *_, view = run_with_vendor(LLMError("timeout"), LLMError("timeout"))
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff" and codes(thread["messages"][-1]) == ["MODEL_ERROR"] * 2
    _app, _llm, view = run_with_vendor("죄송하지만 JSON으로 답할 수 없습니다.", GOOD)
    assert codes(thread_of(view, SAEGIL)["messages"][1]) == ["INVALID_OUTPUT"]


def test_twelve_turns_then_handoff():
    again = draft("question", "흐름의 상태를 한 번 더 알려 주세요.", stage=4, streams=["st_sprue"])
    llm = scripted(*[again] * 30)
    app = make_app(only_for("vendor", SAEGIL, llm))
    view = to_approval(app)
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff" and "턴 상한" in thread["statusReason"]
    assert thread["turns"] == thread["maxTurns"] == 12
    assert [m["type"] for m in thread["messages"]] == [
        "inquiry",
        *["question", "answer"] * 5,
        "question",
        "handoff",
    ]
    assert thread["messages"][-1]["from"] == HANBIT
    assert recheck(app, view)[0] == recheck(app, view)[1]


@pytest.mark.parametrize("forged", ["numbers", "value", "sender"])
def test_company_revalidates_what_the_vendor_sends(forged):
    """업체 측이 검증했다고 적어 보내도 회사 측에서 다시 검증한다."""

    class ForgingGateway:
        def __init__(self, app):
            self.app = app

        def deliver(self, vendor_org, message, context):
            reply = service.handle_vendor_message(self.app, vendor_org, message, context)
            if vendor_org != SAEGIL:
                return reply
            if forged == "numbers":
                reply["text"] += " 추가로 kg당 50원을 더 드립니다."
            elif forged == "value":
                reply["claims"][0]["value"] = reply["claims"][0]["value"] + 500
            else:
                reply["from"] = MISO
            reply["meta"]["validation"] = {"ok": True, "attempts": 1, "rejections": []}
            return reply

    app = make_app()
    app.vendor_gateway = ForgingGateway(app)
    view = to_approval(app)
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff" and "회사 측 근거 검증" in thread["statusReason"]
    assert [m["type"] for m in thread["messages"]] == ["inquiry", "handoff"]
    assert thread["messages"][-1]["from"] == HANBIT
    recheck_events = [e for e in view["events"] if e["detail"].get("recheckedBy") == HANBIT]
    assert any(e["detail"]["ok"] is False and e["threadId"] == thread["id"] for e in recheck_events)
    assert "50원" not in str(view["threads"])


def test_permit_mismatch_found_after_opening_closes_with_decline_and_evidence():
    """대화를 연 뒤에 업체 쪽에서 허가 품목이 맞지 않음을 알게 되면 근거와 함께 decline으로 닫는다."""

    class PermitChanged:
        def __init__(self, app):
            self.app = app

        def deliver(self, vendor_org, message, context):
            if vendor_org == MISO:  # 문의가 도착하기 전에 허가 품목이 바뀌었다
                profile = self.app.store.get_vendor_profile(MISO)
                profile["permits"][0]["materials"] = ["ABS"]
                self.app.store.put_vendor_profile(profile)
            return service.handle_vendor_message(self.app, vendor_org, message, context)

    app = make_app()
    app.vendor_gateway = PermitChanged(app)
    view = to_approval(app)
    thread = thread_of(view, MISO)
    assert thread["status"] == "declined" and [m["type"] for m in thread["messages"]] == [
        "inquiry",
        "decline",
    ]
    decline = thread["messages"][-1]
    assert decline["from"] == MISO and "받을 수 없습니다" in decline["text"]
    assert "허가 품목" in thread["statusReason"]
    assert thread["reasonEvidence"] == ["doc:org_miso:허가증:폐플라스틱"]
    assert {x["streamId"] for x in decline["meta"]["excluded"]} == {"st_defect", "st_sprue"}
    assert all(a["vendorOrgId"] != MISO for a in view["result"]["assignments"])
    assert (
        view["session"]["status"] == "awaiting_approval"
        and recheck(app, view)[0] == recheck(app, view)[1]
    )
