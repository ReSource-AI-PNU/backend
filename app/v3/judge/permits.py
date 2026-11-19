"""허가 근거의 수준. 허가 품목이 맞는 업체의 허가를 무엇으로 확인했는지 가른다.

| 수준 | 뜻 | 판정 |
|---|---|---|
| registry | 허가 품목에 더해 공공 허가 명부(전국폐기물처리업소표준데이터)에서 같은 상호의 허가 업종과 처리 폐기물 분류번호가 이 폐기물·처리 방식과 맞는다 | 충족 |
| document | 허가증 문서에서 읽은 허가 품목(업종을 읽었으면 처리 방식과도 맞음). 공공 명부에서 이 업체를 찾지 못했다 | 충족(계약 전에 허가 기관에 확인하라고 권한다) |
| self | 업체 담당자가 입력한 허가 품목뿐이다(허가증 원본 확인 전) | 조건부 |
| mismatch | 허가 업종이 처리 방식과 맞지 않거나, 공공 명부의 처리 폐기물 분류에 이 폐기물이 없다 | 불충족(배정하지 않는다) |

허가 품목이 맞는지(permit_match)는 대화를 열지 정하는 문턱이고, 이 수준은 그 허가를 얼마나 믿을 수 있는지다.
self는 최종 배정과 법령 요건(받는 업체의 허가)을 조건부로 내리고, mismatch인 업체에는 그 폐기물을 배정하지 않는다.
허가의 처리 방법은 업종으로 본다: 업체 처리 방식이 소각·매립이면 처분업, 그 밖(재활용, 열분해)이면 재활용업이어야 한다.
허가가 지금도 유효한지는 공공 명부의 기준일까지만 안다(명부에 영업 상태 열이 없다). 기준일이 한 해 넘게 지났으면 그렇게 적는다.
문장에는 숫자를 쓰지 않는다(분류번호, 허가일, 기준일은 근거의 원문에 있다).
"""

from __future__ import annotations

from typing import Any

from app.v3 import ids, public_vendors
from app.v3.evidence.model import Evidence
from app.v3.judge.common import haversine_km, num
from app.v3.textutil import josa

LEVELS = ("registry", "document", "self", "mismatch")
LABELS = {
    "registry": "허가증·공공 명부 확인",
    "document": "허가증 문서",
    "self": "업체 입력",
    "mismatch": "허가 근거가 맞지 않음",
}
SAME_PLACE_KM = 50.0  # 같은 상호라도 업체 차고지에서 이보다 먼 사업장은 다른 곳으로 본다
REGISTRY_SERIES = "허가명부"


def matched_permit(stream: dict, vendor: dict) -> dict | None:
    """이 폐기물의 품목·재질이 든 허가 품목(permit_match와 같은 규칙). 없으면 None."""
    for permit in vendor.get("permits") or []:
        materials = permit.get("materials") or []
        if permit.get("wasteItem") == stream.get("wasteItem") and (
            not materials or stream.get("material") in materials
        ):
            return permit
    return None


def needed_business(vendor: dict) -> str:
    """이 업체의 처리 방식에 필요한 허가 업종의 낱말. 소각·매립이면 "처분", 그 밖이면 "재활용"."""
    process = str(vendor.get("process") or "")
    return "처분" if any(word in process for word in ("소각", "매립")) else "재활용"


def _fits(business: Any, need: str) -> bool:
    return need in str(business or "").replace(" ", "")


def registry_records(name: Any, near: Any = None) -> list[dict[str, Any]]:
    """상호가 같은 공공 명부 업체(같은 상호가 업종만 달리 여러 줄이면 모두). near는 [경도, 위도]."""
    key = public_vendors._norm(str(name or ""))
    if not key:
        return []
    found = [
        v for v in public_vendors.registry()["vendors"] if public_vendors._norm(v["name"]) == key
    ]
    point = near if isinstance(near, (list, tuple)) and len(near) == 2 else None
    if point is not None and all(num(x) is not None for x in point):
        found = [v for v in found if haversine_km(point, (v["lon"], v["lat"])) <= SAME_PLACE_KM]
    return sorted(found, key=lambda v: v["id"])


def _registry_evidence(registry, records: list[dict[str, Any]]) -> str:
    """공공 명부의 그 업체 줄을 근거(ref)로 올린다. 공공데이터라 모두에게 열린다."""
    data = public_vendors.registry()
    first = records[0]
    codes = sorted({c for r in records for c in r.get("codes") or []})
    business = " · ".join(dict.fromkeys(r["business"] for r in records))
    named = [f"{c} {public_vendors.code_name(c) or ''}".strip() for c in codes]
    evidence = Evidence(
        id=ids.ref_id(REGISTRY_SERIES, first["id"], first.get("asOf") or "na"),
        kind="ref",
        label=f"공공 허가 명부: {first['name']} ({business})",
        excerpt=(
            f"상호 {first['name']} | 업종 {business} | 허가일 {first.get('permitDate') or '없음'} | "
            f"허가 기관 {first.get('agency') or '없음'} | 소재지 {first.get('address') or '없음'} | "
            f"처리 폐기물 분류번호 {', '.join(named) or '없음'} | 명부 기준일 {first.get('asOf') or '없음'}"
        ),
        source=f"{data['source']} ({data['url']})",
        as_of=first.get("asOf") or data["asOfRange"][-1],
        meta={
            "series": REGISTRY_SERIES,
            "registryIds": [r["id"] for r in records],
            "codes": codes,
            "codesReliable": all(r.get("codesReliable", True) for r in records),
            "agency": first.get("agency"),
        },
    )
    return registry.put(evidence).id


