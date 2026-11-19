"""방문 수거 상태와 운반비 조건. 업체가 밝힌 노선·운반 조건과 공장 지역에서 이 공장에 적용되는 조건을 정한다.

수거 상태(status)와 운반비 조건(basis, paidBy, 금액·단위·횟수)은 따로 둔다.

- status: route 기존 노선 포함, check 별도 수거 확인 필요(노선 밖이고 업체가 방문 조건을 밝히지 않음),
  visit 업체 수거 가능 확인(노선 밖이지만 업체가 따로 오겠다고 밝힘), unavailable 수거 불가,
  unknown 미상(업체가 노선을 밝히지 않았거나 공장 지역을 모름)
- basis: included 단가에 포함, separate 별도, undecided 미정
- paidBy: vendor(업체 부담) | emitter(공장 부담) | None(미정)

업체 프로필의 transport(운반 조건)는 노선 수거의 조건이다. 노선 밖 공장에 대한 조건은 transport.offRoute에
둔다: {"status": "visit"|"unavailable", "paidBy", "feePerPickupKrw"|"feePerMonthKrw", "pickupsPerMonth",
"evidence"}. 노선 밖인데 offRoute가 없으면 노선 수거의 "단가에 포함"을 그대로 쓰지 않는다(미정).

운반비가 정해졌다(determined)는 것은 월 운반비를 원 단위로 낼 수 있다는 뜻이다: 단가에 포함이면 0원,
별도이면 월 금액이 있거나 1회 금액과 월 수거 횟수가 모두 있을 때다. 정해지지 않았으면 월 손익은
"별도 운반비 반영 전" 값이다(0원으로 확정하지 않는다).
"""

from __future__ import annotations

import math
from typing import Any

STATUSES = ("route", "check", "visit", "unavailable", "unknown")
BASES = ("included", "separate", "undecided")
STATUS_LABEL = {
    "route": "기존 노선 포함",
    "check": "별도 수거 확인 필요",
    "visit": "업체 수거 가능 확인",
    "unavailable": "수거 불가",
    "unknown": "수거 방식 미상",
}
BASIS_LABEL = {"included": "단가에 포함", "separate": "별도", "undecided": "미정"}
UNIT_OF = {"per_pickup": "원/회", "per_month": "원/월"}
WEEKDAY_ORDER = ("월", "화", "수", "목", "금", "토", "일")
# 이 상태에서는 수거가 확인되지 않았다(결과는 조건부)
UNCONFIRMED = ("check", "unknown")


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _core(text: Any) -> str:
    """지역·산업단지 이름에서 "국가산업단지", "산단" 같은 꼬리를 뗀다."""
    plain = "".join(str(text).split())
    for tail in ("국가산업단지", "일반산업단지", "산업단지", "산단"):
        if plain.endswith(tail) and len(plain) > len(tail):
            return plain[: -len(tail)]
    return plain


def route_cover(routes: Any, region: Any) -> tuple[bool | None, list[str]]:
    """(업체가 밝힌 노선이 공장 지역을 지나는가 | 모름(None), 지나는 노선의 운행 요일).

    노선의 지역 이름에서 꼬리를 뗀 이름이 공장 지역 글에 들어 있으면 지나간다고 본다. 노선이 없거나
    공장 지역을 모르면 None이다.
    """
    routes = [r for r in routes or [] if isinstance(r, dict)]
    where = "".join(str(region or "").split())
    if not routes or not where:
        return None, []
    passing = [
        r
        for r in routes
        if any(
            _core(p) and _core(p) in where
            for p in [*(r.get("regions") or []), *(r.get("complexes") or [])]
        )
    ]
    if not passing:
        return False, []
    days = [d for d in WEEKDAY_ORDER if any(d in (r.get("days") or []) for r in passing)]
    return True, days


def fee_terms(section: Any) -> dict[str, Any]:
    """운반 조건 한 묶음(transport 또는 transport.offRoute)을 고른 모양으로. 없으면 미정."""
    section = section if isinstance(section, dict) else {}
    paid_by = section.get("paidBy") if section.get("paidBy") in ("vendor", "emitter") else None
    basis = section.get("basis") if section.get("basis") in BASES else None
    if basis is None:
        basis = {"vendor": "included", "emitter": "separate"}.get(paid_by or "", "undecided")
    if basis == "separate" and paid_by is None:
        paid_by = "emitter"  # 따로 받는 운반비는 공장이 낸다
    if basis == "included":
        paid_by = "vendor"
    per_month = _num(section.get("feePerMonthKrw"))
    per_pickup = _num(section.get("feePerPickupKrw"))
    amount, unit = (
        (per_month, "per_month")
        if per_month is not None
        else (per_pickup, "per_pickup")
        if per_pickup is not None
        else (None, None)
    )
    return {
        "basis": basis,
        "paidBy": paid_by if basis != "undecided" else None,
        "amount": amount if basis == "separate" else None,
        "unit": unit if basis == "separate" else None,
        "pickups": _num(section.get("pickupsPerMonth")),
        "evidence": _ids(section.get("evidence")),
        # 금액 근거와 횟수 근거(없으면 evidence 전체가 금액 근거다)
        "feeEvidence": _ids(section.get("feeEvidence")),
        "pickupsEvidence": _ids(section.get("pickupsEvidence")),
    }


