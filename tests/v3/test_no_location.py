"""배출 기업의 위치가 없을 때. 업체 측은 노선 추가 비용을 계산하지 못한다(도구가 실패한다).

전에는 규칙 정책 업체가 실패한 도구를 거듭 부르다 도구 횟수 상한(TOO_MANY_TOOL_ROUNDS)에 두 번 걸려
모든 대화가 담당자에게 넘어갔다. 실패한 호출도 부른 것으로 보고 다음으로 넘어간다. 가상 데이터다.
"""

from __future__ import annotations

from app.v3 import service
from tests.v3.runtime_fixtures import make_app

EMITTER = "org_no_location"


def test_vendors_answer_when_the_emitter_has_no_location():
    app = make_app()
    app.store.put_org({"id": EMITTER, "name": "(주)위치없음", "role": "emitter", "demo": False})
    service.update_stream(
        app,
        EMITTER,
        "st_nl_pp",
        {
            "name": "PP 스크랩",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "가공 전",
            "monthlyKg": 400,
        },
        EMITTER,
    )
    session_id = service.create_session(app, EMITTER, EMITTER)["session"]["id"]
    view = service.run_session(app, session_id, EMITTER)
    for _ in range(3):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                service.answer_question(app, session_id, question["id"], "모름", EMITTER)
        view = service.run_session(app, session_id, EMITTER)
    assert view["session"]["status"] == "awaiting_approval"
    assert view["threads"] and all(t["status"] != "handoff" for t in view["threads"])
    codes = [
        code
        for entry in app.store.list_activity(session_id, 0)
        for code in entry.get("codes") or []
    ]
    assert "TOO_MANY_TOOL_ROUNDS" not in codes
