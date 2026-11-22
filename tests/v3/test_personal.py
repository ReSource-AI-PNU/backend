"""회사별 보정(개인화, 2026-11-22): 지난 검수로 업체별 예상 단가를 맞추고, 담당자가 승인했을 때만 판정에 쓴다.

- 계층형 축소 추정(judge/personal.py): 같은 업체 기록 → 이 회사의 이 폐기물 기록 → 계약대로(0) 순서로 당긴다.
- 보정 강도는 코드 상수가 아니라 담당자 승인 근거(human)의 값이다. 승인이 없거나 가짜면 보정하지 않는다.
- 회사 측 AI는 같은 업체와의 지난 검수 요약(숫자 없음)을 받고, 업체는 보정값을 보지 못한다.
"""

import uuid

import pytest

from app.v3 import memory, service
from app.v3.judge import personal
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    SAEGIL,
    approve_all,
    make_app,
    thread_of,
    to_approval,
)


def _record(app, view, grade, price):
    body = {
        "requestId": f"req-{uuid.uuid4().hex}",
        "threadId": thread_of(view, SAEGIL, "confirmed")["id"],
        "streamId": "st_sprue",
        "actualGrade": grade,
        "actualUnitPriceKrwPerKg": price,
        "actualKg": 500,
        "inspectedOn": "2026-11-16",
    }
    service.record_outcome(app, view["session"]["id"], body, SAEGIL)


def _sprue(view):
    return next(a for a in view["result"]["assignments"] if a["streamId"] == "st_sprue")


@pytest.fixture
def below():
    """한빛·새길 확정 뒤 스프루 검수 셋이 합의(A, kg당 320원)보다 낮게(kg당 300원) 나온 앱."""
    app = make_app()
    view = approve_all(app, to_approval(app))
    for grade in ("B", "C", "C"):
        _record(app, view, grade, 300)
    return app


# ── 추정식 ───────────────────────────────────────────────────────────


def test_estimate_pulls_toward_the_contract_when_records_are_few():
    obs = [{"streamId": "s", "vendorOrgId": "a", "rel": -0.3, "evidence": ["e1"]}]
    made = personal.estimate(obs, 3, "s", "a")
    # 폐기물 수준 −0.3/4, 업체 수준 (−0.3 + 3 × −0.075)/4
    assert made["streamRate"] == pytest.approx(-0.075)
    assert made["rate"] == pytest.approx((-0.3 + 3 * -0.075) / 4)
    assert made["records"] == 1 and made["streamRecords"] == 1


def test_a_new_vendor_inherits_the_company_tendency_for_that_waste():
    obs = [{"streamId": "s", "vendorOrgId": "a", "rel": -0.2, "evidence": []} for _ in range(6)]
    other = personal.estimate(obs, 3, "s", "b")
    assert other["records"] == 0
    assert other["rate"] == pytest.approx(other["streamRate"]) and other["rate"] < 0
    assert personal.estimate(obs, 3, "x", "a") is None  # 그 폐기물 기록이 없으면 보정하지 않는다


def test_more_records_trust_the_records_more():
    few = [{"streamId": "s", "vendorOrgId": "a", "rel": -0.2, "evidence": []}] * 2
    many = few * 10
    assert (
        personal.estimate(many, 3, "s", "a")["rate"] < personal.estimate(few, 3, "s", "a")["rate"]
    )
    assert personal.estimate(many, 3, "s", "a")["rate"] > -0.2  # 기록만큼 내려가되 넘지는 않는다


def test_one_extreme_record_is_clipped():
    assert personal.relative(1000, 1) == personal.REL_LIMIT
    assert personal.relative(-500, 100) == -personal.REL_LIMIT
    assert personal.relative(10, 0) is None


def test_treatment_fees_move_the_same_direction():
    # 처리비(음수): 더 많이 냈으면(−120 대 −100) 나쁜 쪽(음수)이다
    assert personal.relative(-120, -100) == pytest.approx(-0.2)
    assert personal.expected_price(-100, -0.2) == pytest.approx(-120)


# ── 판정 반영 ────────────────────────────────────────────────────────


def test_records_do_not_change_the_judgment_without_approval(below):
    view = to_approval(below)
    sprue = _sprue(view)
    assert sprue["personal"] is None
    assert sprue["revenue"]["value"] == pytest.approx(320 * 600)


