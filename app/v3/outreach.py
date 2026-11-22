"""가입하지 않은 처리 업체 후보(공공데이터 허가 명부)와 견적 요청서.

- 진행기가 ④ 업체 찾기에서 가입 업체를 찾은 뒤 흐름마다 find_outside를 부르고, 결과 한 장의 최상위
  outsideVendors에 둔다. 최상위 키라서 업체가 보는 결과에는 실리지 않는다(service._result_view).
- 후보는 AI가 대화하지 않는다. 단가·매입 기준을 모르므로 손익 계산과 배정에 넣지 않고, 담당자가 직접
  견적을 물을 수 있게 허가 정보와 연락처만 보인다.
- 후보마다 공공데이터 한 줄을 ref 근거(누구나 볼 수 있음)로, 직선거리를 calc 근거(배출 기업만 봄)로 남긴다.
- 2024년 처리 실적(「2024 전국 폐기물 발생 및 처리현황」 결과표 한 줄)이 맞는 후보는 처리량과 처리능력을 각각
  ref 근거로 남기고 claim으로 보인다(treatment2024). 표시용이며 손익 계산·배정·견적 요청서 본문에는 쓰지 않는다.
- 견적 요청서는 코드 템플릿이다. 숫자는 흐름의 근거가 붙은 값(claim)만 쓰고 근거 검증기를 거친다.
  주소·전화번호는 본문이 아니라 받는 곳 칸(recipient)에 둔다(검증기는 본문의 맨 숫자를 거부한다).
- 후보는 찾은 때의 흐름 자료로 고른 것이다. 진행기는 그때 본 칸(stream_basis)을 work.outsideBasis에 남기고,
  요청서를 만들기 전에 service가 지금 자료로 후보가 맞는지 다시 본다(public_vendors.recheck). 맞지 않으면
  요청서를 만들지 않고 stale_message로 다시 찾으라고 알린다.
- 요청서는 값마다 출처(계량값·담당자 입력·사진 추정·미확인)를 적고, 거래 방식·단가·방문 수거·최소 수거량·
  운반비와 그 부담·공장이 낼 비용을 넣은 예상 정산을 묻는다. 만들기만 하고 저장·발송·손익 갱신은 하지 않는다.
"""

from __future__ import annotations

from typing import Any

from app.v3 import complex_partners, ids, public_vendors
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceNotFound
from app.v3.evidence.render import render_text, split_text
from app.v3.evidence.validator import ALLOWED_TOKENS, name_tokens, validate_text
from app.v3.judge.common import Recorder, quality
from app.v3.store import NotFound
from app.v3.textutil import josa
from app.v3.untrusted import (
    contaminant_labels,
    form_key,
    material_word,
    org_label,
    safe_text,
    stream_label,
)

