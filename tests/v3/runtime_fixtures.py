"""두 에이전트, 진행기, 서비스 테스트가 함께 쓰는 도우미. 회사와 수치는 모두 가상이다."""

from __future__ import annotations

import json

from app.v3 import service
from app.v3.a2a.message import Message
from app.v3.agents.llm import ScriptedLLM
from app.v3.agents.policy_rule import RulePolicy
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.validator import validate_message, validate_text
from app.v3.store import InMemoryStore

TODAY = "2026-11-17"
CLOCK = "2026-11-17T00:00:00+00:00"
HANBIT, DAON = "org_hanbit", "org_daon"
SAEGIL, NAMHAE, MISO = "org_saegil", "org_namhae", "org_miso"
ADMIN = "org_admin"  # 운영 조직(관리자 화면과 시연 되돌리기)
FILM_MOISTURE = 2.0  # 시연에서 담당자가 답하는 가상 값


def make_app(llm_factory=None, **options):
    app = service.App(
        InMemoryStore(lambda: CLOCK),
        InMemoryRegistry(),
        llm_factory=llm_factory,
        today=TODAY,
        **options,
    )
    service.seed(app)
    return app


def briefing_of(messages):
    """에이전트가 모델에게 준 마지막 입력(JSON)."""
    for message in reversed(messages):
        if message["role"] == "user":
            return json.loads(message["content"])
    raise AssertionError("입력이 없다")


def rule_fallback(messages, tools, schema):
    return RulePolicy().complete(messages, tools=tools, response_schema=schema)


def scripted(*responses, pad=60):
    """정해 둔 응답을 차례로 내고, 다 쓰면 규칙 정책으로 답하는 ScriptedLLM."""
    return ScriptedLLM([*responses, *([rule_fallback] * pad)])


def only_for(role, org_id, llm):
    """한 조직의 한 역할에만 llm을 쓰고 나머지는 규칙 정책(None)으로 돌린다."""

    def factory(asked_role, asked_org, _model):
        return llm if (asked_role, asked_org) == (role, org_id) else None

    return factory


def start(app, emitter=HANBIT):
    view = service.create_session(app, emitter, emitter)
    return service.run_session(app, view["session"]["id"], emitter)


def answer_all(app, view, emitter=HANBIT):
    """포장 필름의 수분만 값으로 답하고 나머지는 '모름'으로 답한다."""
    sid = view["session"]["id"]
    for question in view["questions"]:
        if question["answered"]:
            continue
        film_moisture = question["streamId"] == "st_film" and question["field"] == "moisturePct"
        value = FILM_MOISTURE if film_moisture else "unknown"
        service.answer_question(app, sid, question["id"], value, emitter)
    return service.run_session(app, sid, emitter)


def to_approval(app, emitter=HANBIT):
    view = start(app, emitter)
    for _ in range(3):
        if view["session"]["status"] != "awaiting_answers":
            break
        view = answer_all(app, view, emitter)
    return view


def approve_all(app, view, emitter=HANBIT):
    sid = view["session"]["id"]
    vendors = {t["id"]: t["vendorOrgId"] for t in view["threads"]}
    for item in view["approvals"]:
        service.approve(app, sid, item["threadId"], emitter)
        service.approve(app, sid, item["threadId"], vendors[item["threadId"]])
    return service.get_view(app, sid, emitter)


def thread_of(view, vendor, status=None):
    found = [
        t
        for t in view["threads"]
        if t["vendorOrgId"] == vendor and (status is None or t["status"] == status)
    ]
    opened = [t for t in found if t["status"] != "not_opened"]
    return (opened or found)[0]


def recheck(app, view, emitter=HANBIT):
    """나간 메시지와 문서를 근거 검증기에 다시 넣는다. (검사한 수, 통과한 수)."""
    checked = passed = 0
    for thread in view["threads"]:
        for item in thread["messages"]:
            message = Message.from_dict({**item, "threadId": thread["id"]})
            checked += 1
            passed += validate_message(message, app.registry, today=TODAY).ok
    for document in view["documents"]:
        checked += 1
        passed += validate_text(
            document["text"], document["claims"], app.registry, org_id=emitter, today=TODAY
        ).ok
    return checked, passed


def draft(kind, text, *, stage=5, streams=(), refs=(), **extra):
    return {
        "type": kind,
        "stage": stage,
        "streamIds": list(streams),
        "text": text,
        "claimRefs": list(refs),
        "toolCalls": [],
        **extra,
    }
