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

업체가 수거 가능 지역(fleet.serviceAreas)이나 차고지에서의 최대 수거 거리(fleet.maxPickupKm)를 밝혔으면 그것으로도
본다(2026-11-19 멘토링). 노선 밖이어도 수거 가능 지역 안이거나 최대 거리 안이면 업체 수거 가능(visit, 운반비는
노선 밖 조건이 있으면 그것, 없으면 노선 수거 조건), 밖이면 노선 밖 방문을 따로 밝히지 않은 한 수거 불가다. 최대
거리만 밝혔는데 거리를 모르면 확인 필요다. reason에 무엇으로 정했는지(route, area, distance, offRoute)를 남긴다.

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


# ── 수거 가능 지역 대조(계약 검토 담당 reviewer.in_area와 같은 규칙) ─────────────
PROVINCES = {
    "부산광역시": "부산",
    "울산광역시": "울산",
    "대구광역시": "대구",
    "서울특별시": "서울",
    "인천광역시": "인천",
    "광주광역시": "광주",
    "대전광역시": "대전",
    "세종특별자치시": "세종",
    "경기도": "경기",
    "강원도": "강원",
    "강원특별자치도": "강원",
    "충청북도": "충북",
    "충청남도": "충남",
    "전라북도": "전북",
    "전북특별자치도": "전북",
    "전라남도": "전남",
    "경상북도": "경북",
    "경상남도": "경남",
    "제주특별자치도": "제주",
}
SHORT = frozenset(PROVINCES.values())


def _where(region: Any) -> str:
    where = "".join(str(region or "").split())
    for long, short in PROVINCES.items():
        where = where.replace(long, short)
    return where


