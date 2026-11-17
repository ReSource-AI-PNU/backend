"""AI 메시지 문구. 브라우저 QA에서 본 글:

- 모델이 "처리비는 처리비는 60 원/kg"처럼 낱말을 두 번 썼다.
- 업체 측이 "아래 값을 회신해 주시면 매입 기준과 대조하겠습니다."라고만 쓰고 목록이 없었다(묻는 항목은
  meta.asks에만 있었다).
- 이름 뒤 조사: "(주)QA정밀2202" 뒤는 숫자 2(이)를 읽어 "를"이다.
회사와 수치는 모두 가상이다.
"""

from __future__ import annotations

import json

from app.v3.agents import draft as draft_module
from app.v3.agents.pool import ClaimPool
from app.v3.agents.questions import with_ask_lines
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.textutil import drop_repeats, josa
from tests.v3.runtime_fixtures import (
    HANBIT,
    SAEGIL,
    draft,
    make_app,
    only_for,
    rule_fallback,
    scripted,
    to_approval,
)


def test_repeated_words_are_dropped():
    assert drop_repeats("참고로 당사가 이해한 처리비는 처리비는 {{fee}}입니다.") == (
        "참고로 당사가 이해한 처리비는 {{fee}}입니다."
    )
    assert drop_repeats("매입 단가는 {{a}}, 처리비는 {{b}}입니다.") == (
        "매입 단가는 {{a}}, 처리비는 {{b}}입니다."
    )
    assert drop_repeats("- PP 스크랩: 오염도, 수분") == "- PP 스크랩: 오염도, 수분"


def test_built_messages_do_not_repeat_words():
    pool = ClaimPool(InMemoryRegistry(), HANBIT)
    message = draft_module.build_message(
        draft("inquiry", "참고로 당사가 이해한 처리비는 처리비는 확인 필요입니다.", stage=4),
        pool,
        thread_id="th_1",
        sender=HANBIT,
        recipient=SAEGIL,
        allowed_types=("inquiry",),
        stream_ids=(),
    )
    assert message.text == "참고로 당사가 이해한 처리비는 확인 필요입니다."


def test_question_text_lists_what_is_asked():
    asks = [
        {"streamId": "st_a", "field": "contaminationPct"},
        {"streamId": "st_a", "field": "moisturePct"},
        {"streamId": "st_b", "field": "terms"},
    ]
    text = "측정을 요청드립니다. 아래 값을 회신해 주시면 매입 기준과 대조하겠습니다."
    names = {"st_a": "PP 압축품(a)", "st_b": "LDPE 필름(b)"}
    assert with_ask_lines(text, asks, names).splitlines() == [
        text,
        "- PP 압축품(a): 오염도, 수분",
        "- LDPE 필름(b): 받는 조건",
    ]
    listed = "다음 값을 알려 주세요.\n- PP 압축품(a): 수분"
    assert with_ask_lines(listed, asks, names) == listed  # 목록이 있으면 그대로다
    assert "st_" not in with_ask_lines(text, [{"streamId": "st_x", "field": "form"}], {})


def test_a_vendor_question_without_a_list_gets_one():
    """업체 측 모델이 목록 없이 질문해도 상대에게 간 글에는 묻는 항목이 있다."""
    asked = {"done": False}

    def vendor(messages, tools, schema):
        brief = json.loads(messages[-1]["content"])
        if not asked["done"] and "question" in brief["thread"]["allowedTypes"]:
            asked["done"] = True
            sid = brief["thread"]["streamIds"][0]
            return {
                "content": json.dumps(
                    draft(
                        "question",
                        "측정을 요청드립니다. 아래 값을 회신해 주시면 매입 기준과 대조하겠습니다.",
                        stage=4,
                        streams=[sid],
                        asks=[{"streamId": sid, "field": "moisturePct"}],
                    ),
                    ensure_ascii=False,
                )
            }

        return rule_fallback(messages, tools, schema)

    app = make_app(only_for("vendor", SAEGIL, scripted(*[vendor] * 200, pad=0)))
    view = to_approval(app)
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == SAEGIL)
    question = next(m for m in thread["messages"] if m["type"] == "question")
    lines = question["text"].splitlines()
    assert lines[0].startswith("측정을 요청드립니다. 아래 값을")
    assert any(line.startswith("- ") and line.endswith(": 수분") for line in lines[1:])


def test_particles_after_names_ending_in_digits_or_latin():
    assert josa("(주)QA정밀2202", "을를") == "(주)QA정밀2202를"  # 2 = 이
    assert josa("(주)QA정밀2201", "을를") == "(주)QA정밀2201을"  # 1 = 일
    assert josa("(주)QA정밀", "을를") == "(주)QA정밀을"
    assert josa("ABC", "을를") == "ABC를" and josa("(주)PTL", "을를") == "(주)PTL을"
