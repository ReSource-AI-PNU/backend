"""사람이 읽는 이름. claim의 label·role, calc 근거의 이름, 매입 기준의 이름을 한곳에서 정한다.

- claim의 `role`은 그 값이 무엇인지 알려 주는 낱말이고(화면이 부호나 묶음을 정할 때 쓴다),
  `label`은 화면에 그대로 보여 줄 한국어 이름이다. 둘 다 선택 필드이고 근거 검증기는 보지 않는다.
- 이름은 코드가 claim의 키에서 정한다. 모델이나 상대 조직이 보낸 label은 믿지 않고 role에서
  다시 만든다(이름 칸으로 숫자를 흘려보내지 못하게 한다).
- 단가는 값이 음수면 배출 기업이 내는 처리비다. 값은 그대로 두고 role을 `treatmentFee`로 준다.
"""

from __future__ import annotations

import re
from typing import Any

# 판단 도구 이름(기계 ID) -> 화면에 보여 줄 한국어 이름. 실시간 진행(live.py)과 근거의 출처가 함께 쓴다.
# 도구 ID(judge_route 등)는 관리자 화면과 기술 정보에만 둔다(DESIGN.md 제품 문구 원칙)
TOOL_LABELS: dict[str, str] = {
    "identify_stream": "폐기물 식별·등급",
    "judge_route": "경로 판정·매입 기준 대조",
    "judge_reuse": "공정 재투입 검토",
    "check_loading": "최소 물량·적재 검사",
    "search_clauses": "법령 조항 찾기",
    "check_requirements": "규정 확인 항목",
    "match_permit": "허가 품목 대조",
    "find_vendors": "업체 찾기",
    "cost_carbon": "비용·탄소 계산",
    "route_extra_cost": "노선 추가 비용 계산",
    "optimize_assignment": "최적 배정 계산",
    "grade_stream": "등급 판정",
    "negotiate_ask": "단가 협상(요청)",
    "negotiate_reply": "단가 협상(업체 답)",
    "find_outside_vendors": "가입하지 않은 업체 찾기",
}

# role -> 화면에 보여 줄 이름
LABELS: dict[str, str] = {
    "unitPrice": "매입 단가",
    "treatmentFee": "처리비",
    "currentUnitCost": "현행 처리 단가",
    "currentUnitPrice": "현행 판매 단가",
    "referencePrice": "참고 시세",
    "monthlyKg": "월 발생량",
    "totalKg": "합친 월 물량",
    "totalM3": "합친 월 부피",
    "monthlyRevenue": "월 매입액",
    "monthlyAfter": "변경 뒤 월 손익",
    "monthlyNow": "현행 월 손익",
    "difference": "현행 대비 차이",
    "sensitivity": "현행 가정이 다를 때의 차이",
    "carbon": "탄소 변화",
    "carbonValue": "내부 탄소 가격 환산액(판단용)",
    # 자원순환 효과(2026-11-19). 세 지표는 서로 다른 성과라 합치지 않는다
    "ghgReduction": "예상 온실가스 감축량",
    "energySaving": "예상 전 과정 에너지 절감량",
    "virginDisplaced": "신재 원료 대체량",
    "routedKg": "경로별 물량",
    "transport": "월 운반비",
    "labor": "전처리 인건비",
    "extraCost": "추가 운송비",
    "distance": "거리",
    "trips": "회차 수",
    "pickups": "월 수거 횟수",
    "perPickup": "한 번 수거량",
    "capacity": "차량 적재 한도",
    "threshold": "기준값",
    "contamination": "오염도",
    "moisture": "수분",
    "priceDeviation": "기준표 대비 단가 차이",
    "clause": "조항 인용",
}
ROLES = tuple(LABELS)

