"""두 에이전트, 진행기, 서비스 테스트가 함께 쓰는 도우미. 회사와 수치는 모두 가상이다."""

from __future__ import annotations

import json

from app.v3 import service
from app.v3.a2a.message import Message
from app.v3.agents.llm import ScriptedLLM
from app.v3.agents.policy_rule import RulePolicy
from app.v3.demo.seed import DEMO_ANSWERS
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.validator import validate_message, validate_text
from app.v3.store import InMemoryStore

TODAY = "2026-11-17"
CLOCK = "2026-11-17T00:00:00+00:00"
HANBIT, DAON = "org_hanbit", "org_daon"
SAEGIL, NAMHAE, MISO = "org_saegil", "org_namhae", "org_miso"
ADMIN = "org_admin"  # 운영 조직(관리자 화면과 시연 되돌리기)
FILM_MOISTURE = DEMO_ANSWERS[("st_film", "moisturePct")]  # 시연에서 담당자가 답하는 가상 값


# 미소환경이 업체 정보에서 "노선 밖도 방문 수거하고 운반비는 단가에 포함"이라고 답한 상태(가상).
# 시연 시드는 이 답이 없어 미소환경 경로가 "운반비 미정"이고, 계약 검토 담당이 그 승인을 막는다(필수 운반비 미정)
MISO_VISITS = {"status": "visit", "basis": "included"}


def make_app(llm_factory=None, miso_visits=True, **options):
    """시연 세계. miso_visits가 참이면 미소환경의 노선 밖 수거 답(MISO_VISITS)을 넣어 세 업체 모두 확정까지 간다.
    노선 밖 운반비 미정 장면을 보는 시험은 miso_visits=False로 시드 그대로 쓴다."""
    # 시나리오 시험은 협상 전 흐름(매입 기준표 단가)을 본다. 협상은 test_negotiation.py가 negotiate=True로 본다
    options.setdefault("negotiate", False)
    app = service.App(
        InMemoryStore(lambda: CLOCK),
        InMemoryRegistry(),
        llm_factory=llm_factory,
        today=TODAY,
        **options,
    )
    service.seed(app)
    if miso_visits:
        service.update_vendor(app, MISO, {"transport": {"offRoute": dict(MISO_VISITS)}}, MISO)
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
    """시연 담당자의 답(DEMO_ANSWERS: 포장 필름 수분, 필름·퍼지 현행 처리비)만 값으로 답하고 나머지는 '모름'."""
    sid = view["session"]["id"]
    for question in view["questions"]:
        if question["answered"]:
            continue
        value = DEMO_ANSWERS.get((question["streamId"], question["field"]), "unknown")
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
    """양쪽이 승인할 수 있는 대화를 모두 승인한다. 계약 검토 담당이 막은 대화(예: 필수 운반비 미정)는 건너뛴다."""
    sid = view["session"]["id"]
    vendors = {t["id"]: t["vendorOrgId"] for t in view["threads"]}
    for item in view["approvals"]:
        if (item.get("review") or {}).get("status") == "blocked":
            continue
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
