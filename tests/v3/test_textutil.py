"""한국어 조사 도우미(app/v3/textutil.py)와, 코드가 만든 문장에 "은(는)" 같은 표기가 없는지."""

from __future__ import annotations

import json
import re

import pytest

from app.v3 import service
from app.v3.a2a import guardrails as rails
from app.v3.agents import questions
from app.v3.judge.common import subject
from app.v3.textutil import josa, particle
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    approve_all,
    make_app,
    to_approval,
)

BOTH_FORMS = re.compile(
    r"은\(는\)|는\(은\)|이\(가\)|가\(이\)|을\(를\)|를\(을\)|와\(과\)|과\(와\)|\(으\)로"
)


@pytest.mark.parametrize(
    ("word", "pair", "expected"),
    [
        ("(주)미소환경", "와과", "(주)미소환경과"),
        ("(주)새길자원", "와과", "(주)새길자원과"),
        ("(주)남해열분해", "와과", "(주)남해열분해와"),
        ("수분", "이가", "수분이"),
        ("오염도", "이가", "오염도가"),
        ("수분", "은는", "수분은"),
        ("오염도", "은는", "오염도는"),
        ("퍼지 덩어리", "을를", "퍼지 덩어리를"),
        ("포장 필름", "을를", "포장 필름을"),
        ("서울", "으로", "서울로"),  # ㄹ 받침은 "로"
        ("부산", "으로", "부산으로"),
        ("창고", "으로", "창고로"),
    ],
)
def test_hangul_final_consonant_picks_the_particle(word, pair, expected):
    assert josa(word, pair) == expected


@pytest.mark.parametrize(
    ("word", "pair", "expected"),
    [
        # 닫는 괄호로 끝나면 괄호 앞의 말을 본다
        ("출처(PIR·PCR)", "이가", "출처(PIR·PCR)가"),
        ("한빛정밀(주)", "은는", "한빛정밀(주)은"),
        ("포장 필름(포장재·LDPE)", "이가", "포장 필름(포장재·LDPE)이"),
        # 말 전체가 괄호나 따옴표 안이면 안쪽을 본다
        ("(수분)", "이가", "(수분)이"),
        ("'스프루·런너'", "을를", "'스프루·런너'를"),
        ("“불량 사출품”", "은는", "“불량 사출품”은"),
        ('"오염도"', "이가", '"오염도"가'),
        ("{{c1}}", "을를", "{{c1}}을"),  # 1(일)
    ],
)
def test_brackets_and_quotes_are_looked_through(word, pair, expected):
    assert josa(word, pair) == expected


@pytest.mark.parametrize(
    ("word", "pair", "expected"),
    [
        ("PP", "은는", "PP는"),  # 피
        ("ABS", "이가", "ABS가"),  # 에스
        ("PVC", "을를", "PVC를"),  # 씨
        ("PP+ABS", "와과", "PP+ABS와"),
        ("HDPL", "은는", "HDPL은"),  # 엘
        ("KRM", "이가", "KRM이"),  # 엠
        ("PIR", "으로", "PIR로"),  # 알: ㄹ 받침
        ("LDPE", "으로", "LDPE로"),
        ("film", "은는", "film은"),
        ("offer", "을를", "offer를"),
        ("handoff", "을를", "handoff를"),
        ("st_film", "이가", "st_film이"),
        ("org_saegil", "은는", "org_saegil은"),
        ("th_3", "이가", "th_3이"),  # 삼
        ("th_2", "이가", "th_2가"),  # 이
        ("th_1", "으로", "th_1로"),  # 일: ㄹ 받침
        ("502", "을를", "502를"),
        ("500", "을를", "500을"),  # 오백
        ("404", "으로", "404로"),
        ("10", "으로", "10으로"),  # 십
    ],
)
def test_latin_letters_and_digits_are_read_aloud(word, pair, expected):
    assert josa(word, pair) == expected


@pytest.mark.parametrize(
    ("word", "pair", "expected"),
    [
        ("1,300 kg/월", "은는", "은"),
        ("1,300 kg", "이가", "이"),  # 킬로그램
        ("8%", "은는", "는"),  # 퍼센트
        ("320원/kg", "을를", "을"),
        ("2회/월", "으로", "로"),
        ("0.9 t", "이가", "이"),  # 톤
        ("12 km", "은는", "는"),  # 킬로미터
    ],
)
def test_units_are_read_by_their_sound(word, pair, expected):
    assert particle(word, pair) == expected


def test_pair_spellings_and_unknown_pairs():
    assert particle("수분", "은/는") == "은"
    assert particle("수분", "이(가)") == "이"
    assert particle("오염도", "를") == "를"
    assert particle("오염도", "로") == "로"
    assert particle("", "은는") == "는"
    with pytest.raises(ValueError):
        particle("수분", "에게")


def test_helpers_that_used_both_forms_now_pick_one():
    assert subject("수분") == "수분이"
    assert subject("오염도") == "오염도가"
    reason = rails.auto_decline_reason({"autoDecline": ["PVC"]}, {"name": "PVC 시트", "id": "st_x"})
    assert reason.startswith("PVC 시트는 ")
    question = questions.make_question({"id": "st_x", "name": "포장 필름"}, "origin")
    assert question["text"].startswith("포장 필름은 ")
    question = questions.make_question({"id": "st_x", "name": "퍼지 덩어리"}, "origin")
    assert question["text"].startswith("퍼지 덩어리는 ")


def test_no_both_form_particles_reach_any_view_of_the_demo():
    app = make_app()
    view = approve_all(app, to_approval(app))
    session_id = view["session"]["id"]
    assert view["session"]["status"] == "confirmed"
    for viewer in (HANBIT, SAEGIL, NAMHAE, MISO):
        text = json.dumps(service.get_view(app, session_id, viewer), ensure_ascii=False)
        assert not BOTH_FORMS.search(text), BOTH_FORMS.search(text).group(0)
        assert "(주)미소환경와" not in text and "(주)새길자원와" not in text
        assert "수분가" not in text and "오염도이" not in text
    events = [e["summary"] for e in view["events"]]
    assert any(s.startswith("(주)미소환경과 대화를 엽니다") for s in events)
    assert any(s.startswith("(주)남해열분해와 대화를 엽니다") for s in events)
    closed = [t["statusReason"] for t in view["threads"] if t["status"] == "not_opened"]
    assert closed
    assert all(("가 없어" in reason) or ("이 없어" in reason) for reason in closed)
