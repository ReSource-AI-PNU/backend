"""자리표시자 렌더링. 문서 내보내기에는 render_text, 화면에는 split_text를 쓴다."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.v3.a2a.message import PLACEHOLDER, Claim

MISSING = "확인 필요"
# 정수로만 보여 주는 단위
INTEGER_UNITS = frozenset({"원", "원/월", "원/회"})
# 숫자에 붙여 쓰는 단위. 나머지는 한 칸 띄운다.
_ATTACHED_UNITS = frozenset({"%", "일", "개", "회/월"})
_MAX_DECIMALS = 4


def format_number(value: float, unit: str | None = None) -> str:
    """천 단위 쉼표를 넣고 소수는 필요한 자리까지만 쓴다. 원 단위는 정수로 반올림한다."""
    number = Decimal(str(value))
    places = 0 if unit in INTEGER_UNITS else _MAX_DECIMALS
    number = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{number:,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_value(value: Any, unit: str | None = None) -> str:
    """값과 단위를 한국어 표기로 붙인다. 값이 없으면 '확인 필요'."""
    if value is None:
        return MISSING
    if isinstance(value, str):
        return value
    number = format_number(value, unit)
    if not unit:
        return number
    if unit.startswith("원") or unit in _ATTACHED_UNITS:
        return f"{number}{unit}"
    return f"{number} {unit}"


def format_claim(claim: Claim | dict[str, Any]) -> str:
    item = _as_claim(claim)
    value = item.value
    if item.role == "treatmentFee" and isinstance(value, (int, float)) and value < 0:
        value = -value  # 처리비는 "처리비 80원/kg"처럼 크기로 적는다(값 자체는 음수 그대로다)
    return format_value(value, item.unit if item.kind == "number" else None)


def render_text(text: str, claims: Sequence[Claim | dict[str, Any]]) -> str:
    """자리표시자를 값과 단위로 바꾼 문장. 맞는 claim이 없으면 '확인 필요'를 넣는다."""
    by_id = {c.id: c for c in map(_as_claim, claims)}

    def replace(match):
        claim = by_id.get(match.group(1))
        return MISSING if claim is None else format_claim(claim)

    return PLACEHOLDER.sub(replace, text)


def split_text(text: str, claims: Sequence[Claim | dict[str, Any]]) -> list[dict[str, Any]]:
    """본문을 글자 조각과 claim 조각으로 나눈다. 화면은 claim 조각을 눌러 근거 패널을 연다."""
    by_id = {c.id: c for c in map(_as_claim, claims)}
    parts: list[dict[str, Any]] = []
    cursor = 0
    for match in PLACEHOLDER.finditer(text):
        if match.start() > cursor:
            parts.append({"type": "text", "text": text[cursor : match.start()]})
        claim = by_id.get(match.group(1))
        if claim is None:
            parts.append(
                {"type": "claim", "claimId": match.group(1), "missing": True, "display": MISSING}
            )
        else:
            parts.append(
                {
                    "type": "claim",
                    "claimId": claim.id,
                    "kind": claim.kind,
                    "value": claim.value,
                    "unit": claim.unit,
                    "evidence": list(claim.evidence),
                    "missing": False,
                    "display": format_claim(claim),
                }
            )
        cursor = match.end()
    if cursor < len(text):
        parts.append({"type": "text", "text": text[cursor:]})
    return parts


def _as_claim(claim: Claim | dict[str, Any]) -> Claim:
    return claim if isinstance(claim, Claim) else Claim.from_dict(claim)