def in_area(area: Any, region: Any) -> bool:
    """공장 지역 글(region, 예: "부산 강서구 녹산산단")이 업체가 적은 수거 가능 지역 하나(area) 안에 드는지.

    지역은 띄어 쓴 낱말로 본다("경남 김해시", "부산", "녹산국가산업단지"). 시·도만 적었으면 그 시·도가 공장 지역에
    있어야 하고, 시·군·구나 산업단지를 적었으면 그 낱말이 모두 공장 지역에 있어야 한다. 시·도를 함께 적었고 공장
    지역에도 시·도가 있으면 둘이 같아야 한다(부산 강서구와 서울 강서구를 가른다).
    """
    where = _where(region)
    if not where:
        return False
    tokens = [_core(PROVINCES.get(t, t)) for t in str(area or "").split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    named = [t for t in tokens if t in SHORT]
    rest = [t for t in tokens if t not in SHORT]
    if not rest:
        return all(t in where for t in named)
    if not all(t in where for t in rest):
        return False
    there = [p for p in SHORT if where.startswith(p)]
    return not (named and there and not set(named) & set(there))


def service_area(fleet: Any, region: Any, distance_km: Any = None) -> dict[str, Any] | None:
    """업체가 밝힌 수거 가능 지역·최대 수거 거리와 공장의 대조. 둘 다 밝히지 않았으면 None.

    {"inside": True | False | None(거리를 몰라 대조 못 함), "area": 맞은 지역 | None, "areas": 밝힌 지역,
     "limit": 최대 거리 | None, "km": 거리 | None, "by": "area" | "distance"}
    """
    fleet = fleet if isinstance(fleet, dict) else {}
    areas = [str(a).strip() for a in fleet.get("serviceAreas") or [] if str(a).strip()]
    limit = _num(fleet.get("maxPickupKm"))
    if not areas and limit is None:
        return None
    km = _num(distance_km)
    hit = next((a for a in areas if in_area(a, region)), None)
    out = {"area": hit, "areas": areas, "limit": limit, "km": km}
    if hit:
        return {**out, "inside": True, "by": "area"}
    if limit is not None and km is not None:
        return {**out, "inside": km <= limit, "by": "distance"}
    if limit is not None:
        return {**out, "inside": None, "by": "distance"}
    return {**out, "inside": False, "by": "area"}


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


def effective(
    transport: Any, routes: Any, region: Any, fleet: Any = None, distance_km: Any = None
) -> dict[str, Any]:
    """이 공장에 적용되는 수거 상태와 운반비 조건.

    {"status", "reason", "days", "basis", "paidBy", "amount", "unit", "pickups", "evidence", "routeBasis", "area"}.
    routeBasis는 노선 수거의 운반비 조건이다(노선 밖이라 그대로 쓰지 않았을 때 참고로 보인다). fleet은 수거 가능
    지역·최대 수거 거리를 읽는 노선·차량이고, distance_km는 공장 → 업체 차고지 거리다(최대 거리와 견준다).
    """
    transport = transport if isinstance(transport, dict) else {}
    covered, days = route_cover(routes, region)
    general = fee_terms(transport)
    off = transport.get("offRoute") if isinstance(transport.get("offRoute"), dict) else {}
    stance = off.get("status")
    out: dict[str, Any] = {"days": days, "routeBasis": general["basis"], "area": None, "areas": []}
    if covered is True:
        return {**out, "status": "route", "reason": "route", **general}
    reach = service_area(fleet, region, distance_km)
    if reach is not None:
        out.update(area=reach["area"], areas=reach["areas"])
        if reach["inside"] is True and stance == "unavailable":
            # 수거 가능 지역에는 들지만 업체가 노선 밖 공장은 수거하지 않는다고 밝혔다. 어느 쪽으로도 정하지 않는다
            return {**out, "status": "check", "reason": "conflict", **fee_terms({})}
        if reach["inside"] is True:
            # 수거 가능 지역(또는 최대 거리) 안: 업체가 수거하러 간다. 노선 밖 조건이 있으면 그 운반비를 쓴다
            terms = fee_terms(off) if stance == "visit" else general
            return {**out, "status": "visit", "reason": reach["by"], **terms}
        if stance == "visit":
            return {**out, "status": "visit", "reason": "offRoute", **fee_terms(off)}
        if reach["inside"] is False or stance == "unavailable":
            return {
                **out,
                "status": "unavailable",
                "reason": reach["by"] if reach["inside"] is False else "offRoute",
                **fee_terms({}),
                "evidence": _ids(off.get("evidence")),
            }
        return {**out, "status": "check", "reason": "distance", **fee_terms({})}
    # 노선을 밝히지 않았어도 노선 밖 조건(방문, 수거 불가)을 밝혔으면 그것을 따른다(공장 지역을 알 때)
    if covered is False or (
        covered is None and _where(region) and stance in ("visit", "unavailable")
    ):
        if stance == "unavailable":
            return {
                **out,
                "status": "unavailable",
                "reason": "offRoute",
                **fee_terms({}),
                "evidence": _ids(off.get("evidence")),
            }
        if stance == "visit":
            return {**out, "status": "visit", "reason": "offRoute", **fee_terms(off)}
        return {**out, "status": "check", "reason": "route", **fee_terms({})}
    return {**out, "status": "unknown", "reason": None, **general}


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
    status, reason = terms.get("status"), terms.get("reason")
    head = {
        "route": "공장 지역이 기존 수거 노선에 들어 있어 노선으로 수거합니다.",
        "visit": "공장 지역이 수거 가능 지역 안이라 수거하러 갑니다(수거 요일은 따로 정합니다)."
        if reason in ("area", "distance")
        else "공장 지역이 기존 수거 노선 밖이지만 따로 수거하러 갑니다.",
        "check": "차고지에서 공장까지 거리를 확인해 최대 수거 거리 안인지 담당자가 알려 드립니다."
        if reason == "distance"
        else "수거 가능 지역과 노선 밖 수거 조건이 서로 달라, 이 공장까지 수거하러 갈 수 있는지 담당자가 확인한 뒤 "
        "알려 드립니다."
        if reason == "conflict"
        else "공장 지역이 기존 수거 노선 밖입니다. 따로 수거하러 갈 수 있는지는 담당자가 확인한 뒤 알려 드립니다.",
        "unavailable": "공장 지역이 수거 가능 지역 밖이라 수거하러 갈 수 없습니다."
        if reason == "area"
        else "공장까지 거리가 최대 수거 거리를 넘어 수거하러 갈 수 없습니다."
        if reason == "distance"
        else "공장 지역이 수거 노선 밖이라 수거하러 갈 수 없습니다.",
        "unknown": "수거 노선을 따로 밝히지 않았습니다.",
    }.get(str(status), "")
    if status == "unavailable":
        return f"수거·운반: {head}"
    basis = terms.get("basis")
    if basis == "included":
        fee = "운반비는 단가에 포함합니다."
    elif basis == "separate" and fee_ref:
        fee = f"운반비는 단가와 따로 {{{{{fee_ref}}}}}이고 공장이 부담합니다."
        if pickups_ref and terms.get("unit") != "per_month":
            fee += f" 수거 횟수는 {{{{{pickups_ref}}}}}입니다."
        elif terms.get("unit") != "per_month":
            fee += " 월 수거 횟수는 담당자가 확인한 뒤 알려 드립니다."
    elif basis == "separate":
        fee = "운반비는 단가와 따로 받으며 금액은 담당자가 확인한 뒤 알려 드립니다."
    else:
        fee = "운반비는 아직 정하지 않았습니다."
    return f"수거·운반: {head} {fee}".strip()