# claim 키(끝부분, 대소문자 무시) -> (role, 이름). 이름이 None이면 LABELS[role]을 쓴다.
_KEYS: dict[str, tuple[str, str | None]] = {
    "unitPrice": ("unitPrice", None),
    "currentUnitCost": ("currentUnitCost", None),
    "currentUnitPrice": ("currentUnitPrice", None),
    "referencePrice": ("referencePrice", None),
    "monthlyKg": ("monthlyKg", None),
    "minMonthlyKg": ("threshold", "최소 물량"),
    "maxContaminationPct": ("threshold", "오염도 상한"),
    "maxMoisturePct": ("threshold", "수분 상한"),
    "totalKg": ("totalKg", None),
    "totalM3": ("totalM3", None),
    "monthlyRevenue": ("monthlyRevenue", None),
    "monthlyAfter": ("monthlyAfter", None),
    "totalAfter": ("monthlyAfter", "변경 뒤 월 손익 합계"),
    "monthlyNow": ("monthlyNow", None),
    "totalNow": ("monthlyNow", "현행 월 손익 합계"),
    "difference": ("difference", None),
    "totalDifference": ("difference", "현행 대비 차이 합계"),
    "bundleDifference": ("difference", "묶음 전체 현행 대비 차이"),
    "sensitivity": ("sensitivity", None),
    "carbon": ("carbon", None),
    "carbonDifference": ("carbon", "처리 방식 탄소 변화"),
    "carbonProcess": ("carbon", "처리 방식 탄소 변화"),
    "carbonTransport": ("carbon", "운송 배출"),
    "transportCarbon": ("carbon", "운송 배출"),
    "carbonTotal": ("carbon", "탄소 변화(운송 포함)"),
    "carbonPrice": ("carbonValue", "내부 탄소 가격(배출권 시세)"),
    "greenMonthlyAfter": ("monthlyAfter", "탄소 우선 배정의 월 손익"),
    "greenCarbon": ("carbon", "탄소 우선 배정의 탄소 변화"),
    "greenMoneyGap": ("difference", "탄소 우선 배정과의 월 손익 차이"),
    "greenCarbonGap": ("carbon", "탄소 우선 배정과의 탄소 차이"),
    "totalCarbon": ("carbon", "탄소 변화 합계"),
    "kauValue": ("carbonValue", None),
    "carbonValue": ("carbonValue", None),
    "monthlyTransport": ("transport", None),
    "transport": ("transport", None),
    "transportPerPickup": ("transport", "1회 운반비"),
    "transportPerMonth": ("transport", "월 운반비 견적"),
    "transportFee": ("transport", "운반비 견적"),
    "transportPickups": ("pickups", "운반 수거 횟수"),
    "transportMonthly": ("transport", "업체 월 운반비"),
    "transportRoom": ("transport", "운반비 손익분기"),
    "revenue": ("monthlyRevenue", None),
    "labor": ("labor", None),
    "monthlyLabor": ("labor", None),
    "laborRate": ("labor", "시간당 인건비"),
    "pretreatHours": ("labor", "월 전처리 작업 시간"),
    "extraCost": ("extraCost", None),
    "roundTripKm": ("distance", "왕복 거리"),
    "distanceKm": ("distance", "공장에서 업체까지 거리"),
    "baseKm": ("distance", "기존 노선 거리"),
    "newKm": ("distance", "정류장을 더한 노선 거리"),
    "extraKm": ("distance", "늘어난 노선 거리"),
    "trips": ("trips", None),
    "pickupsPerMonth": ("pickups", None),
    "perPickupKg": ("perPickup", None),
    "perPickupM3": ("perPickup", "한 번 수거 부피"),
    "capacityKg": ("capacity", "차량 적재 중량"),
    "capacityM3": ("capacity", "차량 적재 부피"),
    "costPerKm": ("transport", "km당 운송비"),
    "contaminationPct": ("contamination", "오염도(측정)"),
    "measuredContaminationPct": ("contamination", "오염도(측정)"),
    "photoContaminationPct": ("contamination", "사진 추정 오염 비율"),
    # 담당자가 질문에 답한 값(측정값이 아니다)
    "answeredContaminationPct": ("contamination", "오염도(담당자 답)"),
    "answeredMoisturePct": ("moisture", "수분(담당자 답)"),
    "moisturePct": ("moisture", None),
    "measuredMoisturePct": ("moisture", "수분(측정)"),
    "photoMoisturePct": ("moisture", "수분(담당자 답)"),
    "priceDeviation": ("priceDeviation", None),
    "deviation": ("priceDeviation", None),
    "clause": ("clause", None),
    # 자원순환 효과(judge/circular.py). 키 끝말로 role을 정한다
    "ghgReduction": ("ghgReduction", None),
    "energySaving": ("energySaving", None),
    "virginDisplaced": ("virginDisplaced", None),
    "routedKg": ("routedKg", None),
    "bundleKg": ("routedKg", "묶음 수거로 재활용 경로에 드는 물량"),
    "baselineEmission": ("carbon", "현행 처리 단계 배출"),
}
_ORDERED = sorted(_KEYS, key=len, reverse=True)
_TAIL = re.compile(r"(?:_\d+|\d+)$")  # 풀이 겹침을 피하려고 붙인 번호, distanceKm0의 순번
_LETTER = re.compile(r"(sensitivity|clause)[A-Z]$")
_DIGIT = re.compile(r"\d")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def role_of(key: Any, value: Any = None, kind: str | None = None) -> str | None:
    """claim 키에서 role을 정한다. 모르는 키면 None."""
    found = _match(key)
    if found is None:
        return "clause" if kind == "clause" else None
    role = found[0]
    if role == "unitPrice" and _is_number(value) and value < 0:
        return "treatmentFee"
    return role


def _match(key: Any) -> tuple[str, str | None] | None:
    whole = _LETTER.sub(r"\1", str(key or "")).lower()
    for text in (whole, _TAIL.sub("", whole)):  # capacityM3처럼 숫자로 끝나는 키를 먼저 본다
        for known in _ORDERED:
            if text and text.endswith(known.lower()):
                return _KEYS[known]
    return None


