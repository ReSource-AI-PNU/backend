"""질문 묶음의 머리 문장은 실제 묶음과 맞는다. QA에서 "3가지만 여쭤볼게요" 뒤에 넷째 질문(둘째 묶음)이 왔다.
가상 데이터다."""

from __future__ import annotations

from app.v3 import service
from tests.v3.runtime_fixtures import HANBIT, make_app, start


def test_question_batch_intro_matches_the_batch():
    app = make_app()
    view = start(app)
    assert view["session"]["status"] == "awaiting_answers"
    first = view["questionBatch"]
    waiting = [q for q in view["questions"] if not q["answered"]]
    assert first["batch"] == 1 and first["count"] == len(waiting) <= first["maxPerBatch"] == 3
    assert first["intro"] == f"찾기 전에 {first['count']}가지를 여쭙겠습니다."
    assert "만" not in first["intro"] and first["note"] and first["last"] is False
    session_id = view["session"]["id"]
    for question in waiting[:1]:
        service.answer_question(app, session_id, question["id"], "모름", HANBIT)
    after = service.get_view(app, session_id, HANBIT)["questionBatch"]
    assert after["count"] == first["count"] and after["open"] == first["count"] - 1
    for question in waiting[1:]:
        service.answer_question(app, session_id, question["id"], "모름", HANBIT)
    view = service.run_session(app, session_id, HANBIT)
    second = view["questionBatch"]  # 시연 데이터에서는 둘째 묶음(2개)이 온다. 마지막이라고 밝힌다
    assert second["batch"] == 2 and second["last"] is True and second["note"] is None
    assert second["intro"] == f"{second['count']}가지만 더 여쭙겠습니다. 이번이 마지막 질문입니다."