def test_approval_is_recorded_as_human_evidence_and_used(below):
    found = service.update_memory(below, HANBIT, {"applyToJudgment": True}, HANBIT)
    approval = found["calibration"]["applied"]
    evidence = below.registry.get(approval["evidence"], HANBIT)
    assert evidence.kind == "human" and evidence.meta["memoryApproval"] is True
    assert evidence.value == memory.PROPOSED_STRENGTH
    view = to_approval(below)
    sprue = _sprue(view)
    rel = (300 - 320) / 320
    stream_rate = 3 * rel / 6
    rate = (3 * rel + 3 * stream_rate) / 6
    assert sprue["personal"]["records"] == 3 and sprue["personal"]["streamRecords"] == 3
    assert sprue["personal"]["rate"]["value"] == pytest.approx(rate * 100)
    expected = 320 * (1 + rate)
    assert sprue["personal"]["expectedUnitPrice"]["value"] == pytest.approx(expected)
    assert sprue["personal"]["expectedUnitPrice"]["role"] == "unitPrice"
    # 계약 단가는 그대로 두고, 매각 수익과 한 달 차이를 예상 단가로 계산한다
    assert sprue["unitPrice"]["value"] == 320
    assert sprue["revenue"]["value"] == pytest.approx(expected * 600)
    # 계산 사슬: 예상 단가 → 보정 비율 → 검수 근거와 승인 근거
    rate_calc = below.registry.get(sprue["personal"]["rate"]["evidence"][0], HANBIT)
    # 입력 근거는 계산할 때의 사본(snapshot)으로 고정된다. 원래 ID로 따라간다
    originals = [
        below.registry.get(e, HANBIT).meta.get("originalId", e)
        for e in rate_calc.meta["inputEvidence"]
    ]
    assert approval["evidence"] in originals
    assert any("검수-insp-" in e for e in originals)


def test_other_offers_are_compared_on_the_same_basis(below):
    service.update_memory(below, HANBIT, {"applyToJudgment": True}, HANBIT)
    sprue = _sprue(to_approval(below))
    others = [a for a in sprue["alternatives"] if a["vendorOrgId"] == MISO]
    assert others and others[0]["personalUnitPrice"] is not None
    assert "회사 기록 보정 전" in others[0]["note"]
    # 미소환경과는 거래 기록이 없어 이 회사의 스프루 기록(폐기물 수준)만큼 보정한다
    assert others[0]["personalUnitPrice"]["value"] < others[0]["unitPrice"]["value"]


def test_turning_off_stops_the_adjustment_but_keeps_the_approval_record(below):
    found = service.update_memory(below, HANBIT, {"applyToJudgment": True}, HANBIT)
    kept = found["calibration"]["applied"]["evidence"]
    off = service.update_memory(below, HANBIT, {"applyToJudgment": False}, HANBIT)
    assert off["calibration"]["applied"] is None
    assert _sprue(to_approval(below))["personal"] is None
    assert below.registry.get(kept, HANBIT).kind == "human"
    # 기록 사용을 끄면 판정 반영도 멈추고, 꺼진 채로는 승인할 수 없다
    service.update_memory(below, HANBIT, {"applyToJudgment": True}, HANBIT)
    assert (
        service.update_memory(below, HANBIT, {"enabled": False}, HANBIT)["calibration"]["applied"]
        is None
    )
    with pytest.raises(service.ServiceError) as caught:
        service.update_memory(below, HANBIT, {"applyToJudgment": True}, HANBIT)
    assert caught.value.code == "MEMORY_DISABLED"


def test_a_forged_approval_is_not_used(below):
    from app.v3.judge import toolbox

    view = to_approval(below)
    assert (
        below.store.get_session(view["session"]["id"])["work"]["optimize"]["output"]["personal"]
        is None
    )
    # 업체 단가 근거(doc)를 승인처럼 넘기면 보정하지 않고 확인 필요로 남긴다
    fake = {
        "approval": _sprue(view)["unitPrice"]["evidence"][0],
        "observations": [
            {"streamId": "st_sprue", "vendorOrgId": SAEGIL, "rel": -0.9, "evidence": []}
        ],
    }
    result = toolbox.call_tool(
        "optimize_assignment",
        {"streams": below.store.list_streams(HANBIT), "vendors": [], "history": fake},
        registry=below.registry,
        org_id=HANBIT,
        today=below.today,
    )
    assert result["output"]["personal"] is None
    assert any(c["key"] == "history_approval" for c in result["checks"])


# ── 회사 측 AI와 업체 화면 ───────────────────────────────────────────


def test_company_ai_asks_for_inspection_terms_when_records_came_in_below(below):
    view = to_approval(below)
    thread = thread_of(view, SAEGIL)
    inquiry = next(m for m in thread["messages"] if m["type"] == "inquiry")
    assert "합의보다 낮게 나온 적이 여러 번" in inquiry["text"]
    assert not any(ch.isdigit() for ch in inquiry["text"].split("지난 반입 검수")[1])
    # 다른 업체에게는 새길과의 기록을 말하지 않는다
    miso = next(m for m in thread_of(view, MISO)["messages"] if m["type"] == "inquiry")
    assert "지난 반입 검수" not in miso["text"]


