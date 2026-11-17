"""업체 가드레일. 업체 측 에이전트는 이 범위 밖의 조건을 내보낼 수 없다.

단가는 계약 6장의 부호를 따른다: unitPriceKrwPerKg가 양수면 업체가 배출 기업에 주는 매입가,
음수면 배출 기업이 내는 처리비다.

- floorPriceKrwPerKg: 업체가 제시할 수 있는 단가의 하한. 이보다 낮은 단가는 내보내지 않는다.
- ceilingPriceKrwPerKg(선택, 계약에 없는 확장): 단가의 상한. 협상에서 이보다 높은 매입가나
  이보다 낮은 처리비로 끌려가지 않게 한다.
- days: 수거할 수 있는 요일. 제안한 요일은 모두 이 안에 있어야 한다.
- minPickupsPerMonth: 월 최소 수거 횟수.
- autoDecline: 자동 거절 조건. 흐름이나 조건 문구에 이 낱말이 있으면 거절한다.

숫자 없이 조건을 뜻하는 낱말("무상", "주말", "전량" 등)은 check_term_words가 본다. 낱말 목록은
term_words.py에 있고, 해당하는 terms 항목이나 claim이 없으면 UNBACKED_TERM이다. 이 검사는 업체가
정한 값과 관계없이 업체 측과 회사 측 메시지 모두에 건다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.v3.a2a import term_words as words
from app.v3.a2a.message import Message
from app.v3.textutil import josa

ENFORCED_TYPES = ("offer", "counter", "conditional")
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


@dataclass(frozen=True)
class Violation:
    code: str
    message: str  # 한국어 한 문장. 에이전트에게 돌려줘 다시 쓰게 한다
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class GuardrailViolation(ValueError):
    """가드레일 밖의 조건이라 메시지를 내보낼 수 없다."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = list(violations)
        super().__init__(" / ".join(v.message for v in violations))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _norm(text: Any) -> str:
    return "".join(str(text).split()).lower()


def check_terms(guardrails: dict[str, Any] | None, terms: dict[str, Any] | None) -> list[Violation]:
    """terms: {"unitPriceKrwPerKg", "days", "pickupsPerMonth", "conditions"}. 없는 항목은 보지 않는다."""
    guardrails = guardrails or {}
    terms = terms or {}
    found: list[Violation] = []

    price = _number(terms.get("unitPriceKrwPerKg"))
    floor = _number(guardrails.get("floorPriceKrwPerKg"))
    ceiling = _number(guardrails.get("ceilingPriceKrwPerKg"))
    if price is not None and floor is not None and price < floor:
        found.append(
            Violation(
                "PRICE_BELOW_FLOOR",
                "제시한 단가가 업체가 정한 하한 단가보다 낮습니다.",
                {"unitPriceKrwPerKg": price, "floorPriceKrwPerKg": floor},
            )
        )
    if price is not None and ceiling is not None and price > ceiling:
        found.append(
            Violation(
                "PRICE_ABOVE_CEILING",
                "제시한 단가가 업체가 정한 상한 단가보다 높습니다.",
                {"unitPriceKrwPerKg": price, "ceilingPriceKrwPerKg": ceiling},
            )
        )

    allowed_days = guardrails.get("days")
    days = terms.get("days")
    if allowed_days and days:
        outside = [d for d in days if d not in allowed_days]
        if outside:
            found.append(
                Violation(
                    "DAY_NOT_ALLOWED",
                    "업체가 수거할 수 없는 요일이 들어 있습니다.",
                    {"days": list(days), "allowedDays": list(allowed_days), "outside": outside},
                )
            )

    minimum = _number(guardrails.get("minPickupsPerMonth"))
    pickups = _number(terms.get("pickupsPerMonth"))
    if minimum is not None and pickups is not None and pickups < minimum:
        found.append(
            Violation(
                "PICKUPS_BELOW_MINIMUM",
                "월 수거 횟수가 업체가 정한 최소 횟수보다 적습니다.",
                {"pickupsPerMonth": pickups, "minPickupsPerMonth": minimum},
            )
        )

    conditions = terms.get("conditions") or []
    if isinstance(conditions, str):
        conditions = [conditions]
    for keyword in guardrails.get("autoDecline") or []:
        hits = [c for c in conditions if _norm(keyword) in _norm(c)]
        if hits:
            found.append(
                Violation(
                    "AUTO_DECLINE_CONDITION",
                    f"자동 거절 조건({keyword})에 해당하는 내용이 조건에 들어 있습니다.",
                    {"keyword": keyword, "conditions": hits},
                )
            )
    return found