TOOL = "find_outside_vendors"
ORIGIN_WORDS = {"PIR": "공정에서 나온 것", "PCR": "쓰고 난 뒤 나온 것(포장재 등)"}
BASIS_WORDS = {
    "measured": "측정값",
    "photo_estimate": "사진 추정",
    "photo_unverified": "사진 추정(검증 안 됨)",
    "answered": "담당자 답",
}
CHECK_NOTE = "공공데이터에 적힌 처리 폐기물을 믿기 어려워 상호로 고른 곳입니다. 처리 품목을 먼저 확인해 주세요."
# 시연 회사(가상 자료)의 요청서 첫 줄. 복사한 글에도 남는다
DEMO_MARK = "※ 시연용 가상 자료입니다. 실제 업체에 보내지 마세요."
INQUIRY_ONLY = "이 글은 조건을 여쭙는 문의이며, 거래나 수거를 정하는 요청이 아닙니다."
BASIS_NOTE = (
    "괄호 안은 값의 출처입니다. 계량값·측정값은 저희가 잰 값이고, 담당자 입력·담당자 답은 담당자가 적은 "
    "값입니다. 사진 추정은 사진으로 어림한 값이라 실제와 다를 수 있습니다. 사진 추정과 미확인 값은 시료나 "
    "반입 검수로 정해 주세요."
)
# 공장이 되레 내야 하는 돈(운반비·상하차비 등)까지 넣은 한 달 정산을 업체가 직접 적게 한다
SETTLEMENT_ASK = (
    "귀사가 주는 금액 ____원 − 저희가 내는 비용 ____원 = 한 달에 저희가 □ 받는 돈 □ 내는 돈 ____원"
)
# 값 출처 -> 묶음. known: 잰 값·담당자가 적은 값, estimated: 사진 추정·추정, unknown: 미확인
FACT_STATUS = {
    "계량값": "known",
    "측정값": "known",
    "담당자 입력": "known",
    "담당자 답": "known",
    "사진 추정": "estimated",
    "추정": "estimated",
    "미확인": "unknown",
}
# 후보를 찾을 때 본 흐름 칸과 보이는 이름. 이 칸이 바뀌면 후보가 맞는지 다시 본다
BASIS_FIELDS = (
    ("material", "재질"),
    ("wasteItem", "품목"),
    ("form", "형태"),
    ("origin", "출처"),
    ("process", "공정"),
    ("name", "이름"),
)
_ORIGIN_SHORT = {"PIR": "공정에서 나온 것", "PCR": "쓰고 난 뒤 나온 것"}
# public_vendors.recheck의 이유 -> 담당자에게 보일 문장
STALE_REASONS = {
    "non_plastic": "지금 자료는 플라스틱이 아니어서, 폐합성수지 허가로 찾은 이 업체와 맞지 않습니다.",
    "unknown_material": "지금 자료에 재질이 없어 이 업체의 허가 품목과 맞는지 알 수 없습니다.",
    "codes": "이 업체의 허가 품목에 지금 재질의 분류가 없습니다.",
    "registered": "이 업체가 가입해 이제 AI가 직접 문의할 수 있습니다.",
    "no_origin": "회사 위치가 없어 업체까지의 거리를 확인할 수 없습니다.",
    "too_far": "지금 회사 위치에서 찾는 반경 밖에 있습니다.",
    "not_listed": "허가 명부에서 이 업체를 찾을 수 없습니다.",
    "deleted": "이 폐기물이 지워졌습니다.",
    "not_in_complex": "지금 회사 정보로는 산업단지를 알 수 없어 같은 단지 후보인지 확인할 수 없습니다.",
}


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


# 결과표 한 줄에서 보이는 값: 후보 칸, 근거 ID 끝말, 단위, 이름
TREATMENT_VALUES = (
    ("tonsPerYear", "처리량", "톤/년", "2024년 처리량"),
    ("capacityTonsPerDay", "처리능력", "톤/일", "하루 처리능력"),
)