def test_vendor_never_sees_the_adjustment(below):
    service.update_memory(below, HANBIT, {"applyToJudgment": True}, HANBIT)
    view = to_approval(below)
    mine = service.get_view(below, view["session"]["id"], SAEGIL)
    assert all(a["personal"] is None for a in mine["result"]["assignments"])
    assert all(a["alternatives"] == [] for a in mine["result"]["assignments"])


def test_memory_view_previews_the_calibration(below):
    found = service.get_memory(below, HANBIT, HANBIT)
    calibration = found["calibration"]
    assert calibration["applied"] is None and calibration["strength"] == memory.PROPOSED_STRENGTH
    sprue = next(s for s in calibration["streams"] if s["streamId"] == "st_sprue")
    assert sprue["records"] == 3 and sprue["streamRatePct"] < 0
    assert sprue["vendors"][0]["vendorOrgId"] == SAEGIL and sprue["vendors"][0]["ratePct"] < 0


# ── 업체 쪽 회사 기록, 보정 강도 제안, 체험하기 ──────────────────────


def test_vendor_sees_its_own_inspections_grouped_by_emitter(below):
    found = service.get_memory(below, SAEGIL, SAEGIL)
    assert found["role"] == "vendor" and found["summary"]["inspections"] == 3
    (hanbit,) = [e for e in found["emitters"] if e["emitterOrgId"] == HANBIT]
    assert hanbit["deals"] >= 1 and hanbit["inspections"]["status"] == "below"
    assert all(r["streamName"] and r["vendorOrgId"] == SAEGIL for r in hanbit["inspectionRows"])
    # 다른 업체(미소환경)는 새길의 검수를 보지 못한다
    miso = service.get_memory(below, MISO, MISO)
    assert miso["summary"]["inspections"] == 0


def test_strength_is_suggested_only_with_enough_real_records():
    small = personal.suggest_strength({("a", "s"): [-0.1, -0.1]})
    assert small["suggested"] is None and "적어" in small["reason"]
    groups = {
        (f"e{i}", "s"): [base + d for d in (-0.02, 0.0, 0.02)]
        for i, base in enumerate((-0.3, -0.1, 0.0, 0.1, 0.2, -0.2))
    }
    found = personal.suggest_strength(groups)
    # 묶음 안 흔들림이 작고 묶음 사이 차이가 크면 k가 작다(회사 기록을 빨리 믿는다)
    assert found["suggested"] is not None and 0 < found["suggested"] < 1
    same = {(f"e{i}", "s"): [-0.1, 0.1, 0.0] for i in range(6)}
    assert personal.suggest_strength(same)["suggested"] is None


def test_admin_status_carries_the_suggestion_but_nothing_changes(below):
    from tests.v3.runtime_fixtures import ADMIN

    status = service.admin_status(below, ADMIN)
    suggestion = status["retrain"]["personalStrength"]
    # 시연 회사 사이의 기록은 실제 기록이 아니라 세지 않는다
    assert suggestion["records"] == 0 and suggestion["suggested"] is None
    assert suggestion["current"] == memory.PROPOSED_STRENGTH


def test_demo_shows_the_adjustment():
    """체험하기: 시연 지난 거래의 스프루 검수 셋 중 둘이 합의보다 낮고, 시연 회사는 판정 반영을 승인해 둔다."""
    from app.v3.evidence.registry import InMemoryRegistry
    from app.v3.store import InMemoryStore

    app = service.App(InMemoryStore(), InMemoryRegistry(), auth_mode="bff", today="2026-11-17")
    found = service.reset_demo(app)
    view = service.get_view(app, found["demoSessionId"], HANBIT)
    sprue = _sprue(view)
    assert sprue["personal"] is not None and sprue["personal"]["rate"]["value"] < 0
    approval = next(a for a in view["approvals"] if a["threadId"] == thread_of(view, SAEGIL)["id"])
    history = next(i for i in approval["review"]["items"] if i["key"] == "history")
    assert history["status"] == "check"
    assert service.get_memory(app, HANBIT, HANBIT)["calibration"]["applied"] is not None
    # 시연 담당자가 포장 필름 수분을 고친 가상 교정이 결과에 "담당자 교정 미반영"으로 보인다
    film = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_film")
    assert any(c["key"] == "correction_pattern" for c in film["checks"])


# ── 교정 경고, 업체 측 AI ───────────────────────────────────────────


def _correct(app, field, before, after):
    sid = service.list_sessions(app, HANBIT)[0]["id"]
    body = {"streamId": "st_sprue", "field": field, "before": before, "after": after}
    service.record_correction(app, sid, body, HANBIT)


