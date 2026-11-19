"""허가 근거의 수준. 허가 품목이 맞는 업체의 허가를 무엇으로 확인했는지 가른다.

| 수준 | 뜻 | 판정 |
|---|---|---|
| registry | 허가 품목에 더해 공공 허가 명부(전국폐기물처리업소표준데이터)에서 같은 상호의 재활용업 허가와 처리 폐기물 분류번호가 이 폐기물과 맞는다 | 충족 |
| document | 허가증 문서에서 읽은 허가 품목이다. 공공 명부에서 이 업체를 찾지 못해 대조하지 못했다 | 충족(계약 전에 허가 기관에 확인하라고 권한다) |
| self | 업체 담당자가 입력한 허가 품목뿐이다(허가증 원본 확인 전) | 조건부 |
| mismatch | 공공 명부에 이 업체가 있는데 처리 폐기물 분류번호에 이 폐기물이 없다 | 조건부 |

허가 품목이 맞는지(permit_match)는 대화를 열지 정하는 문턱이고, 이 수준은 그 허가를 얼마나 믿을 수 있는지다.
self와 mismatch는 최종 배정과 법령 요건(받는 업체의 허가)을 조건부로 내린다. 문장에는 숫자를 쓰지 않는다
(분류번호와 허가일은 근거의 원문에 있다).
"""

from __future__ import annotations

from typing import Any

from app.v3 import ids, public_vendors
from app.v3.evidence.model import Evidence
from app.v3.judge.common import haversine_km, num, permit_match

LEVELS = ("registry", "document", "self", "mismatch")
LABELS = {
    "registry": "허가증·공공 명부 확인",
    "document": "허가증 문서",
    "self": "업체 입력",
    "mismatch": "공공 명부와 다름",
}
SAME_PLACE_KM = 50.0  # 같은 상호라도 업체 차고지에서 이보다 먼 사업장은 다른 곳으로 본다
REGISTRY_SERIES = "허가명부"


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
            f"처리 폐기물 분류번호 {', '.join(named) or '없음'}"
        ),
        source=f"{data['source']} ({data['url']})",
        as_of=first.get("asOf") or data["asOfRange"][-1],
        meta={
            "series": REGISTRY_SERIES,
            "registryIds": [r["id"] for r in records],
            "codes": codes,
            "codesReliable": all(r.get("codesReliable", True) for r in records),
        },
    )
    return registry.put(evidence).id


def permit_level(registry, stream: dict, vendor: dict) -> dict[str, Any] | None:
    """허가 품목이 맞는 업체의 허가 근거 수준. 허가 품목이 맞지 않거나 없으면 None.

    {"level", "label", "status", "text", "evidence"}. vendor["name"]이 있어야 공공 명부와 대조한다.
    """
    matched, evidence = permit_match(stream, vendor)
    if matched is not True:
        return None
    evidence = list(evidence)
    has_doc = any(str(e).startswith("doc:") for e in evidence)
    depot = (vendor.get("fleet") or {}).get("depot")
    records = registry_records(vendor.get("name"), depot)
    if records:
        evidence.append(_registry_evidence(registry, records))
        business = " · ".join(dict.fromkeys(r["business"] for r in records))
        reliable = all(r.get("codesReliable", True) for r in records)
        held = {c for r in records for c in r.get("codes") or []}
        wanted = public_vendors.stream_codes(stream)
        if reliable and wanted and not any(c in held for c in wanted):
            return {
                "level": "mismatch",
                "label": LABELS["mismatch"],
                "status": "conditional",
                "text": (
                    f"공공 허가 명부({business})의 처리 폐기물 분류에 이 폐기물이 없습니다. "
                    "허가증과 명부가 달라 맡기기 전에 허가를 확인해야 합니다."
                ),
                "evidence": evidence,
            }
        if reliable and wanted:
            return {
                "level": "registry",
                "label": LABELS["registry"],
                "status": "met",
                "text": f"허가 품목과 공공 허가 명부({business})의 처리 폐기물 분류가 모두 이 폐기물과 맞습니다.",
                "evidence": evidence,
            }
    if has_doc:
        return {
            "level": "document",
            "label": LABELS["document"],
            "status": "met",
            "text": (
                "허가증 문서에서 허가 품목을 확인했습니다. 공공 허가 명부와는 대조하지 못했으니 "
                "계약 전에 허가 기관에 허가가 유효한지 확인하세요."
                if not records
                else "허가증 문서에서 허가 품목을 확인했습니다. 공공 허가 명부의 처리 폐기물 정보는 "
                "믿기 어려워 대조하지 못했습니다. 계약 전에 허가 기관에 허가가 유효한지 확인하세요."
            ),
            "evidence": evidence,
        }
    return {
        "level": "self",
        "label": LABELS["self"],
        "status": "conditional",
        "text": (
            "허가 품목은 업체가 입력한 것뿐이고 허가증 원본을 확인하지 않았습니다. "
            "허가증 사본을 받아 이 폐기물이 들어 있는지 확인해야 합니다."
        ),
        "evidence": evidence,
    }