def _amount(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def treatment_evidence(vendor: dict[str, Any]) -> dict[str, Evidence]:
    """결과표 한 줄을 값마다 ref 근거로. 조문처럼 그 줄의 칸을 그대로 옮긴다(대표자는 옮기지 않는다).

    근거 ID는 결과표의 시트와 행 번호로 정한다(같은 줄이면 같은 근거). 맞는 줄이 없으면 빈 dict다.
    """
    row = public_vendors.treatment.match(vendor)
    if row is None:
        return {}
    data = public_vendors.treatment.table()
    sheet = row["sheet"]
    parts = [
        row["name"],
        sheet.split(". ", 1)[-1],
        " ".join(p for p in (row["sido"], row["sgg"], row["address"]) if p),
    ]
    if row.get("waste"):
        parts.append(f"영업대상 폐기물 {row['waste']}")
    if row.get("facility"):
        parts.append(f"처리시설 {row['facility']}")
    if row.get("capacityTonsPerDay") is not None:
        parts.append(f"처리능력 {_amount(row['capacityTonsPerDay'])}톤/일")
    if row.get("tonsPerYear") is not None:
        parts.append(f"2024년 처리량 {_amount(row['tonsPerYear'])}톤/년")
    if row.get("permitDate"):
        parts.append(f"허가(승인)일 {row['permitDate']}")
    if row.get("returnedOn"):
        parts.append(f"반납 신고일 {row['returnedOn']}")
    out = {}
    for key, word, unit, label in TREATMENT_VALUES:
        if row.get(key) is None:
            continue
        out[key] = Evidence(
            id=ids.ref_id("처리업체현황", f"{sheet.split('.')[0]}-{row['row']}-{word}", "2024"),
            kind="ref",
            value=row[key],
            unit=unit,
            label=f"{row['name']} {label}",
            excerpt=" · ".join(parts),
            source=f"{data['source']} · {sheet}",
            as_of=data["asOf"],
            meta={
                "url": data["url"],
                "download": data["download"],
                "file": data["file"],
                "sheet": sheet,
                "row": row["row"],
                "year": data["year"],
                "published": data["published"],
                "matchedBy": "상호·시군구(허가 명부와 공통 번호가 없어 상호와 소재지로 맞춤)",
            },
        )
    return out


def _treatment_view(
    registry: Any, vendor: dict[str, Any], item: dict[str, Any], key: str, permit_ref: str
) -> dict[str, Any] | None:
    """후보의 treatment2024를 화면 모양으로. 값은 claim(근거: 결과표 한 줄, 허가 명부 한 줄)이다."""
    summary = item.get("treatment2024")
    if not summary:
        return None
    refs = {k: registry.put(e).id for k, e in treatment_evidence(vendor).items()}
    if "tonsPerYear" not in refs:
        return None
    # 출처 이름·주소·공표일은 결과 한 장의 outsideVendors.source.treatment2024에 한 번만 둔다
    view = {k: summary[k] for k in ("sheet", "row", "returnedOn", "asOf")}
    for field, _, unit, label in TREATMENT_VALUES:
        view[field] = (
            {
                "id": f"{key}_{field}2024",
                "kind": "number",
                "value": summary[field],
                "unit": unit,
                "evidence": [refs[field], permit_ref],
                "label": f"{item['name']} {label}",
            }
            if field in refs
            else None
        )
    view["evidence"] = [refs[field] for field, *_ in TREATMENT_VALUES if field in refs]
    return view


def _ensure_ref(registry: Any, vendor: dict[str, Any], org_id: str) -> str:
    """명부 한 줄의 근거 ID. 이미 있으면 읽기만 한다(체험하기의 읽기 전용 레지스트리에서도 열리게)."""
    evidence_id = _ref_id(vendor)
    try:
        registry.get(evidence_id, org_id)
    except EvidenceNotFound:
        return registry.put(ref_evidence(vendor)).id
    return evidence_id


def _registered_names(store: Any) -> tuple[str, ...]:
    names = []
    for profile in store.list_vendor_profiles():
        try:
            org = store.get_org(profile["orgId"])
            if not org.get("simulation"):
                names.append(org.get("name") or "")
        except NotFound:  # 지워진 조직은 건너뛴다
            continue
    return tuple(n for n in names if n)


def find_outside(
    registry: Any, store: Any, emitter: dict[str, Any], streams: list[dict[str, Any]], today: str
) -> dict[str, Any]:
    """흐름마다 가입하지 않은 업체 후보. 결과 한 장의 outsideVendors 모양이다."""
    data = public_vendors.registry()
    treatment_data = public_vendors.treatment.table()
    location = emitter.get("location")
    origin = (float(location[0]), float(location[1])) if location else None
    registered = _registered_names(store)
    same = complex_partners.same_complex_vendor_ids(emitter)
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
                    # 회사와 같은 산업단지(단지 범위 안이거나 같은 단지 등록공장과 상호가 같음)
                    "sameComplex": item["id"] in same,
                    # 2024년 처리 실적(값은 근거가 열리는 claim). 결과표에서 확실히 맞는 줄이 없으면 None
                    "treatment2024": _treatment_view(
                        registry, vendor, item, f"{stream['id']}_outside{index}", ref
                    ),
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
            # 후보의 2024년 처리 실적 출처(2026-11-22 추가)
            "treatment2024": {
                "name": treatment_data["source"],
                "url": treatment_data["url"],
                "asOf": treatment_data["asOf"],
                "published": treatment_data["published"],
            },
        },
        "streams": out,
    }


def stream_basis(stream: dict[str, Any]) -> dict[str, Any]:
    """후보를 찾을 때 본 흐름 칸(BASIS_FIELDS)."""
    return {key: stream.get(key) for key, _ in BASIS_FIELDS}