def _stream_texts(stream: dict[str, Any]) -> list[str]:
    texts = [stream.get(k) for k in ("material", "name", "wasteItem", "form", "notes")]
    for item in stream.get("contaminants") or []:
        texts.append(item.get("name") if isinstance(item, dict) else item)
    return [str(t) for t in texts if t]


def auto_decline_reason(guardrails: dict[str, Any] | None, stream: dict[str, Any]) -> str | None:
    """흐름이 자동 거절 조건에 걸리면 이유 문장을, 아니면 None을 돌려준다."""
    texts = _stream_texts(stream)
    for keyword in (guardrails or {}).get("autoDecline") or []:
        if any(_norm(keyword) in _norm(text) for text in texts):
            name = stream.get("name") or stream.get("id") or "이 폐기물"
            return f"{josa(name, '은는')} 업체의 자동 거절 조건({keyword})에 해당합니다."
    return None


def check_term_words(
    message: Message,
    terms: dict[str, Any] | None = None,
    names: Iterable[Any] = (),
) -> list[Violation]:
    """숫자 없이 조건을 뜻하는 낱말("무상", "주말", "전량" 등)에 해당하는 terms 항목이나 claim이 있는지.

    없으면 UNBACKED_TERM. 본문과, 본문 밖에 실려 화면으로 나가는 문장(이유, 조건 문장)을 함께 본다.
    업체 측과 회사 측 메시지에 똑같이 건다. terms를 주지 않으면 message.meta["terms"]를 본다
    (회사 측 메시지에는 terms가 없으므로 claim으로만 뒷받침할 수 있다). names는 조직·흐름 이름이다.
    """
    terms = terms if terms is not None else message.meta.get("terms")
    terms = terms if isinstance(terms, dict) else {}
    text = "\n".join([message.text or "", *[found for _, found in message.free_texts()]])
    for name in sorted({str(n) for n in names if n}, key=len, reverse=True):
        text = text.replace(name, " ")  # 이름에 든 낱말은 조건이 아니다
    days = [d for d in terms.get("days") or [] if d in WEEKDAYS]
    prices = [
        _number(item.get("unitPriceKrwPerKg"))
        for item in terms.get("items") or []
        if isinstance(item, dict)
    ]
    prices += [
        _number(claim.value)
        for claim in message.claims
        if claim.kind == "number" and claim.unit in words.PRICE_UNITS
    ]
    has_quantity = any(
        claim.kind == "number"
        and claim.unit in words.QUANTITY_UNITS
        and _number(claim.value) is not None
        for claim in message.claims
    )
    backed = {
        words.ZERO_PRICE: any(price == 0 for price in prices if price is not None),
        words.WEEKEND_DAYS: any(day in words.WEEKEND for day in days),
        words.EVERY_DAY: set(days) == set(WEEKDAYS),
        words.QUANTITY: has_quantity,
        words.NONE: False,
    }
    found: list[Violation] = []
    for word in words.TERM_WORDS:
        hit = word.pattern.search(text)
        if hit is None or backed[word.needs]:
            continue
        found.append(
            Violation(
                words.CODE,
                f"'{hit.group(0)}'처럼 숫자 없이 조건을 뜻하는 낱말이 있는데 그에 해당하는 terms "
                f"항목이나 claim이 없습니다. {word.hint}",
                {"word": hit.group(0), "key": word.key, "needs": word.needs},
            )
        )
    return found


def enforce(
    guardrails: dict[str, Any] | None,
    message: Message,
    terms: dict[str, Any] | None = None,
) -> Message:
    """offer, counter, conditional이 가드레일 밖이면 GuardrailViolation을 낸다.

    terms를 주지 않으면 message.meta["terms"]를 본다. 조건이 담긴 메시지에 terms가 없으면
    확인할 수 없으므로 내보내지 않는다.
    """
    if message.type not in ENFORCED_TYPES:
        return message
    terms = terms if terms is not None else message.meta.get("terms")
    if not isinstance(terms, dict):
        raise GuardrailViolation(
            [Violation("TERMS_MISSING", "조건(terms)이 없어 가드레일을 확인할 수 없습니다.")]
        )
    violations = check_terms(guardrails, terms)
    if violations:
        raise GuardrailViolation(violations)
    return message
