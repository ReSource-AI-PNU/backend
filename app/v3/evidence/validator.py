"""근거 검증기. 메시지나 문서가 나가기 전에 본문과 claims를 근거 레지스트리와 대조한다."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.v3.a2a.message import PLACEHOLDER, Claim, Message
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, EvidenceRegistry
from app.v3.evidence.units import UnitError, same_value
from app.v3.textutil import josa

NUMBER_OUTSIDE_PLACEHOLDER = "NUMBER_OUTSIDE_PLACEHOLDER"
UNKNOWN_PLACEHOLDER = "UNKNOWN_PLACEHOLDER"
UNUSED_CLAIM = "UNUSED_CLAIM"
CLAIM_WITHOUT_EVIDENCE = "CLAIM_WITHOUT_EVIDENCE"
EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
EVIDENCE_FORBIDDEN = "EVIDENCE_FORBIDDEN"
EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
VALUE_MISMATCH = "VALUE_MISMATCH"
UNIT_MISMATCH = "UNIT_MISMATCH"
QUOTE_MISMATCH = "QUOTE_MISMATCH"

CODES = (
    NUMBER_OUTSIDE_PLACEHOLDER,
    UNKNOWN_PLACEHOLDER,
    UNUSED_CLAIM,
    CLAIM_WITHOUT_EVIDENCE,
    EVIDENCE_NOT_FOUND,
    EVIDENCE_FORBIDDEN,
    EVIDENCE_EXPIRED,
    VALUE_MISMATCH,
    UNIT_MISMATCH,
    QUOTE_MISMATCH,
)

# 숫자가 들어 있지만 수치가 아닌 낱말. 재질, 단위, 기술 이름만 둔다.
ALLOWED_TOKENS: tuple[str, ...] = (
    "CO2",
    "CO2e",
    "tCO2e",
    "kgCO2e",
    "m3",
    "PA6",
    "PA66",
    "PA12",
    "PM10",
    "PM2.5",
    "A2A",
    "bge-m3",
)
# 식별자 머리말. 이 뒤에 오는 영문·숫자·밑줄은 식별자의 일부로 본다.
IDENTIFIER_PREFIXES: tuple[str, ...] = ("st", "org", "th", "doc", "ses", "msg", "q")

_FULLWIDTH = str.maketrans("０１２３４５６７８９．，％－", "0123456789.,%-")
_YEAR = r"(?:19|20)\d{2}"
_NO_DIGIT_BEFORE = r"(?<![\d.])"
_NO_DIGIT_AFTER = r"(?!\d)"
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 2026-11-16, 2026/11/16
    re.compile(rf"{_NO_DIGIT_BEFORE}{_YEAR}[-/]\d{{1,2}}[-/]\d{{1,2}}{_NO_DIGIT_AFTER}"),
    # 2026.11.16 또는 2026. 11. 16.
    re.compile(rf"{_NO_DIGIT_BEFORE}{_YEAR}\.\s?\d{{1,2}}\.\s?\d{{1,2}}{_NO_DIGIT_AFTER}\.?"),
    # 2026-08 (기준월)
    re.compile(rf"{_NO_DIGIT_BEFORE}{_YEAR}-(?:0[1-9]|1[0-2]){_NO_DIGIT_AFTER}(?!-)"),
    # 2026년, 2026년 11월, 2026년 11월 16일
    re.compile(rf"{_NO_DIGIT_BEFORE}{_YEAR}년(?:\s*\d{{1,2}}월)?(?:\s*\d{{1,2}}일)?"),
    # 11월, 11월 16일 (3개월은 날짜가 아니다)
    re.compile(rf"{_NO_DIGIT_BEFORE}(?:1[0-2]|0?[1-9])월(?:\s*(?:[12]\d|3[01]|0?[1-9])일)?"),
)
_STAGE = re.compile(rf"{_NO_DIGIT_BEFORE}[1-6]단계")
_LIST_NUMBER = re.compile(r"(?m)^[ \t]*\d{1,2}[.)](?=[ \t])")
_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(IDENTIFIER_PREFIXES) + r")_[A-Za-z0-9][A-Za-z0-9_]*"
)
_NUMBER = re.compile(r"[-−+]?\d+(?:,\d{3})*(?:\.\d+)?\s?%?")
# 식별자 바로 뒤에 단위가 붙으면 식별자가 아니라 숫자를 숨긴 것으로 본다 (예: st_320원)
_UNIT_AFTER = re.compile(r"\s?(?:원|kg|킬로|톤|t(?![A-Za-z])|%|퍼센트|회|개|건|km|m3)")
_DIGITS = re.compile(r"\d+")
# 한글로 적은 수: 십·백·천·만·억이 들었거나 두 글자 이상이고, 바로 뒤에 단위가 온다 (예: 삼백이십 원)
_HANGUL_NUMBER = re.compile(
    r"(?<![가-힣])(?=[일이삼사오육칠팔구십백천만억]*[십백천만억])[일이삼사오육칠팔구십백천만억]+"
    r"\s?(?:원|킬로그램|킬로|kg|톤|퍼센트|%|회)"
    r"(?=$|[\s.,;:!?)\]]|[이가을를은는의에도로과와씩당]|입니다|이며|이고)"
)
# 이름 안의 "숫자+단위". 이런 이름은 허용 낱말로 쓰지 않는다
_NAME_WITH_AMOUNT = re.compile(r"\d\s?(?:원|kg|킬로|톤|t(?![A-Za-z])|%|퍼센트|회|개|건|km|m3)")
# 인용은 이 길이 이상이어야 한다. 짧은 조각(숫자 한 토막 등)을 인용으로 내보내는 것을 막는다.
MIN_QUOTE_CHARS = 10


@dataclass(frozen=True)
class Rejection:
    code: str
    message: str
    claim_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "claimId": self.claim_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    rejections: tuple[Rejection, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "rejections": [r.to_dict() for r in self.rejections]}

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.rejections)


def find_numbers(text: str, *, allowed_tokens: Iterable[str] = ALLOWED_TOKENS) -> list[str]:
    """자리표시자와 허용 예외를 뺀 본문에 남은 숫자 표기를 나온 순서대로 돌려준다."""
    work = text.translate(_FULLWIDTH)
    work = _mask(PLACEHOLDER, work)
    work = _IDENTIFIER.sub(_mask_identifier, work)
    for pattern in _DATE_PATTERNS:
        work = pattern.sub(_mask_date, work)
    work = _mask(_STAGE, work)
    work = _mask(_LIST_NUMBER, work)
    for token in sorted(set(allowed_tokens), key=len, reverse=True):
        work = _mask(_token_pattern(token), work)
    found = [(m.start(), m.group(0).strip()) for m in _NUMBER.finditer(work)]
    found += [(m.start(), m.group(0).strip()) for m in _HANGUL_NUMBER.finditer(work)]
    return [token for _, token in sorted(found)]


def validate_text(
    text: str,
    claims: Sequence[Claim | dict[str, Any]],
    registry: EvidenceRegistry,
    *,
    org_id: str | None,
    today: str,
    allowed_tokens: Iterable[str] = ALLOWED_TOKENS,
) -> ValidationResult:
    """본문과 claims를 검사한다. org_id는 보내는 조직이고 today는 YYYY-MM-DD다."""
    items = [c if isinstance(c, Claim) else Claim.from_dict(c) for c in claims]
    rejections: list[Rejection] = []

    numbers = find_numbers(text, allowed_tokens=allowed_tokens)
    if numbers:
        shown = ", ".join(numbers[:8])
        rejections.append(
            Rejection(
                NUMBER_OUTSIDE_PLACEHOLDER,
                f"본문에 자리표시자 밖 숫자({shown})가 있습니다. 숫자는 {{{{claimId}}}} "
                "자리표시자로 바꾸고 claims에 값과 근거 ID를 넣으세요.",
                detail={"found": numbers},
            )
        )

    used = [m.group(1) for m in PLACEHOLDER.finditer(text)]
    claim_ids = {c.id for c in items}
    for name in dict.fromkeys(used):
        if name not in claim_ids:
            rejections.append(
                Rejection(
                    UNKNOWN_PLACEHOLDER,
                    f"본문의 자리표시자 {{{{{name}}}}}에 맞는 claim이 없습니다. "
                    "claims에 같은 id를 넣거나 자리표시자를 지우세요.",
                    claim_id=name,
                )
            )
    used_set = set(used)
    for claim in items:
        if claim.id not in used_set:
            rejections.append(
                Rejection(
                    UNUSED_CLAIM,
                    f"claim '{claim.id}'가 본문에 쓰이지 않았습니다. 본문에 "
                    f"{josa('{{' + claim.id + '}}', '을를')} 넣거나 claim을 지우세요.",
                    claim_id=claim.id,
                )
            )

    for claim in items:
        rejections.extend(_check_claim(claim, registry, org_id=org_id, today=today))

    return ValidationResult(ok=not rejections, rejections=tuple(rejections))


def name_tokens(names: Iterable[Any]) -> tuple[str, ...]:
    """이름(조직, 흐름)에 든 숫자를 수치로 보지 않게 하는 허용 낱말.

    이름은 코드가 저장소에서 읽어 넘긴다(모델이 정하지 못한다). 숫자만으로 된 이름과 수치에
    단위가 붙은 이름("단가 999원 보장")은 수치와 구분할 수 없어 허용하지 않는다.
    """
    out = []
    for name in names:
        text = str(name or "").strip()
        if not any(ch.isdigit() for ch in text) or not re.search(r"[^\d\s.,%+\-−]", text):
            continue
        if _NAME_WITH_AMOUNT.search(text.translate(_FULLWIDTH)):
            # "단가 999원 보장"처럼 수치와 단위가 붙은 이름은 값과 구분할 수 없다. 허용하지 않는다
            continue
        out.append(text)
    return tuple(dict.fromkeys(out))


def validate_message(
    message: Message, registry: EvidenceRegistry, *, today: str, names: Iterable[Any] = ()
) -> ValidationResult:
    """보내는 조직(message.sender)의 권한으로 메시지를 검사한다.

    names는 이 대화에 나오는 조직과 흐름의 이름이다. "(주)제2공장", "PP 2차 분쇄품"처럼 이름에
    든 숫자는 자리표시자 밖 숫자로 세지 않는다.
    """
    allowed = (*ALLOWED_TOKENS, *name_tokens(names))
    result = validate_text(
        message.text,
        message.claims,
        registry,
        org_id=message.sender,
        today=today,
        allowed_tokens=allowed,
    )
    extra = validate_notes(message, allowed_tokens=allowed)
    if not extra:
        return result
    return ValidationResult(ok=False, rejections=(*result.rejections, *extra))


_NOTE_LABELS = {
    "reason": "이유(reason)",
    "excluded": "받지 않는 폐기물의 이유(excluded)",
    "terms.conditions": "조건 문장(terms.conditions)",
    "terms.days": "수거 요일(terms.days)",
}


def validate_notes(
    message: Message, *, allowed_tokens: Iterable[str] = ALLOWED_TOKENS
) -> list[Rejection]:
    """본문 밖에 실린 문장(닫는 이유, 받지 않는 이유, 조건 문장, 요일)도 본문과 같이 검사한다.

    이 문장들도 화면과 문서에 그대로 나간다. 자리표시자 밖 숫자가 없어야 하고, 자리표시자는
    이 메시지의 claims(본문에 쓰여 검증된 것)만 가리켜야 한다.
    """
    claim_ids = {c.id for c in message.claims}
    out: list[Rejection] = []
    for where, text in message.free_texts():
        label = _NOTE_LABELS.get(where, where)
        numbers = find_numbers(text, allowed_tokens=allowed_tokens)
        if numbers:
            shown = ", ".join(numbers[:8])
            out.append(
                Rejection(
                    NUMBER_OUTSIDE_PLACEHOLDER,
                    f"{label}에 자리표시자 밖 숫자({shown})가 있습니다. 숫자는 본문에 쓴 "
                    "{{claimId}} 자리표시자로만 적으세요.",
                    detail={"found": numbers, "where": where},
                )
            )
        for name in dict.fromkeys(m.group(1) for m in PLACEHOLDER.finditer(text)):
            if name not in claim_ids:
                out.append(
                    Rejection(
                        UNKNOWN_PLACEHOLDER,
                        f"{label}의 자리표시자 {{{{{name}}}}}에 맞는 claim이 이 메시지에 없습니다. "
                        "본문에 쓴 claim만 가리키세요.",
                        claim_id=name,
                        detail={"where": where},
                    )
                )
    return out


def _check_claim(
    claim: Claim, registry: EvidenceRegistry, *, org_id: str | None, today: str
) -> list[Rejection]:
    if not claim.evidence:
        return [
            Rejection(
                CLAIM_WITHOUT_EVIDENCE,
                f"claim '{claim.id}'에 근거 ID가 없습니다. 근거가 없는 값은 쓰지 말고 "
                "'확인 필요'로 두세요.",
                claim_id=claim.id,
            )
        ]
    out: list[Rejection] = []
    primary: Evidence | None = None
    for index, evidence_id in enumerate(claim.evidence):
        try:
            evidence = registry.get(evidence_id, org_id)
        except EvidenceNotFound:
            out.append(
                Rejection(
                    EVIDENCE_NOT_FOUND,
                    f"claim '{claim.id}'의 근거 '{evidence_id}'가 근거 저장소에 없습니다. "
                    "검색 도구가 돌려준 근거 ID만 쓰세요.",
                    claim_id=claim.id,
                    detail={"evidenceId": evidence_id},
                )
            )
            continue
        except EvidenceForbidden:
            out.append(
                Rejection(
                    EVIDENCE_FORBIDDEN,
                    f"claim '{claim.id}'의 근거 '{evidence_id}'는 보내는 조직이 볼 수 없는 "
                    "근거입니다. 이 조직이 접근할 수 있는 근거로 바꾸세요.",
                    claim_id=claim.id,
                    detail={"evidenceId": evidence_id},
                )
            )
            continue
        if evidence.expired(today):
            out.append(
                Rejection(
                    EVIDENCE_EXPIRED,
                    f"claim '{claim.id}'의 근거 '{evidence_id}'는 유효 기간"
                    f"({evidence.valid_until})이 지났습니다. 최신 기준의 근거로 바꾸세요.",
                    claim_id=claim.id,
                    detail={"evidenceId": evidence_id, "validUntil": evidence.valid_until},
                )
            )
        if index == 0:
            primary = evidence
    if primary is not None:
        mismatch = (
            _check_number(claim, primary)
            if claim.kind == "number"
            else _check_quote(claim, primary)
        )
        if mismatch is not None:
            out.append(mismatch)
    return out


def _check_number(claim: Claim, evidence: Evidence) -> Rejection | None:
    detail = {
        "evidenceId": evidence.id,
        "claimValue": claim.value,
        "claimUnit": claim.unit,
        "evidenceValue": evidence.value,
        "evidenceUnit": evidence.unit,
    }
    if not _is_number(claim.value):
        return Rejection(
            VALUE_MISMATCH,
            f"claim '{claim.id}'는 number인데 값이 숫자가 아닙니다. 근거 '{evidence.id}'의 "
            "숫자 값을 그대로 넣으세요.",
            claim_id=claim.id,
            detail=detail,
        )
    if not _is_number(evidence.value):
        return Rejection(
            VALUE_MISMATCH,
            f"claim '{claim.id}'의 근거 '{evidence.id}'에는 수치 값이 없습니다. 수치가 있는 "
            "근거를 첫 번째 근거로 쓰세요.",
            claim_id=claim.id,
            detail=detail,
        )
    try:
        same = same_value(float(claim.value), claim.unit, float(evidence.value), evidence.unit)
    except UnitError:
        return Rejection(
            UNIT_MISMATCH,
            f"claim '{claim.id}'의 단위({claim.unit})를 근거 '{evidence.id}'의 단위"
            f"({evidence.unit})로 환산할 수 없습니다. 근거와 같은 차원의 단위를 쓰세요.",
            claim_id=claim.id,
            detail=detail,
        )
    if same:
        return None
    return Rejection(
        VALUE_MISMATCH,
        f"claim '{claim.id}'의 값({_shown(claim.value, claim.unit)})이 근거 '{evidence.id}'의 "
        f"값({_shown(evidence.value, evidence.unit)})과 다릅니다. 근거의 값을 그대로 쓰세요.",
        claim_id=claim.id,
        detail=detail,
    )


def _shown(value: Any, unit: str | None) -> str:
    return f"{value} {unit}" if unit else f"{value}"


def _check_quote(claim: Claim, evidence: Evidence) -> Rejection | None:
    quote = claim.value
    if isinstance(quote, str) and quote and quote in evidence.excerpt:
        if len(quote.strip()) >= MIN_QUOTE_CHARS or quote.strip() == evidence.excerpt.strip():
            return None
        return Rejection(
            QUOTE_MISMATCH,
            f"claim '{claim.id}'의 인용이 너무 짧습니다. 근거 '{evidence.id}'의 원문에서 "
            "뜻이 통하는 구절이나 문장 전체를 그대로 복사하세요.",
            claim_id=claim.id,
            detail={"evidenceId": evidence.id, "quote": quote, "minChars": MIN_QUOTE_CHARS},
        )
    return Rejection(
        QUOTE_MISMATCH,
        f"claim '{claim.id}'의 인용이 근거 '{evidence.id}'의 원문과 글자 그대로 일치하지 "
        "않습니다. 원문에서 이어진 부분을 공백과 문장부호까지 그대로 복사하세요.",
        claim_id=claim.id,
        detail={"evidenceId": evidence.id, "quote": quote},
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mask_identifier(match: re.Match[str]) -> str:
    """식별자는 가리되, 바로 뒤에 단위가 붙은 것은 숫자로 남긴다."""
    if _UNIT_AFTER.match(match.string, match.end()):
        return match.group(0)
    return " " * len(match.group(0))


def _mask_date(match: re.Match[str]) -> str:
    """날짜는 가리되, 월·일이 범위를 벗어나면 날짜가 아니므로 숫자로 남긴다."""
    text = match.group(0)
    parts = [int(d) for d in _DIGITS.findall(text)]
    month_day = parts[1:] if parts and parts[0] > 31 else parts  # 맨 앞이 연도면 뺀다
    if len(month_day) >= 1 and not 1 <= month_day[0] <= 12:
        return text
    if len(month_day) >= 2 and not 1 <= month_day[1] <= 31:
        return text
    return " " * len(text)


def _mask(pattern: re.Pattern[str], text: str) -> str:
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _token_pattern(token: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])")
