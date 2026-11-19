"""계약 검토 담당: 두 AI가 합의한 조건을 사람이 승인하기 전에 다시 대조하는 세 번째 에이전트.

2026-11-19 멘토링 의견("계약서 작성이나 더 엄밀한 부분을 체크할 새 에이전트가 필요하다", "아무도 써 보지 않아
단점이 보이지 않으니 최소한의 안전장치가 중요하다")에 답한다.

- 회사 측 AI와 업체 측 AI는 각자 자기 편을 대변한다. 계약 검토 담당은 어느 편도 아니고, 합의한 조건을
  결과 한 장의 확인 항목, 업체가 정한 범위, 허가 근거, 수거 가능 지역, 법령 요건, 근거 검증 결과와 하나씩 대조한다.
- 안전장치이므로 모델을 부르지 않는다. 규칙으로 대조해 같은 입력에는 늘 같은 결과를 낸다. 그래서 화면에서도
  "AI"가 아니라 "담당"이라고 부른다(DESIGN.md "정직한 이름").
- 항목 상태는 셋이다. block(막힘)은 이대로 승인하면 안 되는 것이라 양쪽 모두 승인할 수 없다
  (orchestrator.approve가 REVIEW_BLOCKED로 막는다). check(확인)는 사람이 확인해야 하는 것이라 화면에서
  담당자가 확인했다고 표시해야 승인 버튼이 켜진다. ok(통과)는 대조해 맞은 것이다.
- 문장에는 숫자를 쓰지 않는다(숫자는 결과 한 장과 문서의 근거가 붙은 값이 보여 준다). 업체가 정한 하한·상한
  단가처럼 상대에게 감춘 값도 쓰지 않는다. 검토 결과는 양쪽이 함께 본다.

수거 가능 지역(area_check)도 여기에 둔다. 업체가 밝힌 수거 가능 지역(시·군·구, 산업단지)과 차고지에서의
최대 수거 거리를 공장 지역·거리와 견준다(fleet.serviceAreas, fleet.maxPickupKm).
"""

from __future__ import annotations

import re
from typing import Any

LABEL = "계약 검토 담당"
ACTOR = "contract_reviewer"

# 항목 순서(화면에 이 순서로 보인다). 키: 제목
TITLES = {
    "evidence": "근거 대조",
    "terms_range": "업체가 정한 범위",
    "permit": "허가 근거",
    "pickup": "수거 가능 지역",
    "volume": "최소 물량과 적재",
    "transport": "운반비 부담",
    "inspection": "반입 검수 조건",
    "law": "법령 요건",
    "pilot": "첫 거래 시범 기간",
}
# 문장 속 근거 자리표시자와 그 앞 빈칸(예: "거리 {{st_a_transportKm}}가" → "거리가")
PLACEHOLDER = re.compile(r"\s?\{\{[^{}]+\}\}")
RANK = {"ok": 0, "check": 1, "block": 2}
# 결과 한 장의 확인 항목 상태 → 검토 상태
FROM_CHECK = {"met": "ok", "conditional": "check", "needs_information": "check", "unmet": "block"}

# ── 수거 가능 지역 ──────────────────────────────────────────────────

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
TAILS = ("국가산업단지", "일반산업단지", "산업단지", "산단")


def _plain(text: Any) -> str:
    return "".join(str(text or "").split())


def _core(token: str) -> str:
    for tail in TAILS:
        if token.endswith(tail) and len(token) > len(tail):
            return token[: -len(tail)]
    return token


def _where(region: str) -> str:
    where = _plain(region)
    for long, short in PROVINCES.items():
        where = where.replace(long, short)
    return where


