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

# role -> 화면에 보여 줄 이름
LABELS: dict[str, str] = {
    "unitPrice": "매입 단가",
    "treatmentFee": "처리비",
    "currentUnitCost": "현행 처리 단가",
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
    "carbonValue": "탄소 감축 환산액",
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
    "sensitivity": ("sensitivity", None),
    "carbon": ("carbon", None),
    "carbonDifference": ("carbon", None),
    "totalCarbon": ("carbon", "탄소 변화 합계"),
    "kauValue": ("carbonValue", None),
    "carbonValue": ("carbonValue", None),
    "monthlyTransport": ("transport", None),
    "transport": ("transport", None),
    "transportPerPickup": ("transport", "1회 운반비"),
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
    "moisturePct": ("moisture", None),
    "measuredMoisturePct": ("moisture", "수분(측정)"),
    "photoMoisturePct": ("moisture", "수분(담당자 답)"),
    "priceDeviation": ("priceDeviation", None),
    "deviation": ("priceDeviation", None),
    "clause": ("clause", None),
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
