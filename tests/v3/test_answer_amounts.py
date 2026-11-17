"""담당자 수치 답 읽기(agents/amounts.py, questions.parse_answer). 수치는 가상이다.

평가 L4 변형(test-v1)에서 "2 퍼센트", "2프로 정도", "650 킬로", "대략 650kg"처럼 적은 답이 숫자로 읽히지
않아 실행이 멈췄다(변형 8건). 시험셋의 문구를 옮기지 않고 같은 꼴을 넓게 본다.
"""

from __future__ import annotations

import re

import pytest

from app.v3.a2a.policy import UNKNOWN
from app.v3.agents.amounts import read_amount
from app.v3.agents.questions import parse_answer


@pytest.mark.parametrize(
    ("text", "unit", "expected"),
    [
        ("4 퍼센트", "%", 4.0),
        ("4퍼센트", "%", 4.0),
        ("4%", "%", 4.0),
        ("4 %", "%", 4.0),
        ("４％", "%", 4.0),  # 전각
        ("약 4%", "%", 4.0),
        ("대략 0.7프로", "%", 0.7),
        ("0.7프로 정도요", "%", 0.7),
        ("4 percent", "%", 4.0),
        (".5%", "%", 0.5),
        ("820 킬로", "kg/월", 820.0),
        ("820kg", "kg/월", 820.0),
        ("820 kgs", "kg/월", 820.0),
        ("820키로", "kg/월", 820.0),
        ("0.82톤", "kg/월", 820.0),
        ("0.82 t", "kg/월", 820.0),
        ("1.2만 kg", "kg/월", 12000.0),
        ("820,000 g", "kg/월", 820.0),
        ("월 820kg쯤", "kg/월", 820.0),
        ("한 달에 820kg 정도 나와요", "kg/월", 820.0),
        ("1년에 9,840kg", "kg/월", 820.0),
        ("연간 9.84톤", "kg/월", 820.0),
        ("820kg/월", "kg/월", 820.0),
        ("1,200", "kg/월", 1200.0),
        ("kg당 180원", "원/kg", 180.0),
        ("180원/kg", "원/kg", 180.0),
        ("톤당 18만원", "원/kg", 180.0),
        ("180,000원/t", "원/kg", 180.0),
        ("180", "원/kg", 180.0),
        (7, "%", 7.0),
        (2.5, "kg/월", 2.5),
    ],
)
def test_common_korean_and_english_unit_forms_are_read_in_the_field_unit(text, unit, expected):
    assert read_amount(text, unit) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "unit", "says"),
    [
        ("1~2%", "%", "값 하나로"),
        ("1-2%", "%", "값 하나로"),
        ("2 또는 3", "%", "값 하나로"),
        ("2,3%", "%", "값 하나로"),
        ("4% 이하", "%", "범위가 아니라"),
        ("4%가 안 돼요", "%", "범위가 아니라"),
        ("820kg 넘게", "kg/월", "범위가 아니라"),
        ("최대 820kg", "kg/월", "범위가 아니라"),
        ("4kg", "%", "%(퍼센트)로"),
        ("180원", "%", "%(퍼센트)로"),
        ("4%", "kg/월", "한 달 무게(kg)로"),
        ("하루 40kg", "kg/월", "한 달 기준"),
        ("주에 200kg", "kg/월", "한 달 기준"),
        ("매주 200kg", "kg/월", "한 달 기준"),
        ("820kg", "원/kg", "kg당 금액(원)으로"),
        ("많음", "%", "숫자로 답해"),
        ("네 퍼센트", "%", "숫자로 답해"),
        (True, "%", "숫자로 답해"),
    ],
)
def test_ambiguous_or_mismatched_answers_are_refused_with_a_readable_message(text, unit, says):
    with pytest.raises(ValueError, match=re.escape(says)):
        read_amount(text, unit, "수분")


def test_parse_answer_uses_the_field_unit_and_keeps_its_limits():
    assert parse_answer("moisturePct", "1.5 퍼센트") == 1.5
    assert parse_answer("contaminationPct", "약 3%") == 3.0
    assert parse_answer("monthlyKg", "0.65톤") == 650.0
    assert parse_answer("monthlyKg", "대략 650kg") == 650.0
    assert parse_answer("currentCost", "톤당 25만원") == 250.0
    for unknown in ("모름", "몰라요", "잘 모르겠어요", "unknown", "모르겠습니다"):
        assert parse_answer("moisturePct", unknown) == UNKNOWN
    with pytest.raises(ValueError, match="0에서 100"):
        parse_answer("moisturePct", "150 퍼센트")
    with pytest.raises(ValueError, match="0 이상"):
        parse_answer("monthlyKg", "-3kg")
    with pytest.raises(ValueError, match="월 발생량은 한 달 기준"):
        parse_answer("monthlyKg", "하루 30kg")
    with pytest.raises(ValueError, match="비어"):
        parse_answer("moisturePct", "  ")
    assert parse_answer("form", "압축") == "압축"
