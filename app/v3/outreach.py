"""가입하지 않은 처리 업체 후보(공공데이터 허가 명부)와 견적 요청서.

- 진행기가 ④ 업체 찾기에서 가입 업체를 찾은 뒤 흐름마다 find_outside를 부르고, 결과 한 장의 최상위
  outsideVendors에 둔다. 최상위 키라서 업체가 보는 결과에는 실리지 않는다(service._result_view).
- 후보는 AI가 대화하지 않는다. 단가·매입 기준을 모르므로 손익 계산과 배정에 넣지 않고, 담당자가 직접
  견적을 물을 수 있게 허가 정보와 연락처만 보인다.
- 후보마다 공공데이터 한 줄을 ref 근거(누구나 볼 수 있음)로, 직선거리를 calc 근거(배출 기업만 봄)로 남긴다.
- 견적 요청서는 코드 템플릿이다. 숫자는 흐름의 근거가 붙은 값(claim)만 쓰고 근거 검증기를 거친다.
  주소·전화번호는 본문이 아니라 받는 곳 칸(recipient)에 둔다(검증기는 본문의 맨 숫자를 거부한다).
"""

from __future__ import annotations

from typing import Any

from app.v3 import ids, public_vendors
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.model import Evidence
from app.v3.evidence.render import render_text
from app.v3.evidence.validator import ALLOWED_TOKENS, name_tokens, validate_text
from app.v3.judge.common import Recorder, quality
from app.v3.store import NotFound
from app.v3.textutil import josa
from app.v3.untrusted import org_label, stream_label

TOOL = "find_outside_vendors"
ORIGIN_WORDS = {"PIR": "공정에서 나온 것", "PCR": "쓰고 난 뒤 나온 것(포장재 등)"}
BASIS_WORDS = {"measured": "측정값", "photo_estimate": "사진 추정", "answered": "담당자 답"}
CHECK_NOTE = "공공데이터에 적힌 처리 폐기물을 믿기 어려워 상호로 고른 곳입니다. 처리 품목을 먼저 확인해 주세요."


def _ref_id(vendor: dict[str, Any]) -> str:
    return ids.ref_id("허가업체명부", vendor["id"], vendor.get("asOf") or "unknown")


def ref_evidence(vendor: dict[str, Any]) -> Evidence:
    """공공데이터 명부 한 줄. 조문처럼 원문을 그대로 발췌한다."""
    data = public_vendors.registry()
    if vendor.get("codesReliable", True) and vendor["codes"]:
        names = [public_vendors.code_name(c) or c for c in vendor["codes"]]
        waste = "처리 폐기물: " + ", ".join(
            f"{c} {n}" for c, n in zip(vendor["codes"], names, strict=True)
        )
    else:
        waste = "처리 폐기물: 원본에 적힌 값을 믿기 어려움(지자체가 일괄 입력)"
    parts = [vendor["name"], vendor["business"], vendor["address"], waste]
    if vendor.get("permitDate"):
        parts.append(f"허가일 {vendor['permitDate']}")
    if vendor.get("agency"):
        parts.append(f"관리기관 {vendor['agency']}")
    return Evidence(
        id=_ref_id(vendor),
        kind="ref",
        label=f"{vendor['name']} 허가 정보",
        excerpt=" · ".join(parts),
        source=f"{data['source']} · {vendor.get('agency') or '관리기관 미기재'}",
        as_of=vendor.get("asOf") or data["asOfRange"][1],
        meta={
            "url": data["url"],
            "dataset": "15114147",
            "registryVersion": data["version"],
            "codesReliable": bool(vendor.get("codesReliable", True)),
        },
    )


def _registered_names(store: Any) -> tuple[str, ...]:
    names = []
    for profile in store.list_vendor_profiles():
        try:
            names.append(store.get_org(profile["orgId"]).get("name") or "")
        except NotFound:  # 지워진 조직은 건너뛴다
            continue
    return tuple(n for n in names if n)


def find_outside(
    registry: Any, store: Any, emitter: dict[str, Any], streams: list[dict[str, Any]], today: str
) -> dict[str, Any]:
    """흐름마다 가입하지 않은 업체 후보. 결과 한 장의 outsideVendors 모양이다."""
    data = public_vendors.registry()
    location = emitter.get("location")
    origin = (float(location[0]), float(location[1])) if location else None
    registered = _registered_names(store)
    out = []
    for stream in streams:
        found = public_vendors.search(stream, origin, exclude_names=registered)
        rec = Recorder(
            TOOL, registry, emitter["id"], today, {"streamId": stream["id"], "origin": location}
        )
        candidates = []
        for index, item in enumerate(found["candidates"]):
            vendor = public_vendors.get(item["id"])
            ref = registry.put(ref_evidence(vendor)).id
            distance = rec.number(
                f"{stream['id']}_outside{index}_distanceKm",
                item["distanceKm"],
                "km",
                "회사 위치와 업체 주소 좌표 사이의 직선거리입니다. 실제 운반 거리는 이보다 깁니다.",
                [ref],
                label=f"{item['name']}까지 직선거리 계산",
            )
            candidates.append(
                {
                    **{k: v for k, v in item.items() if k != "distanceKm"},
                    "distance": distance,
                    "evidence": [ref],
                    "note": CHECK_NOTE if item["match"] == "check" else None,
                }
            )
        out.append(
            {
                "streamId": stream["id"],
                "codes": found["codes"],
                "candidates": candidates,
                "total": found.get("total", 0),
                "confirmedTotal": found.get("confirmedTotal", 0),
                "reason": found.get("reason"),
            }
        )
    return {
        "source": {
            "name": data["source"],
            "url": data["url"],
            "asOfRange": data.get("asOfRange"),
            "note": data["note"],
            "radiusKm": public_vendors.RADIUS_KM,
        },
        "streams": out,
    }