def _shown(key: str, value: Any) -> str:
    if value in (None, ""):
        return "빈칸"
    if key == "origin":
        return _ORIGIN_SHORT.get(str(value), str(value))
    return safe_text(value) or str(value)


def basis_changes(before: dict[str, Any] | None, stream: dict[str, Any]) -> list[dict[str, Any]]:
    """후보를 찾은 뒤 바뀐 칸. 찾을 때 남긴 칸이 없으면(이전 진단) 빈 목록이다."""
    if not before:
        return []
    out = []
    for key, label in BASIS_FIELDS:
        old, new = before.get(key) or None, stream.get(key) or None
        if old != new:
            out.append(
                {
                    "field": key,
                    "label": label,
                    "before": _shown(key, old),
                    "after": _shown(key, new),
                }
            )
    return out


def changes_sentence(changes: list[dict[str, Any]]) -> str:
    """ "재질이 PP에서 알루미늄으로, 품목이 폐플라스틱에서 금속 스크랩으로 바뀌었습니다." """
    if not changes:
        return ""
    parts = [
        f"{josa(c['label'], '이가')} {c['before']}에서 {josa(c['after'], '으로')}" for c in changes
    ]
    return ", ".join(parts) + " 바뀌었습니다."


def stale_message(changes: list[dict[str, Any]], reason: str) -> str:
    """지금 자료로는 후보가 아니어서 요청서를 만들지 않을 때 담당자에게 보일 문장."""
    head = f"후보를 찾은 뒤 {changes_sentence(changes)} " if changes else ""
    why = STALE_REASONS.get(reason, "지금 자료로는 이 업체가 후보가 아닙니다.")
    return f"{head}{why} 그래서 견적 요청서를 만들지 않았습니다. 바뀐 자료로 받아 줄 곳을 다시 찾아 주세요."