def _ids(value: Any) -> list[str]:
    return [e for e in value or [] if isinstance(e, str)]


def effective(transport: Any, routes: Any, region: Any) -> dict[str, Any]:
    """이 공장에 적용되는 수거 상태와 운반비 조건.

    {"status", "days", "basis", "paidBy", "amount", "unit", "pickups", "evidence", "routeBasis"}.
    routeBasis는 노선 수거의 운반비 조건이다(노선 밖이라 그대로 쓰지 않았을 때 참고로 보인다).
    """
    transport = transport if isinstance(transport, dict) else {}
    covered, days = route_cover(routes, region)
    general = fee_terms(transport)
    off = transport.get("offRoute") if isinstance(transport.get("offRoute"), dict) else {}
    out: dict[str, Any] = {"days": days, "routeBasis": general["basis"]}
    if covered is True:
        return {**out, "status": "route", **general}
    if covered is False:
        stance = off.get("status")
        if stance == "unavailable":
            return {
                **out,
                "status": "unavailable",
                **fee_terms({}),
                "evidence": [e for e in off.get("evidence") or [] if isinstance(e, str)],
            }
        if stance == "visit":
            return {**out, "status": "visit", **fee_terms(off)}
        return {**out, "status": "check", **fee_terms({})}
    return {**out, "status": "unknown", **general}


def monthly(terms: dict[str, Any], pickups: float | None = None) -> float | None:
    """월 운반비(원). 단가에 포함이면 0, 별도면 월 금액 또는 1회 금액 × 월 횟수. 정할 수 없으면 None.

    pickups는 조건에 횟수가 없을 때 쓸 다른 출처의 횟수(업체가 정한 월 최소 횟수 등)다.
    """
    if terms.get("status") == "unavailable":
        return None
    if terms.get("basis") == "included":
        return 0.0
    if terms.get("basis") != "separate" or terms.get("amount") is None:
        return None
    if terms.get("unit") == "per_month":
        return float(terms["amount"])
    count = _num(terms.get("pickups")) or _num(pickups)
    return float(terms["amount"]) * count if count else None


def determined(terms: dict[str, Any], pickups: float | None = None) -> bool:
    return monthly(terms, pickups) is not None


def shown(terms: dict[str, Any]) -> dict[str, Any]:
    """화면과 지문에 쓰는 요약(근거 ID 없이 값만). 같은 조건이면 같은 dict다."""
    return {
        "status": terms.get("status"),
        "basis": terms.get("basis"),
        "paidBy": terms.get("paidBy"),
        "amount": terms.get("amount"),
        "unit": terms.get("unit"),
        "pickups": terms.get("pickups"),
    }


def tool_transport(terms: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """판단 도구(common.transport_charge)에 넘기는 운반 조건. status와 basis가 있으면 밝힌 조건으로 본다."""
    out: dict[str, Any] = {
        "status": terms.get("status"),
        "basis": terms.get("basis"),
        "evidence": list(terms.get("evidence") or []),
    }
    if terms.get("paidBy"):
        out["paidBy"] = terms["paidBy"]
    if terms.get("amount") is not None:
        key = "feePerMonthKrw" if terms.get("unit") == "per_month" else "feePerPickupKrw"
        out[key] = terms["amount"]
    if terms.get("pickups") is not None:
        out["pickupsPerMonth"] = terms["pickups"]
    for key in ("feeEvidence", "pickupsEvidence"):
        if terms.get(key):
            out[key] = list(terms[key])
    out.update(extra)
    return out


def answer_line(terms: dict[str, Any], fee_ref: str | None, pickups_ref: str | None) -> str:
    """업체 측 조건 답에 붙이는 수거·운반 한 줄. 숫자는 {{claim ID}}로만 쓴다."""
    status = terms.get("status")
    head = {
        "route": "공장 지역이 기존 수거 노선에 들어 있어 노선으로 수거합니다.",
        "visit": "공장 지역이 기존 수거 노선 밖이지만 따로 수거하러 갑니다.",
        "check": "공장 지역이 기존 수거 노선 밖입니다. 따로 수거하러 갈 수 있는지는 담당자가 확인한 뒤 알려 드립니다.",
        "unavailable": "공장 지역이 수거 노선 밖이라 수거하러 갈 수 없습니다.",
        "unknown": "수거 노선을 따로 밝히지 않았습니다.",
    }.get(str(status), "")
    if status == "unavailable":
        return f"수거·운반: {head}"
    basis = terms.get("basis")
    if basis == "included":
        fee = "운반비는 단가에 포함합니다."
    elif basis == "separate" and fee_ref:
        word = "한 달에" if terms.get("unit") == "per_month" else "회당"
        fee = f"운반비는 단가와 따로 {word} {{{{{fee_ref}}}}}이고 공장이 부담합니다."
        if pickups_ref and terms.get("unit") != "per_month":
            fee += f" 수거 횟수는 {{{{{pickups_ref}}}}}입니다."
        elif terms.get("unit") != "per_month":
            fee += " 월 수거 횟수는 담당자가 확인한 뒤 알려 드립니다."
    elif basis == "separate":
        fee = "운반비는 단가와 따로 받으며 금액은 담당자가 확인한 뒤 알려 드립니다."
    else:
        fee = "운반비는 아직 정하지 않았습니다."
    return f"수거·운반: {head} {fee}".strip()