def in_area(area: str, region: str) -> bool:
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


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def area_check(
    fleet: dict[str, Any] | None,
    region: str | None,
    distance: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """결과 한 장의 "수거 가능 지역" 확인 항목(key pickup_area). 업체가 지역도 거리도 밝히지 않았으면 None.

    distance는 공장에서 업체까지 거리 claim(최적 배정이 낸 transportKm)이다. 근거가 있으면 문장에 자리표시자로 넣어
    화면이 근거가 열리는 값으로 그린다. 업체가 밝힌 최대 거리 값은 문장에 쓰지 않는다(업체 근거는 배출 기업에 열지 않는다).
    밝힌 지역이나 거리 안이면 met, 거리를 알 수 없어 대조하지 못했으면 conditional, 밖이면 unmet이다.
    """
    fleet = fleet or {}
    areas = [str(a).strip() for a in fleet.get("serviceAreas") or [] if str(a).strip()]
    limit = _number(fleet.get("maxPickupKm"))
    if not areas and limit is None:
        return None
    hit = next((a for a in areas if in_area(a, region or "")), None)
    km = _number((distance or {}).get("value")) if distance else None
    near = limit is not None and km is not None and km <= limit
    numbered = bool(distance and distance.get("evidence") and km is not None)
    claims = [distance] if numbered and distance else []
    span = (
        f"공장까지 거리 {{{{{distance['id']}}}}}가 업체가 밝힌 최대 수거 거리"
        if numbered and distance
        else "공장까지 거리가 업체가 밝힌 최대 수거 거리"
    )
    listed = ", ".join(areas[:3]) + (" 등" if len(areas) > 3 else "")
    if hit:
        status, text = "met", f"공장 지역이 업체가 밝힌 수거 가능 지역({hit}) 안에 있습니다."
        claims = []
    elif near:
        status, text = "met", f"{span} 안입니다."
    elif limit is not None and km is None:
        status = "conditional"
        text = (
            "공장까지 거리를 알 수 없어 업체가 밝힌 최대 수거 거리와 대조하지 못했습니다. "
            "수거하러 올 수 있는지 업체에 확인이 필요합니다."
        )
        claims = []
    else:
        outside = []
        if areas:
            outside.append(f"공장 지역이 업체가 밝힌 수거 가능 지역({listed}) 밖에 있습니다.")
        if limit is not None and km is not None:
            outside.append(f"{span}를 넘습니다.")
        else:
            claims = []
        status = "unmet"
        text = " ".join(
            [*outside, "업체가 따로 수거하러 오기로 확인하기 전에는 계약할 수 없습니다."]
        )
    return {
        "key": "pickup_area",
        "status": status,
        "text": text,
        "claims": claims,
        "evidence": list(dict.fromkeys(e for c in claims for e in c.get("evidence") or [])),
    }


# ── 합의 조건 검토 ──────────────────────────────────────────────────


def _worst(statuses: list[str], empty: str = "ok") -> str:
    return max(statuses, key=lambda s: RANK[s]) if statuses else empty


def _checks(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    return [c for row in rows for c in row.get("checks") or [] if c.get("key") in keys]


def _plain_text(text: str) -> str:
    """자리표시자를 뺀 문장. 검토 결과는 양쪽이 함께 보므로 한쪽에만 열린 근거의 값을 싣지 않는다."""
    return PLACEHOLDER.sub("", text).replace("  ", " ")


def _first_text(found: list[dict[str, Any]], status: str) -> str | None:
    for check in found:
        if FROM_CHECK.get(check.get("status"), "check") == status and check.get("text"):
            return _plain_text(str(check["text"]))
    return None


def _item(key: str, status: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "title": TITLES[key], "status": status, "text": text, **extra}


def review(
    *,
    rows: list[dict[str, Any]],
    offers: dict[str, dict[str, Any]],
    guardrails: dict[str, Any] | None,
    regulation: list[dict[str, Any]] | None,
    approval: dict[str, Any] | None,
    earlier_deals: int,
    stream_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """합의한 대화 하나의 검토 결과.

    rows는 결과 한 장에서 이 대화에 배정된 줄(assignments), offers는 그 대화에서 받은 제안(흐름 ID별),
    guardrails는 업체가 정한 범위, regulation은 결과 한 장의 법령 요건, approval은 저장된 승인 상태(막힘 사유),
    earlier_deals는 같은 두 회사가 다른 찾기에서 확정한 거래 수다.

    돌려주는 값: {status: blocked|needs_check|clear, summary, items: [{key, title, status, text, ...}], reviewer}
    """
    guard = guardrails or {}
    names = stream_names or {}
    items: list[dict[str, Any]] = []

    # 근거 대조: 승인 전 근거 검증(orchestrator._verify)이 막았으면 그 사유를 그대로 막힘으로 둔다
    if approval and approval.get("blocked"):
        items.append(
            _item(
                "evidence",
                "block",
                str(approval.get("blockedReason") or "근거 확인에 실패한 항목이 있습니다."),
            )
        )
    else:
        items.append(
            _item(
                "evidence",
                "ok",
                "두 AI가 주고받은 숫자와 조항을 승인 직전에 근거와 다시 대조했고 모두 맞습니다.",
            )
        )

    # 업체가 정한 범위: 업체 측 AI의 가드레일과 별개로 합의한 단가와 요일을 다시 본다(값은 쓰지 않는다)
    floor = _number(guard.get("floorPriceKrwPerKg"))
    ceiling = _number(guard.get("ceilingPriceKrwPerKg"))
    allowed = list(guard.get("days") or [])
    outside: list[str] = []
    for row in rows:
        offer = offers.get(row.get("streamId")) or {}
        price = _number(offer.get("unitPriceKrwPerKg"))
        name = names.get(row.get("streamId"), "폐기물")
        if price is not None and (
            (floor is not None and price < floor) or (ceiling is not None and price > ceiling)
        ):
            outside.append(f"{name}의 합의 단가")
        if allowed and any(d not in allowed for d in offer.get("days") or []):
            outside.append(f"{name}의 수거 요일")
    if outside:
        items.append(
            _item(
                "terms_range",
                "block",
                f"업체가 정한 범위 밖의 조건이 합의에 들어 있습니다({', '.join(outside)}). "
                "업체 측 담당자가 범위를 바꾸거나 다시 협의해야 합니다.",
            )
        )
    elif floor is None and ceiling is None and not allowed:
        items.append(
            _item(
                "terms_range",
                "check",
                "업체가 AI가 지킬 단가 범위와 수거 요일을 정해 두지 않았습니다. "
                "업체 측 담당자가 합의 단가와 요일을 직접 확인해야 합니다.",
            )
        )
    else:
        items.append(
            _item("terms_range", "ok", "합의한 단가와 수거 요일이 업체가 정한 범위 안입니다.")
        )

    # 허가 근거(결과 한 장의 permit_level)
    permits = _checks(rows, "permit_level")
    status = _worst([FROM_CHECK.get(c.get("status"), "check") for c in permits], "check")
    if not permits:
        text = "업체의 허가 근거를 확인하지 못했습니다. 허가증 사본으로 확인해야 합니다."
    else:
        text = _first_text(permits, status) or "허가 근거를 확인했습니다."
    items.append(_item("permit", status, text))

    # 수거 가능 지역(밝힌 지역·거리가 있으면 그것, 없으면 노선)
    area = _checks(rows, "pickup_area")
    route = _checks(rows, "pickup_route")
    found = area or route
    if found:
        status = _worst([FROM_CHECK.get(c.get("status"), "check") for c in found])
        text = _first_text(found, status) or "수거 가능 지역을 확인했습니다."
        items.append(_item("pickup", status, text))
    else:
        items.append(
            _item(
                "pickup",
                "check",
                "업체가 수거 가능 지역과 수거 노선을 밝히지 않았습니다. "
                "이 공장까지 수거하러 오는지 업체에 확인해야 합니다.",
            )
        )

    # 최소 물량과 적재
    volume = _checks(rows, "min_volume", "loading", "capacity")
    status = _worst([FROM_CHECK.get(c.get("status"), "check") for c in volume])
    text = (
        _first_text(volume, status)
        if status != "ok"
        else "합친 물량이 업체의 최소 물량 이상이고 한 번 수거량이 차량에 실립니다."
    )
    items.append(_item("volume", status, text or "물량과 적재를 확인해야 합니다."))

    # 운반비 부담
    payers = {row.get("transportPaidBy") for row in rows}
    if payers == {"vendor"}:
        items.append(_item("transport", "ok", "운반비는 업체가 부담합니다(단가에 포함)."))
    elif payers == {"emitter"}:
        items.append(
            _item(
                "transport",
                "ok",
                "운반비는 회사가 부담하고, 결과의 한 달 손익에 넣어 계산했습니다.",
            )
        )
    else:
        items.append(
            _item(
                "transport",
                "check",
                "운반비를 누가 내는지 정해지지 않은 폐기물이 있습니다. 계약서에 부담 주체를 적어야 합니다.",
            )
        )

    # 반입 검수 조건: 측정하지 않은 값이나 업체가 건 검수 조건이 있으면 사람이 알고 승인해야 한다
    basis = _checks(
        rows, "contamination_basis", "moisture_basis", "monthly_kg_basis", "vendor_condition"
    )
    if basis:
        items.append(
            _item(
                "inspection",
                "check",
                "오염도·수분·물량 가운데 측정하지 않은 값이 있거나 업체가 반입 검수 조건을 걸었습니다. "
                "검수에서 기준을 넘으면 그 업체 매입 기준표의 다음 등급 단가를 따른다는 조항을 계약서에 넣습니다.",
            )
        )
    else:
        items.append(
            _item("inspection", "ok", "오염도와 수분이 측정값이고 업체가 건 검수 조건이 없습니다.")
        )

    # 법령 요건: 결과 한 장의 요건 가운데 이 대화의 폐기물 것만. 허가 보유는 위 허가 근거가 본다
    wanted = {row.get("streamId") for row in rows}
    unmet: list[str] = []
    todo: list[str] = []
    for entry in regulation or []:
        if entry.get("streamId") not in wanted:
            continue
        for req in entry.get("items") or []:
            if req.get("key") == "permit_holder":
                continue
            title = str(req.get("title") or "")
            if req.get("status") == "unmet" and title not in unmet:
                unmet.append(title)
            elif req.get("status") != "met" and title and title not in todo:
                todo.append(title)
    if unmet:
        items.append(
            _item(
                "law",
                "block",
                f"충족하지 못한 법령 요건이 있습니다: {', '.join(unmet)}.",
            )
        )
    elif todo:
        items.append(
            _item(
                "law",
                "check",
                f"담당자가 챙길 법령 요건이 있습니다({todo[0]}"
                + (" 등" if len(todo) > 1 else "")
                + "). 결과의 법령 요건 확인에서 방법과 근거 조항을 볼 수 있습니다.",
            )
        )
    else:
        items.append(_item("law", "ok", "이 거래에 걸리는 법령 요건을 모두 확인했습니다."))

    # 첫 거래 시범 기간(안전장치): 함께 확정한 거래가 없으면 계약서 초안에 시범 기간을 넣는다
    if earlier_deals <= 0:
        items.append(
            _item(
                "pilot",
                "ok",
                "이 업체와 처음 거래합니다. 계약서 초안에 한 달 시범 기간을 넣어, 첫 반입 검수 기록을 "
                "양쪽이 확인한 뒤에 다음 달 수거를 이어 가게 합니다.",
                pilot=True,
            )
        )
    else:
        items.append(
            _item(
                "pilot",
                "ok",
                "이 업체와 앞서 확정한 거래가 있어 시범 기간을 두지 않습니다.",
                pilot=False,
            )
        )

    order = list(TITLES)
    items.sort(key=lambda item: order.index(item["key"]))
    blocks = [item["title"] for item in items if item["status"] == "block"]
    checks = [item["title"] for item in items if item["status"] == "check"]
    if blocks:
        overall = "blocked"
        summary = (
            f"합의 조건을 항목마다 대조했습니다. 이대로는 승인할 수 없습니다: {', '.join(blocks)}."
        )
    elif checks:
        overall = "needs_check"
        summary = (
            "합의 조건을 항목마다 대조했고 막을 항목은 없습니다. "
            f"승인 전에 담당자가 확인할 것: {', '.join(checks)}."
        )
    else:
        overall = "clear"
        summary = "합의 조건을 항목마다 대조했고 모두 맞습니다."
    return {
        "status": overall,
        "summary": summary,
        "items": items,
        "reviewer": {"label": LABEL, "kind": "rules"},
    }


def blocked_titles(found: dict[str, Any] | None) -> list[str]:
    return [i["title"] for i in (found or {}).get("items") or [] if i["status"] == "block"]
