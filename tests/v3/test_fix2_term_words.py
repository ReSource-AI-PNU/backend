"""숫자 없이 조건을 뜻하는 낱말("무상", "주말", "전량" 등)은 terms 항목이나 claim이 있어야 나간다.

없으면 가드레일 검사에서 UNBACKED_TERM으로 거부한다. 업체 측과 회사 측 모두 같다. 수치는 가상이다.
"""

from __future__ import annotations

import json

import pytest

from app.v3 import service
from app.v3.a2a import guardrails as rails
from app.v3.a2a import term_words
from app.v3.a2a.message import Claim, Message
from app.v3.agents.draft import PUBLIC_MESSAGES, public_rejections
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    SAEGIL,
    approve_all,
    make_app,
    recheck,
    thread_of,
    to_approval,
)
from tests.v3.test_rules_adversarial import assert_clean, bad_app, everything

CODE = "UNBACKED_TERM"


def message(text, *, kind="offer", claims=(), **meta):
    return Message(
        thread_id="th_1",
        sender=SAEGIL,
        recipient=HANBIT,
        type=kind,
        stage=5,
        text=text,
        claims=tuple(claims),
        meta=dict(meta),
    )


def price(value, unit="원/kg"):
    return Claim("p", "number", value, ("doc:x",), unit)


def codes(found):
    return [(v.code, v.detail["key"]) for v in found]


# ── 낱말과 뒷받침 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("이번 달은 무상으로 수거해 드리겠습니다.", "free"),
        ("운송은 무료입니다.", "free"),
        ("첫 달은 반값에 드리겠습니다.", "half_price"),
        ("절반 가격으로 처리합니다.", "half_price"),
        ("주말에도 수거합니다.", "weekend"),
        ("공휴일에도 차를 보냅니다.", "holiday"),
        ("매일 수거할 수 있습니다.", "every_day"),
        ("물량은 무제한으로 받습니다.", "unlimited"),
        ("제한 없이 받겠습니다.", "unlimited"),
        ("발생하는 물량을 전량 매입합니다.", "all_volume"),
    ],
)
def test_each_word_is_refused_without_backing(text, key):
    found = rails.check_term_words(message(text, claims=[price(320)], terms={"days": ["화"]}))
    assert codes(found) == [(CODE, key)]
    assert "terms" in found[0].message and found[0].detail["word"] in text


@pytest.mark.parametrize(
    ("text", "claims", "terms"),
    [
        ("이 흐름은 무상으로 받습니다. 단가는 {{p}}입니다.", [price(0)], {}),
        ("처리비 없이 무상입니다.", [], {"items": [{"streamId": "st_x", "unitPriceKrwPerKg": 0}]}),
        ("주말에도 수거합니다.", [], {"days": ["화", "토"]}),
        ("매일 수거합니다.", [], {"days": ["월", "화", "수", "목", "금", "토", "일"]}),
        ("월 {{p}}을 전량 받습니다.", [Claim("p", "number", 600, ("m:x",), "kg/월")], {}),
    ],
)
def test_backed_words_pass(text, claims, terms):
    assert rails.check_term_words(message(text, claims=claims, terms=terms)) == []


@pytest.mark.parametrize(
    "text",
    [
        "수거는 화·목요일 가운데 정합니다.",
        "매입 기준을 충족합니다. 매입 단가는 {{p}}입니다.",
        "사출 공정에서 매일 발생하는 스프루입니다.",  # 흐름이 생기는 주기는 조건이 아니다
        "반입 검수에서 수분이 매입 기준 상한 이하로 확인될 것",
        "받을 수 있는 흐름을 합친 물량은 최소 물량 이상입니다.",
    ],
)
def test_ordinary_sentences_are_not_flagged(text):
    assert rails.check_term_words(message(text, claims=[price(320)], terms={"days": ["화"]})) == []


def test_words_inside_names_and_wrong_backing():
    # 조직·흐름 이름에 든 낱말은 조건이 아니다
    named = message("주말농장 필름의 조건을 알려 주세요.", kind="question")
    assert rails.check_term_words(named, None, ["주말농장 필름"]) == []
    assert codes(rails.check_term_words(named, None, [])) == [(CODE, "weekend")]
    # 0이 아닌 단가는 "무상"을 뒷받침하지 못하고, 평일만 적은 요일은 "주말"을 뒷받침하지 못한다
    assert rails.check_term_words(message("무상 수거", claims=[price(320)]))
    assert rails.check_term_words(message("주말 수거", terms={"days": ["월", "금"]}))
    assert rails.check_term_words(
        message("매일 수거", terms={"days": ["월", "화", "수", "목", "금"]})
    )
    # 물량 claim은 "전량"만 뒷받침한다. 금액 claim으로는 안 된다
    assert rails.check_term_words(message("전량 매입", claims=[price(320)]))
    # 해당하는 조건 항목이 없는 낱말은 무엇을 붙여도 거부한다
    full = {"days": list(rails.WEEKDAYS), "items": [{"streamId": "s", "unitPriceKrwPerKg": 0}]}
    for text in ("반값", "공휴일 수거", "무제한"):
        assert rails.check_term_words(message(text, claims=[price(0)], terms=full))


def test_sentences_outside_the_body_are_checked_too():
    found = rails.check_term_words(
        message(
            "조건부로 받습니다.", kind="conditional", terms={"conditions": ["공휴일에도 반입할 것"]}
        )
    )
    assert codes(found) == [(CODE, "holiday")]
    found = rails.check_term_words(
        message("받지 않습니다.", kind="decline", reason="무상이 아니면 어렵습니다")
    )
    assert codes(found) == [(CODE, "free")]
    several = rails.check_term_words(message("주말과 공휴일에도 전량 무상으로 받습니다."))
    assert {v.detail["key"] for v in several} == {"weekend", "holiday", "all_volume", "free"}


