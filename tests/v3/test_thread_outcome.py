"""실행이 끝나면 대화마다 끝 상태를 실시간 기록과 진단 뷰에 남긴다.

QA에서 실행이 끝난 뒤에도 조건을 받고 배정되지 않은 대화가 "조건을 묻는 중"으로 남아 있었다.
가상 데이터다.
"""

from __future__ import annotations

from app.v3 import service
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app, to_approval


def _outcomes(app, session_id, viewer):
    events = service.stream_events(app, session_id, viewer, 0)["events"]
    found = {}
    for event in events:
        data = event["data"]
        if event["event"] == "activity" and data.get("threadOutcome"):
            found[data["threadId"]] = (data["threadOutcome"], data["threadStatus"], data["text"])
    return found


def test_each_thread_gets_a_final_status_when_the_run_stops():
    app = make_app()
    view = to_approval(app)
    session_id = view["session"]["id"]
    assert view["session"]["status"] == "awaiting_approval"
    opened = [t for t in view["threads"] if t["status"] != "not_opened"]
    final = _outcomes(app, session_id, HANBIT)
    assert set(final) == {t["id"] for t in opened}
    for thread in opened:
        outcome, status, text = final[thread["id"]]
        assert status == thread["status"] and thread["outcome"] == outcome
        assert thread["outcomeText"] == text and "묻는 중" not in text
        if thread["status"] == "open":  # 조건을 받았지만 배정되지 않은 대화
            assert outcome in ("not_selected", "ended")
        else:
            assert outcome == thread["status"]
    # 업체도 자기 대화의 끝 상태를 본다(다른 업체의 것은 보지 않는다)
    mine = _outcomes(app, session_id, SAEGIL)
    saegil = {t["id"] for t in opened if t["vendorOrgId"] == SAEGIL}
    assert set(mine) == saegil
    # 같은 끝 상태는 다시 남기지 않는다
    before = len(app.store.list_activity(session_id, 0))
    service.run_session(app, session_id, HANBIT)
    after = [e for e in app.store.list_activity(session_id, before) if e.get("threadOutcome")]
    assert after == []


def test_threads_waiting_for_answers_say_so():
    app = make_app()
    view = service.create_session(app, HANBIT, HANBIT)
    view = service.run_session(app, view["session"]["id"], HANBIT)
    assert view["session"]["status"] == "awaiting_answers"
    assert all(t["outcome"] in (None, "waiting") for t in view["threads"])