def quote_request(
    registry: Any,
    emitter: dict[str, Any],
    stream: dict[str, Any],
    vendor_id: str,
    today: str,
    *,
    demo: bool = False,
    changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """가입하지 않은 업체 하나에 보낼 견적 요청서 초안. 그 업체가 명부에 없으면 None.

    stream은 지금 자료다(후보가 맞는지는 부르는 쪽이 먼저 본다). demo면 첫 줄에 DEMO_MARK를 둔다.
    """
    vendor = public_vendors.get(vendor_id)
    if vendor is None:
        return None
    ref = _ensure_ref(registry, vendor, emitter["id"])
    pool = ClaimPool(registry, emitter["id"])
    pooled = pool.add_stream(stream, private=True)
    q = quality(stream)
    claims: list[dict[str, Any]] = []

    def use(field: str, claim_id: str) -> str | None:
        entry = pool.get(pooled.get(field)) if pooled.get(field) else None
        if entry is None:
            return None
        claims.append({**entry.claim.to_dict(), "id": claim_id})
        return "{{" + claim_id + "}}"

    facts: list[dict[str, Any]] = []

    def fact(key: str, name: str, value: str | None, basis: str) -> None:
        if value is None:
            basis = "미확인"
        facts.append(
            {
                "key": key,
                "label": name,
                "value": value,
                "basis": basis,
                "status": FACT_STATUS[basis],
            }
        )

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

    # 값과 그 출처. 담당자가 적은 칸은 상대에게 보일 낱말로 정리한다(untrusted)
    material = material_word(stream.get("material"))
    estimate = (stream.get("photo") or {}).get("materialEstimate") or {}
    if material:
        estimated = stream.get("materialBasis") == "estimated"
        fact("material", "재질", material, "추정" if estimated else "담당자 입력")
    else:
        fact("material", "재질", safe_text(estimate.get("label")) or None, "사진 추정")
    form = form_key(stream.get("form"))
    fact("form", "형태", form, "담당자 입력")
    fact("origin", "출처", ORIGIN_WORDS.get(str(stream.get("origin"))), "담당자 입력")
    kg = use("monthlyKg", "monthlyKg")
    if stream.get("monthlyKg") is None:
        kg_basis = "담당자 답"
    else:
        kg_basis = "계량값" if stream.get("monthlyKgBasis") == "measured" else "추정"
    fact("monthlyKg", "한 달 발생량", kg, kg_basis)
    mixed = contaminant_labels(stream)
    fact("contaminants", "섞인 것", ", ".join(mixed) if mixed else "적힌 것 없음", "담당자 입력")
    cont_field = "contaminationPct" if pooled.get("contaminationPct") else "photoContaminationPct"
    cont = use(cont_field, "contaminationPct")
    fact(
        "contaminationPct",
        "오염도",
        cont,
        BASIS_WORDS.get(str(q.get("contaminationBasis")), "미확인"),
    )
    moist = use("moisturePct", "moisturePct")
    fact("moisturePct", "수분", moist, BASIS_WORDS.get(str(q.get("moistureBasis")), "미확인"))

    def fact_line(item: dict[str, Any]) -> str:
        if item["value"] is None:
            return f"- {item['label']}: 미확인"
        return f"- {item['label']}: {item['value']} ({item['basis']})"

    basis_kg = f"한 달 발생량 {kg} 기준" if kg else "귀사가 정한 기준 물량으로"
    lines = [
        *([DEMO_MARK, ""] if demo else []),
        "견적 요청서",
        f"받는 곳: {org_label(vendor['name'], '처리 업체')} 담당자님",
        f"보내는 곳: {sender}",
        f"작성일: {today}",
        "",
        f"공공데이터포털에 공개된 폐기물처리업 허가 정보에서 {why} 견적을 여쭙니다. {INQUIRY_ONLY}",
        "",
        "문의할 폐기물",
        f"- 품목: {label}",
        *[fact_line(item) for item in facts],
        "",
        BASIS_NOTE,
        "",
        "여쭙는 것 (해당하는 곳에 표시하고 빈칸을 채워 주세요)",
        "1. 거래 방식: □ 매입(귀사가 값을 치름) □ 무상 인수 □ 유상 처리(저희가 처리비를 냄)",
        "2. 단가: kg당 ____원 (매입이면 귀사가 주는 값, 유상 처리면 저희가 내는 값)",
        "3. 방문 수거: □ 가능(요일·주기: ____) □ 불가(저희가 직접 반입)",
        "4. 최소 수거량: 한 번에 ____kg 이상",
        "5. 운반비: □ 단가에 포함 □ 별도(한 번에 ____원, 부담: □ 귀사 □ 저희)",
        "6. 그 밖에 저희가 낼 비용: 상하차·계근·선별·잔재물 처리비 등 ____ (없으면 없음)",
        f"7. 예상 정산({basis_kg}): {SETTLEMENT_ASK}",
        "8. 반입 검수 기준(오염도·수분 상한, 섞인 것을 받는지)과 계약에 필요한 서류",
        "",
        "회신은 아래 연락처로 부탁드립니다.",
    ]
    text = "\n".join(lines)
    allowed = (
        *ALLOWED_TOKENS,
        *name_tokens([vendor["name"], emitter.get("name"), label, material, form, *mixed]),
    )
    result = validate_text(
        text, claims, registry, org_id=emitter["id"], today=today, allowed_tokens=allowed
    )
    monthly = next((c for c in claims if c["id"] == "monthlyKg"), None)
    return {
        "kind": "quote_request",
        "title": f"견적 요청서 · {org_label(vendor['name'], '처리 업체')}",
        "streamId": stream["id"],
        "vendorId": vendor["id"],
        # 시연 회사의 가상 자료면 true. 본문 첫 줄(DEMO_MARK)에도 적혀 복사한 글에 남는다
        "demo": demo,
        "item": {
            "name": label,
            "material": next(f["value"] for f in facts if f["key"] == "material"),
            "form": form,
            "monthlyKg": monthly,
        },
        "facts": facts,
        # 후보를 찾은 뒤 바뀌었지만 지금 자료로도 후보가 맞는 칸(요청서는 지금 자료로 만든다)
        "changes": list(changes or []),
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
        # 화면 미리보기용 조각. 글자 조각과 claim의 display를 이으면 rendered와 같다
        "parts": split_text(text, claims),
        "validation": {"ok": result.ok, "rejections": [r.to_dict() for r in result.rejections]},
    }