def quote_request(
    registry: Any, emitter: dict[str, Any], stream: dict[str, Any], vendor_id: str, today: str
) -> dict[str, Any] | None:
    """가입하지 않은 업체 하나에 보낼 견적 요청서 초안. 그 업체가 명부에 없으면 None."""
    vendor = public_vendors.get(vendor_id)
    if vendor is None:
        return None
    ref = registry.put(ref_evidence(vendor)).id
    pool = ClaimPool(registry, emitter["id"])
    facts = pool.add_stream(stream, private=True)
    q = quality(stream)
    claims: list[dict[str, Any]] = []

    def use(field: str, claim_id: str) -> str | None:
        entry = pool.get(facts.get(field)) if facts.get(field) else None
        if entry is None:
            return None
        claims.append({**entry.claim.to_dict(), "id": claim_id})
        return "{{" + claim_id + "}}"

    label = stream_label(stream)
    sender = org_label(emitter.get("name"), "배출 기업")
    reliable = vendor.get("codesReliable", True)
    matched = (
        [c for c in public_vendors.stream_codes(stream) if c in vendor["codes"]] if reliable else []
    )
    business = josa(vendor["business"], "으로")
    if matched:
        waste = (public_vendors.code_name(matched[0]) or "폐합성수지").split("(")[0]
        why = f"귀사가 {josa(waste, '을를')} 처리하는 {business} 올라 있어"
    else:
        why = f"귀사가 {business} 올라 있어"
    kg = use("monthlyKg", "monthlyKg")
    cont_field = "contaminationPct" if facts.get("contaminationPct") else "photoContaminationPct"
    cont = use(cont_field, "contaminationPct")
    moist = use("moisturePct", "moisturePct")
    cont_basis = BASIS_WORDS.get(str(q.get("contaminationBasis")), "")
    moist_basis = BASIS_WORDS.get(str(q.get("moistureBasis")), "")
    lines = [
        "견적 요청서",
        f"받는 곳: {org_label(vendor['name'], '처리 업체')} 담당자님",
        f"보내는 곳: {sender}",
        f"작성일: {today}",
        "",
        f"공공데이터포털에 공개된 폐기물처리업 허가 정보에서 {why} 견적을 여쭙니다.",
        "",
        "폐기물",
        f"- 이름: {label}",
        f"- 재질과 형태: {stream.get('material') or '재질 확인 필요'}, {stream.get('form') or '형태 확인 필요'}",
        f"- 출처: {ORIGIN_WORDS.get(str(stream.get('origin')), '확인 필요')}",
        f"- 한 달 발생량: {kg}" if kg else "- 한 달 발생량: 확인 필요",
        f"- 오염도: {cont}({cont_basis})" if cont else "- 오염도: 확인 필요",
        f"- 수분: {moist}({moist_basis})" if moist else "- 수분: 확인 필요",
        "",
        "여쭙는 것",
        "1. kg당 매입 단가 또는 처리비",
        "2. 수거 가능 요일과 한 번에 받는 최소 물량",
        "3. 반입 검수 기준(오염도·수분 상한)과 필요한 서류",
        "",
        "회신은 아래 연락처로 부탁드립니다.",
    ]
    text = "\n".join(lines)
    allowed = (*ALLOWED_TOKENS, *name_tokens([vendor["name"], emitter.get("name"), label]))
    result = validate_text(
        text, claims, registry, org_id=emitter["id"], today=today, allowed_tokens=allowed
    )
    return {
        "kind": "quote_request",
        "title": f"견적 요청서 · {org_label(vendor['name'], '처리 업체')}",
        "streamId": stream["id"],
        "recipient": {
            "id": vendor["id"],
            "name": vendor["name"],
            "business": vendor["business"],
            "address": vendor["address"],
            "phone": vendor.get("phone"),
            "agency": vendor.get("agency"),
            "asOf": vendor.get("asOf"),
            "evidence": [ref],
        },
        "text": text,
        "claims": claims,
        "rendered": render_text(text, claims),
        "validation": {"ok": result.ok, "rejections": [r.to_dict() for r in result.rejections]},
    }
