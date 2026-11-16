"""여러 흐름을 거절한 대화의 닫는 이유(statusReason)에는 거절한 흐름이 모두 적힌다. 수치는 가상이다."""

from __future__ import annotations

import json

from app.v3 import service
from app.v3.a2a.message import Message
from app.v3.a2a.thread import Thread
from app.v3.agents.orchestrator import Orchestrator
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    SAEGIL,
    briefing_of,
    make_app,
    only_for,
    rule_fallback,
    scripted,
    thread_of,
    to_approval,
)


def decline_all(app, vendor, words):
    profile = app.store.get_vendor_profile(vendor)
    profile["guardrails"] = {**profile["guardrails"], "autoDecline": list(words)}
    app.store.put_vendor_profile(profile)


def test_status_reason_lists_every_refused_stream_with_its_reason():
    app = make_app()
    decline_all(app, MISO, ["불량", "스프루"])  # 미소환경과 연 대화의 두 흐름이 모두 걸린다
    view = to_approval(app)
    thread = thread_of(view, MISO)
    assert thread["status"] == "declined" and sorted(thread["streamIds"]) == [
        "st_defect",
        "st_sprue",
    ]
    reason = thread["statusReason"]
    assert reason.startswith("받지 않는 흐름: ")
    assert reason == (
        "받지 않는 흐름: 불량 사출품은 업체의 자동 거절 조건(불량)에 해당합니다 / "
        "스프루·런너는 업체의 자동 거절 조건(스프루)에 해당합니다."
    )
    # 메시지에 실린 이유(meta.reason)는 그대로다. 닫는 이유만 모두 적는다
    last = thread["messages"][-1]
    assert last["type"] == "decline" and len(last["meta"]["excluded"]) == 2
    assert last["meta"]["reason"].rstrip(".") in reason
    # 업체가 보는 뷰에도 같은 이유가 나간다
    theirs = thread_of(service.get_view(app, view["session"]["id"], MISO), MISO)
    assert theirs["statusReason"] == reason
    assert "은(는)" not in reason


def test_same_reason_is_written_once_for_all_its_streams():
    app = make_app()
    view = to_approval(app)
    raw = next(
        t
        for t in app.store.list_threads(session_id=view["session"]["id"])
        if t["vendorOrgId"] == SAEGIL and t["status"] == "agreed"
    )
    thread = Thread.from_dict(raw)
    message = Message(
        thread_id=thread.id,
        sender=SAEGIL,
        recipient=HANBIT,
        type="decline",
        stage=4,
        text="받을 수 없습니다.",
        stream_ids=thread.stream_ids,
        meta={
            "reason": "받을 수 있는 흐름이 없습니다.",
            "excluded": [
                {"streamId": "st_sprue", "reason": "창고가 찼습니다."},
                {"streamId": "st_film", "reason": "창고가 찼습니다."},
                {"streamId": "st_defect", "reason": "매입 기준에 맞지 않습니다."},
                {"streamId": "st_daon_pp", "reason": "이 대화의 흐름이 아닙니다."},
            ],
        },
    )
    reason = Orchestrator(app)._decline_reason(thread, message)
    assert reason == (
        "받을 수 있는 흐름이 없습니다. 받지 않는 흐름: 스프루·런너, 포장 필름: 창고가 찼습니다 / "
        "불량 사출품: 매입 기준에 맞지 않습니다."
    )


def test_one_refused_stream_keeps_the_plain_reason():
    app = make_app()
    decline_all(app, MISO, ["불량"])
    view = to_approval(app)
    thread = thread_of(view, MISO)
    # 한 흐름만 받지 않으면 대화는 닫히지 않는다(나머지 흐름의 조건을 낸다)
    assert thread["status"] != "declined"
    message = Message(
        thread_id=thread["id"],
        sender=MISO,
        recipient=HANBIT,
        type="decline",
        stage=4,
        text="받을 수 없습니다.",
        meta={
            "reason": "한 가지 이유",
            "excluded": [{"streamId": "st_defect", "reason": "한 가지 이유"}],
        },
    )
    raw = app.store.get_thread(thread["id"])
    assert Orchestrator(app)._decline_reason(Thread.from_dict(raw), message) == "한 가지 이유"


def test_a_model_that_names_only_one_stream_still_gets_all_of_them_listed():
    def only_first(messages, tools, schema):
        response = rule_fallback(messages, tools, schema)
        output = json.loads(response["content"])
        if output.get("type") == "decline" and briefing_of(messages).get("role") == "vendor":
            output["reason"] = output["excluded"][0]["reason"]
            output["text"] = "문의하신 흐름은 받을 수 없습니다."
            return {"content": json.dumps(output, ensure_ascii=False)}
        return response

    app = make_app(only_for("vendor", MISO, scripted(*[only_first] * 40, pad=0)))
    decline_all(app, MISO, ["불량", "스프루"])
    view = to_approval(app)
    thread = thread_of(view, MISO)
    assert thread["status"] == "declined"
    assert thread["messages"][-1]["meta"]["reason"].startswith("불량 사출품")
    assert "불량 사출품" in thread["statusReason"] and "스프루·런너" in thread["statusReason"]
