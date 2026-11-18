"""질문 한도를 다 쓴 뒤 회사 측 차례(실제 모델 실행에서 본 일). 회사와 수치는 모두 가상이다.

실제 모델 실행에서 회사 측 AI가 질문 한도(묶음당 세 개, 두 묶음)를 다 쓴 뒤에도 담당자에게 값을 다시
물으려 했고(ask_user), 두 번째로 그러자 대화가 담당자에게 넘어갔다. 질문 정책은 한도가 끝나면 남은
값을 미확인으로 두고 업체 질문(조건부 제안)에 남기라고 한다.

- 한도를 다 썼으면 회사 측 차례에서 ask_user를 고를 수 없다(응답 스키마와 안내 모두). 모르는 값은
  '확인 필요'로 두고 조건부 제안으로 나아가게 안내한다.
- 스키마를 지키지 않는 모델이 그래도 ask_user를 내면 담당자에게 넘기지 않고 그 까닭과 함께 다시 쓰게 한다.
"""

from __future__ import annotations

import json

from app.v3.agents import company
from app.v3.agents.llm import ScriptedLLM
from tests.v3.runtime_fixtures import (
    HANBIT,
    briefing_of,
    make_app,
    only_for,
    rule_fallback,
    to_approval,
)


def _ask_user(briefing):
    stream_id = briefing["thread"]["streamIds"][0]
    return json.dumps(
        {
            "type": "ask_user",
            "stage": 4,
            "streamIds": [stream_id],
            "text": "",
            "toolCalls": [],
            "userQuestions": [{"streamId": stream_id, "field": "moisturePct"}],
        },
        ensure_ascii=False,
    )


def _types(schema):
    return list((schema or {}).get("properties", {}).get("type", {}).get("enum") or [])


def _reply_calls(llm):
    return [c for c in llm.calls if briefing_of(c["messages"]).get("task") == "reply"]


def _handoffs(view):
    return [
        m
        for t in view["threads"]
        for m in t["messages"]
        if m["type"] == "handoff" and m["from"] == HANBIT
    ]


def test_company_does_not_reask_the_user_after_the_question_budget_is_spent():
    """ask_user를 고를 수 있으면 언제나 고르는 모델: 한도를 다 쓴 뒤에는 고를 수 없어 대화가 이어진다."""

    def greedy(messages, tools, schema):
        briefing = briefing_of(messages)
        if briefing.get("task") == "reply" and "ask_user" in _types(schema):
            return _ask_user(briefing)
        return rule_fallback(messages, tools, schema)

    llm = ScriptedLLM([greedy] * 200)
    app = make_app(only_for("company", HANBIT, llm))
    view = to_approval(app)

    batches = {q["batch"] for q in view["questions"]}
    assert batches == {1, 2}  # 시연 진단은 대화를 열기 전에 두 묶음을 다 쓴다
    replies = _reply_calls(llm)
    assert replies, "회사 측 답 차례가 없었다"
    for call in replies:
        assert "ask_user" not in _types(call["response_schema"])
        assert "userQuestions" not in call["response_schema"]["properties"]
        briefing = briefing_of(call["messages"])
        assert briefing["state"]["cannotAskUser"] is True
        assert "ask_user" not in briefing["thread"]["allowedTypes"]
        assert company.NO_MORE_QUESTIONS in briefing["guide"]
        streams = set(briefing["thread"]["streamIds"])
        assert all(u["streamId"] in streams for u in briefing["state"]["unresolved"])
    assert _handoffs(view) == []
    assert {t["status"] for t in view["threads"] if t["status"] != "not_opened"} == {"agreed"}
    assert view["session"]["status"] == "awaiting_approval"
    # 모르는 값은 결과 한 장의 확인할 것으로 남는다
    assert {"streamId": "st_defect", "field": "moisturePct"} in view["unresolved"]


def test_a_model_that_ignores_the_schema_is_told_why_and_rewrites():
    """스키마를 지키지 않고 ask_user를 낸 첫 답은 거부되고, 다시 쓴 답이 나간다(담당자에게 넘기지 않는다)."""
    sent = {"ask": 0}

    def stubborn(messages, tools, schema):
        briefing = briefing_of(messages)
        if briefing.get("task") == "reply" and sent["ask"] == 0:
            sent["ask"] += 1
            return _ask_user(briefing)
        return rule_fallback(messages, tools, schema)

    llm = ScriptedLLM([stubborn] * 200)
    app = make_app(only_for("company", HANBIT, llm))
    view = to_approval(app)

    assert sent["ask"] == 1
    retry = next(
        briefing_of(c["messages"])
        for c in _reply_calls(llm)
        if briefing_of(c["messages"]).get("rejections")
    )
    rejected = retry["rejections"][0]
    assert rejected["code"] == "TYPE_NOT_ALLOWED"
    assert rejected["message"] == company.NO_MORE_QUESTIONS_REJECTION
    assert _handoffs(view) == []
    assert view["session"]["status"] == "awaiting_approval"


def test_company_guide_without_question_budget():
    class Question:
        type = "question"

    class Conditional:
        type = "conditional"

    common = {"uncovered": [], "asked": False, "names": {"st_a": "스프루"}, "results": []}
    types = ("question", "answer", "accept")
    spent = company.guide(
        task="reply", types=types, incoming=Question(), cannot_ask_user=True, **common
    )
    assert spent[1] == company.NO_MORE_QUESTIONS
    assert "조건부로 제안해 달라고 적는다" in spent[-1] and "ask_user로" not in spent[-1]
    assert company.NO_MORE_QUESTIONS in company.guide(
        task="reply", types=types, incoming=Conditional(), cannot_ask_user=True, **common
    )
    open_budget = company.guide(
        task="reply",
        types=(*types, "ask_user"),
        incoming=Question(),
        cannot_ask_user=False,
        **common,
    )
    assert company.NO_MORE_QUESTIONS not in open_budget
    assert "type을 ask_user로 하고" in open_budget[-1]
    # 첫 문의와 확정 통지에는 붙이지 않는다
    first = company.guide(
        task="inquiry", types=("inquiry",), incoming=None, cannot_ask_user=True, **common
    )
    assert company.NO_MORE_QUESTIONS not in first