def test_a_correction_not_yet_applied_is_flagged_on_the_next_search(below):
    _correct(below, "material", "PP", "PP+ABS")
    _correct(below, "moisturePct", 1, 4)
    sprue = _sprue(to_approval(below))
    found = [c for c in sprue["checks"] if c["key"] == "correction_pattern"]
    assert len(found) == 2 and all(c["status"] == "conditional" and c["evidence"] for c in found)
    material = next(c for c in found if "재질" in c["text"])
    assert "'PP+ABS'로 고쳤지만" in material["text"] and "'PP'" in material["text"]
    moisture = next(c for c in found if "수분" in c["text"])
    assert not any(
        ch.isdigit() for ch in moisture["text"]
    )  # 숫자는 근거가 열리는 자리에서만 보인다


def test_a_correction_already_applied_is_not_flagged(below):
    _correct(below, "material", "PP+ABS", "PP")  # 지금 값과 같다
    sprue = _sprue(to_approval(below))
    assert not [c for c in sprue["checks"] if c["key"] == "correction_pattern"]


def test_correction_warnings_stay_with_the_emitter_and_follow_the_switch(below):
    _correct(below, "material", "PP", "PP+ABS")
    view = to_approval(below)
    mine = service.get_view(below, view["session"]["id"], SAEGIL)
    assert not [
        c
        for a in mine["result"]["assignments"]
        for c in a["checks"]
        if c["key"] == "correction_pattern"
    ]
    service.update_memory(below, HANBIT, {"enabled": False}, HANBIT)
    sprue = _sprue(to_approval(below))
    assert not [c for c in sprue["checks"] if c["key"] == "correction_pattern"]


def test_vendor_ai_asks_for_grade_confirmation_after_past_downgrades(below):
    view = to_approval(below)
    replies = [
        m
        for m in thread_of(view, SAEGIL)["messages"]
        if m["from"] == SAEGIL and m["type"] in ("offer", "conditional")
    ]
    sprue = [m for m in replies if "PP 스프루·러너는 지난 반입 검수" in m["text"]]
    assert sprue and sprue[0]["type"] == "conditional"
    # 다른 업체는 새길과의 기록을 받지 않는다
    miso = [m for m in thread_of(view, MISO)["messages"] if m["from"] == MISO]
    assert not any("지난 반입 검수" in m["text"] for m in miso)


def test_vendor_context_has_no_history_when_the_emitter_turned_it_off(below):
    service.update_memory(below, HANBIT, {"enabled": False}, HANBIT)
    view = to_approval(below)
    replies = [m for m in thread_of(view, SAEGIL)["messages"] if m["from"] == SAEGIL]
    assert not any("지난 반입 검수" in m["text"] for m in replies)


# ── 모델이 빠뜨려도 코드가 붙이는 지난 검수 문장 ─────────────────────


def test_vendor_condition_is_attached_even_if_the_model_left_it_out():
    from app.v3.a2a.message import Message
    from app.v3.agents import past
    from app.v3.agents.vendor import _attach_past

    reply = Message(
        thread_id="th_1",
        sender=SAEGIL,
        recipient=HANBIT,
        type="offer",
        stage=5,
        text="PP 스프루·러너를 받겠습니다.",
        stream_ids=("st_sprue",),
    )
    terms = {"items": [{"streamId": "st_sprue", "unitPriceRef": "x"}], "conditions": []}
    history = [
        {"streamId": "st_sprue", "status": "below"},
        {"streamId": "st_film", "status": "few"},
    ]
    _attach_past(reply, terms, history, {"st_sprue": {"name": "PP 스프루·러너"}})
    assert reply.type == "conditional"
    assert terms["conditions"] == [past.vendor_condition("PP 스프루·러너")]
    assert past.MARK in reply.text
    # 두 번 붙이지 않는다
    _attach_past(reply, terms, history, {"st_sprue": {"name": "PP 스프루·러너"}})
    assert len(terms["conditions"]) == 1 and reply.text.count(past.MARK) == 1


def test_company_line_is_attached_even_if_the_model_left_it_out(below):
    from app.v3.agents import past
    from tests.v3.runtime_fixtures import only_for, rule_fallback, scripted

    def bare(messages, tools, schema):
        """규칙 정책의 답에서 지난 검수 문장만 뺀 모델(빠뜨린 경우를 흉내 낸다)."""
        out = rule_fallback(messages, tools, schema)
        if isinstance(out, dict) and out.get("type") == "inquiry" and out.get("text"):
            out["text"] = "\n".join(l for l in out["text"].split("\n") if past.MARK not in l)
        return out

    below.llm_factory = only_for("company", HANBIT, scripted(*[bare] * 400, pad=0))
    view = to_approval(below)
    inquiry = next(m for m in thread_of(view, SAEGIL)["messages"] if m["type"] == "inquiry")
    assert inquiry["text"].count(past.MARK) == 1
