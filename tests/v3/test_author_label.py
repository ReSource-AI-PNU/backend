"""대화 화면의 작성자 표시. 사용자에게는 구현 이름 대신 중립적인 이름(회사 측 AI, 업체 측 AI)을 준다."""

import json

from app.v3 import service
from tests.v3.runtime_fixtures import (
    ADMIN,
    HANBIT,
    SAEGIL,
    draft,
    make_app,
    only_for,
    scripted,
    thread_of,
    to_approval,
)

LABELS = {"회사 측 AI", "업체 측 AI", "자동 안내"}


def test_messages_carry_a_neutral_author_next_to_the_model():
    app = make_app()  # 규칙 정책(모델 미연결)
    view = to_approval(app)
    opened = [t for t in view["threads"] if t["status"] != "not_opened"]
    assert opened
    for thread in opened:
        assert thread["vendorAuthor"] == {"side": "vendor", "kind": "agent", "label": "업체 측 AI"}
        assert thread["vendorModel"]["provider"] == "rule"  # 기존 필드는 그대로 둔다
        for message in thread["messages"]:
            side = "company" if message["from"] == HANBIT else "vendor"
            assert message["author"]["side"] == side
            assert message["author"]["label"] == (
                "회사 측 AI" if side == "company" else "업체 측 AI"
            )
            assert message["model"]["provider"] == "rule"
    shown = json.dumps(
        [[m["author"] for m in t["messages"]] + [t["vendorAuthor"]] for t in opened],
        ensure_ascii=False,
    )
    assert "규칙" not in shown and "모델" not in shown
    # 구현 이름은 관리자 화면에서 본다
    status = service.admin_status(app, ADMIN)
    assert status["models"]["company"]["name"] == "규칙 정책(모델 미연결)"


def test_code_written_notices_are_labelled_as_automatic():
    bad = draft(
        "offer",
        "스프루·런너는 kg당 320원에 받겠습니다.",
        streams=["st_sprue"],
        terms={"items": [{"streamId": "st_sprue", "unitPriceRef": "pc_saegil_pp_a_unitPrice"}]},
    )
    app = make_app(only_for("vendor", SAEGIL, scripted(bad, bad)))
    thread = thread_of(to_approval(app), SAEGIL)
    handoff = thread["messages"][-1]
    assert handoff["type"] == "handoff" and handoff["model"]["provider"] == "code"
    assert handoff["author"] == {"side": "vendor", "kind": "system", "label": "자동 안내"}
    assert {m["author"]["label"] for m in thread["messages"]} <= LABELS