def describe(key: Any, value: Any = None, kind: str | None = None) -> tuple[str | None, str | None]:
    """(label, role). 모르는 키면 (None, None)이고 화면은 이름 없이 값만 보여 준다."""
    role = role_of(key, value, kind)
    if role is None:
        return None, None
    found = _match(key)
    label = found[1] if found and found[1] and role == found[0] else None
    return label or LABELS[role], role


def annotate(claim: dict[str, Any] | None, key: Any = None, label: str | None = None):
    """claim(dict)에 label과 role을 붙인 새 dict. key가 없으면 claim의 id로 정한다.

    label을 주면 그 이름을 쓴다(코드가 정한 이름만 넘긴다). 모르는 키면 그대로 돌려준다.
    """
    if not claim:
        return claim
    found, role = describe(key or claim.get("id"), claim.get("value"), claim.get("kind"))
    if role is None:
        return claim
    return {**claim, "label": label or found, "role": role}


def trusted(claim: dict[str, Any]) -> dict[str, Any]:
    """밖에서 받은 claim의 label·role을 코드가 아는 값으로만 남긴다.

    role이 아는 낱말이 아니면 키에서 다시 정하고, label은 늘 role과 키에서 다시 만든다.
    단가의 부호와 role이 어긋나면(음수인데 unitPrice) 값에 맞춘다.
    """
    out = {k: v for k, v in claim.items() if k not in ("label", "role")}
    label, role = describe(claim.get("id"), claim.get("value"), claim.get("kind"))
    given = claim.get("role")
    if role is None and given in LABELS and given not in ("unitPrice", "treatmentFee"):
        role, label = given, LABELS[given]
    if role is None:
        return out
    return {**out, "label": label, "role": role}


def calc_label(key: Any, subject: str | None = None, value: Any = None) -> str:
    """calc 근거의 이름. 예: "스프루·런너 현행 대비 차이 계산". 모르는 키면 키를 그대로 쓴다."""
    label, _ = describe(key, value)
    if label is None:
        return str(key)
    return f"{subject} {label} 계산" if subject else f"{label} 계산"


def criterion_label(criterion: dict[str, Any] | None, fallback: str = "") -> str:
    """매입 기준의 이름. 재질·등급·형태로 쓴다. 예: "PP · A등급 · 분쇄"."""
    criterion = criterion or {}
    parts = []
    if criterion.get("material"):
        parts.append(str(criterion["material"]))
    grade = criterion.get("grade")
    if grade:
        parts.append(str(grade) if str(grade) == "등외" else f"{grade}등급")
    if criterion.get("form"):
        parts.append(str(criterion["form"]))
    return " · ".join(parts) or fallback


def has_digit(text: Any) -> bool:
    return bool(_DIGIT.search(str(text or "")))


# ── 근거의 출처 ────────────────────────────────────────────────────────
_TOOL_SOURCE = re.compile(r"^판단 도구 ([a-z_]+)$")
_DOCUMENT_SOURCE = re.compile(r"^(?P<doc>.*) \(Document Intelligence 추출, .*확인\)$")
_DETECTOR_SOURCE = re.compile(r"^탐지 모델 \S+ \S+ · (?P<file>.*)$")
_PLAIN_SOURCES = {
    "카카오모빌리티 자동차 길찾기 API (GET /v1/directions)": "카카오모빌리티 자동차 길찾기",
    "Azure ML 재학습 작업(compare_and_gate.py)이 낸 결과를 관리자가 등록": "재학습 결과(관리자가 등록)",
}


def tool_label(tool: Any) -> str | None:
    return TOOL_LABELS.get(str(tool)) if tool else None


def source_label(kind: str | None, source: str | None, meta: dict[str, Any] | None = None) -> str:
    """근거 출처의 화면 이름. 도구 ID, 서비스 이름, 조직 ID 같은 개발자 말을 하는 일로 바꾼다.

    저장된 출처(source)는 그대로 두고 보여 줄 때만 바꾼다(근거 내용이 바뀌면 판이 올라간다).
    """
    text = str(source or "")
    meta = meta or {}
    tool = meta.get("tool")
    if tool == "grade_stream":
        return "등급 판정 모델" if kind == "model" else "등급 기준값 규칙(가정값)"
    if kind == "calc" and tool_label(tool):
        return str(tool_label(tool))
    found = _TOOL_SOURCE.match(text)
    if found and tool_label(found.group(1)):
        return str(tool_label(found.group(1)))
    found = _DOCUMENT_SOURCE.match(text)
    if found:
        return f"{found.group('doc')} · 문서에서 읽고 담당자가 확인한 값"
    found = _DETECTOR_SOURCE.match(text)
    if found:
        return f"사진 탐지 모델 · {found.group('file')}"
    return _PLAIN_SOURCES.get(text, text)