def test_the_word_list_is_a_constant_table_and_has_a_public_sentence():
    assert term_words.CODE == CODE
    keys = [w.key for w in term_words.TERM_WORDS]
    assert len(keys) == len(set(keys)) and {"free", "weekend", "all_volume"} <= set(keys)
    needs = {term_words.ZERO_PRICE, term_words.WEEKEND_DAYS, term_words.EVERY_DAY}
    assert {w.needs for w in term_words.TERM_WORDS} <= needs | {
        term_words.QUANTITY,
        term_words.NONE,
    }
    shown = public_rejections(
        [{"code": CODE, "message": "'무상'처럼 …", "detail": {"word": "무상"}}]
    )
    assert shown == [
        {"code": CODE, "message": PUBLIC_MESSAGES[CODE], "claimId": None, "detail": {}}
    ]
    assert "무상" not in PUBLIC_MESSAGES[CODE]


# ── 두 에이전트 ──────────────────────────────────────────────────────


def promise(sentence):
    def change(output, briefing):
        if output.get("text"):
            output["text"] += f"\n{sentence}"
        return output

    return change


@pytest.mark.parametrize(
    "sentence",
    [
        "이번 달은 무상으로 수거해 드리겠습니다.",
        "주말에도 수거합니다.",
        "첫 거래는 반값에 드립니다.",
    ],
)
def test_a_vendor_model_that_keeps_promising_is_handed_off(sentence):
    app, _ = bad_app("vendor", SAEGIL, promise(sentence))
    view = to_approval(app)
    session_id = view["session"]["id"]
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff"
    assert [m["type"] for m in thread["messages"]] == ["inquiry", "handoff"]
    rejections = thread["messages"][-1]["validation"]["rejections"]
    assert {r["code"] for r in rejections} == {CODE}
    assert all(r["message"] == PUBLIC_MESSAGES[CODE] and r["detail"] == {} for r in rejections)
    assert sentence not in everything(app, session_id)  # 약속한 문장은 어디에도 남지 않는다
    assert all(a["vendorOrgId"] != SAEGIL for a in view["result"]["assignments"])
    assert_clean(app, session_id)


def test_a_vendor_model_that_rewrites_gets_through_with_the_rejection_on_record():
    app, _ = bad_app("vendor", SAEGIL, promise("공휴일에도 수거합니다."), times=1)
    view = to_approval(app)
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "agreed"
    first = thread["messages"][1]
    assert first["validation"]["attempts"] == 2
    assert [r["code"] for r in first["validation"]["rejections"]] == [CODE]
    assert "공휴일" not in json.dumps(view, ensure_ascii=False)
    checked, passed = recheck(app, view)
    assert checked == passed


def test_a_condition_sentence_cannot_carry_the_promise_either():
    def change(output, briefing):
        if isinstance(output.get("terms"), dict):
            output["terms"]["conditions"] = [
                *output["terms"].get("conditions", []),
                "주말 반입도 가능",
            ]
        return output

    app, _ = bad_app("vendor", MISO, change)
    view = to_approval(app)
    thread = thread_of(view, MISO)
    assert thread["status"] == "handoff"
    assert "주말 반입도 가능" not in everything(app, view["session"]["id"])


def test_the_company_side_is_held_to_the_same_check():
    app, _llm = bad_app(
        "company", HANBIT, promise("전량 무상으로 가져가 주시면 됩니다."), role_task=("inquiry",)
    )
    view = to_approval(app)
    session_id = view["session"]["id"]
    opened = [t for t in view["threads"] if t["messages"]]
    assert opened and all(t["status"] == "handoff" for t in opened)
    for thread in opened:
        assert [m["type"] for m in thread["messages"]] == ["handoff"]
        found = {r["code"] for r in thread["messages"][0]["validation"]["rejections"]}
        assert CODE in found
    assert "무상으로 가져가" not in everything(app, session_id)
    assert view["approvals"] == []


def test_a_remote_vendor_that_skipped_the_check_is_caught_on_receipt():
    class Skipping:
        """자기 쪽 검사를 건너뛴 업체 측 서비스: 통과한 답에 약속을 덧붙여 보낸다."""

        def __init__(self, app):
            self.app = app

        def deliver(self, vendor_org, message, context):
            reply = service.handle_vendor_message(self.app, vendor_org, message, context)
            if vendor_org == SAEGIL and reply["type"] in ("offer", "conditional"):
                reply["text"] += "\n물량은 무제한으로 받습니다."
            return reply

    app = make_app()
    app.vendor_gateway = Skipping(app)
    view = to_approval(app)
    session_id = view["session"]["id"]
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "handoff"
    assert all(m["type"] not in ("offer", "conditional") for m in thread["messages"])
    recheck_events = [
        e
        for e in view["events"]
        if e["threadId"] == thread["id"] and e["detail"].get("recheckedBy") == HANBIT
    ]
    assert recheck_events and recheck_events[-1]["detail"]["codes"] == [CODE]
    assert "무제한" not in everything(app, session_id)


def test_the_demo_is_untouched_by_the_new_check():
    app = make_app()
    view = approve_all(app, to_approval(app))
    assert view["session"]["status"] == "confirmed"
    for thread in view["threads"]:
        for item in thread["messages"]:
            assert item["validation"]["rejections"] == []
    checked, passed = recheck(app, view)
    assert checked == passed == 24  # 대화 12, confirm 3, 문서 9(요청서·이행 계획서·계약서 초안)