def _stale(as_of: Any, today: Any) -> bool:
    """기준일(YYYY-MM-DD)이 오늘보다 한 해 넘게 앞서는가. 판단 경로라 시계·날짜 모듈 없이 글자로 비교한다."""
    a, t = str(as_of or ""), str(today or "")
    if len(a) != 10 or len(t) != 10 or not t[:4].isdigit():
        return False
    return a < f"{int(t[:4]) - 1:04d}{t[4:]}"


def _result(level: str, status: str, text: str, evidence: list[str], **extra) -> dict[str, Any]:
    return {
        "level": level,
        "label": LABELS[level],
        "status": status,
        "text": text,
        "evidence": evidence,
        **extra,
    }


def _mismatch(text: str, evidence: list[str], business: Any) -> dict[str, Any]:
    return _result(
        "mismatch",
        "unmet",
        f"{text} 허가 기관에 확인하기 전에는 이 폐기물을 맡기지 않습니다.",
        evidence,
        business=business,
    )


def permit_level(registry, stream: dict, vendor: dict, today: Any = None) -> dict[str, Any] | None:
    """허가 품목이 맞는 업체의 허가 근거 수준. 허가 품목이 맞지 않거나 없으면 None.

    {"level", "label", "status", "text", "evidence", "business"}. vendor["name"]이 있어야 공공 명부와 대조한다.
    status는 met(registry, document), conditional(self), unmet(mismatch)이다.
    """
    permit = matched_permit(stream, vendor)
    if permit is None:
        return None
    evidence = list(permit.get("evidence") or [])
    has_doc = any(str(e).startswith("doc:") for e in evidence)
    need = needed_business(vendor)
    process = str(vendor.get("process") or "재활용")
    business = permit.get("business")
    if business and not _fits(business, need):
        return _mismatch(
            f"허가증의 업종({business})이 이 업체의 처리 방식({process})에 필요한 {need}업이 아닙니다.",
            evidence,
            business,
        )
    depot = (vendor.get("fleet") or {}).get("depot")
    records = registry_records(vendor.get("name"), depot)
    if records:
        evidence.append(_registry_evidence(registry, records))
        listed = " · ".join(dict.fromkeys(r["business"] for r in records))
        reliable = all(r.get("codesReliable", True) for r in records)
        held = {c for r in records for c in r.get("codes") or []}
        wanted = public_vendors.stream_codes(stream)
        agency = next((r.get("agency") for r in records if r.get("agency")), None)
        if not any(_fits(r["business"], need) for r in records):
            return _mismatch(
                f"공공 허가 명부의 업종({listed})에 이 업체의 처리 방식({process})에 필요한 {need}업이 없습니다.",
                evidence,
                listed,
            )
        if reliable and wanted and not any(c in held for c in wanted):
            return _mismatch(
                f"공공 허가 명부({listed})의 처리 폐기물 분류에 이 폐기물이 없어 허가증과 명부가 다릅니다.",
                evidence,
                listed,
            )
        if reliable and wanted:
            where = f"{josa(agency, '이가')} 올린 " if agency else ""
            tail = (
                " 다만 명부의 기준일이 한 해 넘게 지나 최근 허가 상태는 허가 기관에 확인하세요."
                if _stale(records[0].get("asOf"), today)
                else " 명부의 기준일에 허가 업소로 올라 있었습니다."
            )
            return _result(
                "registry",
                "met",
                f"허가 품목과 {where}공공 허가 명부({listed})의 업종·처리 폐기물 분류가 "
                f"모두 이 폐기물과 맞습니다.{tail}",
                evidence,
                business=listed,
            )
    if has_doc:
        read = (
            f"허가증 문서에서 허가 품목과 업종({business})을 확인했습니다."
            if business
            else "허가증 문서에서 허가 품목을 확인했습니다(업종은 읽지 못했습니다)."
        )
        tail = (
            " 공공 허가 명부와는 대조하지 못했으니 계약 전에 허가 기관에 허가가 유효한지 확인하세요."
            if not records
            else " 공공 허가 명부의 처리 폐기물 정보는 믿기 어려워 대조하지 못했습니다. "
            "계약 전에 허가 기관에 허가가 유효한지 확인하세요."
        )
        return _result("document", "met", read + tail, evidence, business=business)
    return _result(
        "self",
        "conditional",
        "허가 품목은 업체가 입력한 것뿐이고 허가증 원본을 확인하지 않았습니다. "
        "허가증 사본을 받아 이 폐기물과 처리 방법이 들어 있는지 확인해야 합니다.",
        evidence,
        business=business,
    )
