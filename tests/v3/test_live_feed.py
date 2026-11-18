"""진행 기록 쓰기(live.Recorder, live.Turn): 초안 작성의 알림이 진행 기록이 되는 모양."""

from __future__ import annotations

from app.v3 import live
from app.v3.agents import draft
from app.v3.store import InMemoryStore
from tests.v3.runtime_fixtures import HANBIT, SAEGIL


def test_turn_maps_draft_steps_to_activity():
    store = InMemoryStore(lambda: "2026-11-17T00:00:00+00:00")

    class Thread:
        id, emitter_org, vendor_org = "th_1", HANBIT, SAEGIL

    feed = live.Recorder(store)
    with feed.turn("ses_1", Thread, "vendor", 5) as turn:
        assert draft.LIVE.get() is turn
        trace = draft.Trace()
        trace.notify("drafting", {"attempt": 1})
        trace.notify("validating", {"attempt": 1})
        trace.add(
            "validator",
            "validation",
            "검증 거부",
            ok=False,
            attempt=1,
            rejections=[{"code": "GUARDRAIL_PRICE_BELOW_FLOOR"}, {"code": "VALUE_MISMATCH"}],
        )
        trace.notify("drafting", {"attempt": 1})  # 도구를 부른 뒤 이어 쓰는 같은 초안
        trace.notify("drafting", {"attempt": 2})
        trace.add("vendor_agent", "tool_call", "x", tool="cost_carbon", status="calculated")
    assert draft.LIVE.get() is None and draft.Trace().live is None
    entries = store.list_activity("ses_1")
    assert [e["kind"] for e in entries] == [
        "drafting",
        "validating",
        "rejected",
        "rewriting",
        "tool",
    ]
    assert all(e["orgId"] == SAEGIL and e["threadId"] == "th_1" for e in entries)
    assert entries[2]["reasons"] == ["업체 기준 밖의 조건", "근거와 다른 값"]
    assert entries[2]["codes"] == ["GUARDRAIL_PRICE_BELOW_FLOOR", "VALUE_MISMATCH"]
    assert entries[3]["attempt"] == 2 and "고쳐 쓰고 있습니다" in entries[3]["text"]
    assert not any(w in entries[3]["text"] for w in ("거부", "초안", "에이전트"))
    assert entries[4]["tool"] == "cost_carbon" and entries[4]["stage"] == 5
    assert turn.steps == 6  # 알림 수(작성 쪽에서 온 것)


def test_every_rejection_code_has_a_shown_reason():
    for code in draft.PUBLIC_MESSAGES:
        assert code in live.REASONS, code
    assert live.reason_of("GUARDRAIL_DAY_NOT_ALLOWED") == live.GUARDRAIL_REASON
    assert live.reason_of("OTHER_EMITTER_NAMED") == live.OTHER_EMITTER_REASON
    assert live.reason_of("SOMETHING_NEW") == live.DEFAULT_REASON
    for text in live.REASONS.values():  # 사유에 숫자나 내부 코드가 없다
        assert not any(c.isdigit() for c in text) and not any("A" <= c <= "Z" for c in text)
