"""담당자가 글로 적은 수치 답을 읽는다("2 퍼센트", "650 킬로", "0.65톤", "약 2%", "kg당 250원").

    read_amount(value, unit, label="값") -> float  # unit은 질문 항목의 단위(%, kg/월, 원/kg)

- 숫자는 하나여야 한다. "1~2%", "2 또는 3"처럼 둘 이상이면 값 하나로 답해 달라고 한다.
- "약", "대략", "정도", "쯤"처럼 어림을 뜻하는 말은 받아들이고 값 그대로 쓴다(어림값인지는 질문 답이
  담당자 답이라는 근거 종류로 이미 남는다).
- "이하", "이상", "미만", "넘게", "안 돼요"처럼 범위를 뜻하는 말은 값 하나가 아니므로 받지 않는다.
- 단위는 항목의 단위로 바꾼다: 톤 → kg(×1000), 그램 → kg(÷1000), 1년 → 한 달(÷12), 원/톤 → 원/kg(÷1000).
  주·하루처럼 한 달로 바꾸는 방법이 정해지지 않은 기간과 다른 차원의 단위(% 자리에 kg)는 받지 않는다.
- 단위가 없으면 항목의 단위로 본다.

받지 않을 때는 ValueError에 담당자에게 보일 한국어 문장을 담는다.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from app.v3.textutil import josa

NOT_A_NUMBER = "숫자로 답해 주세요. 모르면 '모름'으로 답해 주세요."
_EXAMPLES = {"%": "2%", "kg/월": "650kg", "원/kg": "250원"}
_UNIT_WORDS = {"%": "%(퍼센트)", "kg/월": "한 달 무게(kg)", "원/kg": "kg당 금액(원)"}

# 숫자 하나: 1,200 / 0.65 / .5 / 1.2만. 앞의 빼기표는 따로 본다
_NUMBER = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)?(?:\.(\d+))?(?:\s*(만|천))?")
# "1년에", "한 달에"처럼 기간 앞의 1·한은 숫자로 세지 않는다
_ONE_PERIOD = re.compile(r"(?:(?<![\d.])1|한)\s*(년|달|개월|주일?|주|일|날)")
_BOUND = re.compile(
    r"이하|이상|미만|초과|넘|이내|최대|최소|까지|부터|범위|안\s*(?:되|돼|넘)|못\s*(?:미치|되|돼)|"
    r"\b(?:under|over|below|above|less|more|least|most|max|min|up\s*to)\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"%|퍼센트|퍼센|프로|percent|pct", re.IGNORECASE)
_MONEY = re.compile(r"원|krw|\bwon\b", re.IGNORECASE)
# 무게 단위 → kg 배수(긴 것부터)
_MASS = (
    (re.compile(r"킬로그램|킬로|키로|kilograms?|kilos?|kgs?", re.IGNORECASE), 1.0),
    (re.compile(r"톤|tonnes?|tons?|(?<![a-z])t(?![a-z])", re.IGNORECASE), 1000.0),
    (re.compile(r"그램|grams?|(?<![a-z])g(?![a-z])", re.IGNORECASE), 0.001),
)
_PER_KG = re.compile(r"(?:킬로그램|킬로|키로|kg)\s*당|/\s*(?:kg|킬로|키로)|per\s*kg", re.IGNORECASE)
_PER_TON = re.compile(r"(?:톤|t)\s*당|/\s*(?:t|톤)(?![a-z])|per\s*(?:ton|tonne)", re.IGNORECASE)
_MONTH = re.compile(r"월|달|개월|month", re.IGNORECASE)
_YEAR = re.compile(r"년|연간|(?<![가-힣])연(?![가-힣])|year|annual", re.IGNORECASE)
# 한 달로 바꾸는 방법이 정해지지 않은 기간(주 5일인지 7일인지 모른다)
_OTHER_PERIOD = re.compile(
    r"매주|주당|주간|주에|주마다|주일|/\s*주|하루|매일|일당|날마다|"
    r"(?<![가-힣])(?:주|일)(?![가-힣])|week|\bday|daily",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip()


def _numbers(text: str) -> list[tuple[float, re.Match[str]]]:
    found = []
    for match in _NUMBER.finditer(text):
        whole, fraction, multiplier = match.group(1), match.group(2), match.group(3)
        if whole is None and fraction is None:
            continue
        number = float(f"{(whole or '0').replace(',', '')}.{fraction or '0'}")
        if multiplier:
            number *= 10000 if multiplier == "만" else 1000
        found.append((number, match))
    return found


def _fail(reason: str, unit: str, label: str) -> ValueError:
    example = _EXAMPLES.get(unit, "2")
    if reason == "many":
        return ValueError(
            f"{josa(label, '은는')} 값 하나로 답해 주세요(예: {example}). 범위라면 가장 가까운 값 "
            "하나를 적어 주세요. 모르면 '모름'으로 답해 주세요."
        )
    if reason == "bound":
        return ValueError(
            f"'이하', '이상' 같은 범위가 아니라 {label} 값 하나로 답해 주세요(예: {example})."
        )
    if reason == "period":
        return ValueError(f"{josa(label, '은는')} 한 달 기준으로 답해 주세요(예: {example}).")
    word = josa(_UNIT_WORDS.get(unit, unit), "으로")
    return ValueError(f"{josa(label, '은는')} {word} 답해 주세요(예: {example}).")


def read_amount(value: Any, unit: str | None, label: str = "값") -> float:
    """value를 unit의 수치로 읽는다. 읽을 수 없거나 뜻이 둘 이상이면 ValueError(한국어 문장)."""
    if isinstance(value, bool):
        raise ValueError(NOT_A_NUMBER)  # noqa: TRY004 - 담당자에게 보일 문장으로 받지 않는다
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(NOT_A_NUMBER)
        return number
    text = _ONE_PERIOD.sub(lambda m: m.group(1), _text(value))
    numbers = _numbers(text)
    if not numbers:
        raise ValueError(NOT_A_NUMBER)
    if len(numbers) > 1:
        raise _fail("many", unit or "", label)
    number, match = numbers[0]
    before, rest = text[: match.start()], text[: match.start()] + " " + text[match.end() :]
    if re.search(r"[-−–]\s*$", before):
        number = -number
    if _BOUND.search(rest):
        raise _fail("bound", unit or "", label)
    if unit == "%":
        if _MONEY.search(rest) or any(p.search(rest) for p, _ in _MASS):
            raise _fail("unit", unit, label)
        return number
    if unit == "kg/월":
        if _PERCENT.search(rest) or _MONEY.search(rest):
            raise _fail("unit", unit, label)
        if _OTHER_PERIOD.search(rest):
            raise _fail("period", unit, label)
        for pattern, factor in _MASS:
            if pattern.search(rest):
                number *= factor
                break
        if _YEAR.search(rest) and not _MONTH.search(rest):
            number /= 12
        return number
    if unit == "원/kg":
        if _PERCENT.search(rest):
            raise _fail("unit", unit, label)
        if _PER_TON.search(rest):
            return number / 1000
        if _PER_KG.search(rest):
            return number
        if any(p.search(rest) for p, _ in _MASS):
            raise _fail("unit", unit, label)  # "300kg"은 금액이 아니다
        return number
    return number
