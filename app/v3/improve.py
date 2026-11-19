"""분리 개선 안내: 폐기물마다 무엇을 하면 어느 업체가 얼마에 더 받는지.

2026-11-19 배출 사업장 설문의 자유 답 "사내에서 판매용으로 정확히 분류하기가 어렵다(재질별로 혼입이 생겨 업체가
가져가지 않는다)"에 답한다. 결과 한 장의 최상위 improvements에 싣는다(배출 기업만 본다. 업체 뷰는 최상위 키를 싣지 않는다).

- 할 일(actions)은 흐름의 사실에서 정해진 문장으로 만든다: 섞인 것 떼어 내기, 재질별로 따로 모으기, 오염도·수분 낮추기,
  측정·계량 기록 남기기. 문장에 숫자를 넣지 않는다.
- 더 받는 길(option)은 대화한 업체의 매입 기준 가운데 지금 배정보다 단가가 높고, 오염도·수분 상한만 넘어서 못 받는
  기준이다(분리·세척·건조로 고칠 수 있는 것). 단가와 상한은 매입 기준표 근거가 붙은 claim이고, 한 달 차이는
  (그 기준 단가 − 지금 kg당 손익) × 월 발생량을 calc 근거로 남긴다. 고칠 수 없는 이유(허가 품목, 재질, 형태)는 다루지 않는다.
- 계산은 결정적이다. 실제로 그 등급을 받을지는 반입 검수가 정하므로 화면에서 "검수에서 기준 안으로 확인되면"을 붙인다.
"""

from __future__ import annotations

import logging
from typing import Any

from app.v3 import ids
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.model import Evidence
from app.v3.judge.common import _ranked_criteria, criterion_checks, num, permit_match, quality
from app.v3.store import NotFound
from app.v3.untrusted import safe_text, stream_label

logger = logging.getLogger(__name__)

FIXABLE = {"contaminationPct": "오염도", "moisturePct": "수분"}


def _actions(stream: dict[str, Any], q: dict[str, Any], need: set[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    mixed = [
        safe_text(c.get("name") or c.get("label"))
        for c in stream.get("contaminants") or []
        if not c.get("removed")
    ]
    mixed = [m for m in mixed if m]
    if mixed:
        out.append(
            {
                "key": "remove_contaminants",
                "text": f"섞인 것({', '.join(mixed)})을 떼어 내고 따로 모읍니다. 이물이 붙어 있으면 업체가 단가를 깎거나 받지 않습니다.",
            }
        )
    if "+" in str(stream.get("material") or "") or stream.get("singleMaterial") is False:
        out.append(
            {
                "key": "separate_material",
                "text": "재질이 다른 것을 다른 마대에 따로 모읍니다. 재질이 섞이면 재활용 업체가 매입하지 않습니다.",
            }
        )
    if "contaminationPct" in need:
        out.append(
            {
                "key": "lower_contamination",
                "text": "라벨·테이프·먼지 같은 이물을 줄여 오염도를 매입 기준 상한 안으로 낮춥니다.",
            }
        )
    if "moisturePct" in need:
        out.append(
            {
                "key": "lower_moisture",
                "text": "덮개 아래 보관하거나 말려서 수분을 매입 기준 상한 안으로 낮춥니다.",
            }
        )
    if q.get("contaminationBasis") != "measured" or q.get("moistureBasis") != "measured":
        out.append(
            {
                "key": "measure",
                "text": "오염도와 수분을 한 번 재서 기록해 두면, 조건부 매입이 확정으로 바뀝니다.",
            }
        )
    if stream.get("monthlyKgBasis") != "measured":
        out.append(
            {
                "key": "weigh",
                "text": "내보낼 때마다 무게를 계량해 기록하면 최소 물량 조건을 바로 확인할 수 있습니다.",
            }
        )
    return out


def _split_options(stream, profiles, criterion_claims, pool, names) -> list[dict[str, Any]]:
    """재질이 섞인 흐름(예: PP+ABS)을 재질별로 나누면 받는 곳. 재질마다 기준 하나.

    지금 품질로 맞는 기준이 있으면 그중 단가가 높은 것, 없으면 오염도 상한이 가장 너그러운 것을 고른다(높은 등급
    단가를 약속하지 않게). 나눈 무게 비율을 모르므로 한 달 금액은 계산하지 않고 kg당 단가와 상한만 보인다.
    """
    material = str(stream.get("material") or "")
    if "+" not in material:
        return []
    out = []
    for part in [p.strip() for p in material.split("+") if p.strip()]:
        single = {**stream, "material": part, "singleMaterial": True, "contaminants": []}
        q = quality(single)
        best = None
        for vendor_id, profile in profiles.items():
            if permit_match(single, profile)[0] is not True:
                continue
            for rank, criterion, _ in _ranked_criteria(single, profile, q):
                unmet, conditional, minus_price = rank[:3]
                lenient = -(num(criterion.get("maxContaminationPct")) or 0) if unmet else 0
                key = (unmet, lenient, conditional, minus_price)
                if best is None or key < best[0]:
                    best = (key, vendor_id, criterion)
        item: dict[str, Any] = {
            "material": part,
            "vendorOrgId": None,
            "vendorName": None,
            "grade": None,
            "unitPrice": None,
            "maxContamination": None,
            "meetsNow": False,
        }
        if best is not None:
            key, vendor_id, criterion = best
            fields = criterion_claims.get(vendor_id, {}).get(str(criterion.get("id")), {})
            price = pool.get(fields["unitPrice"]) if fields.get("unitPrice") else None
            limit = (
                pool.get(fields["maxContaminationPct"])
                if fields.get("maxContaminationPct")
                else None
            )
            item.update(
                vendorOrgId=vendor_id,
                vendorName=names.get(vendor_id),
                grade=criterion.get("grade"),
                unitPrice=price.claim.to_dict() if price else None,
                maxContamination=limit.claim.to_dict() if limit else None,
                meetsNow=key[0] == 0,  # 지금 오염도로도 그 기준 안이면 나누기만 하면 된다
            )
        out.append(item)
    return out


def improvements(
    registry: Any,
    store: Any,
    emitter_id: str,
    streams: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    vendor_ids: list[str],
    today: str,
) -> list[dict[str, Any]]:
    """흐름마다 할 일과 더 받는 길. 둘 다 없는 흐름은 싣지 않는다."""
    pool = ClaimPool(registry, emitter_id)
    profiles = {}
    for vendor_id in vendor_ids:
        try:
            profiles[vendor_id] = store.get_vendor_profile(vendor_id)
        except NotFound:  # 지워진 업체는 건너뛴다
            logger.info("분리 개선 안내: 업체 %s 프로필이 없어 건너뜀", vendor_id)
    criterion_claims = {v: pool.add_vendor_profile(p) for v, p in profiles.items()}
    names = {}
    for vendor_id in profiles:
        try:
            names[vendor_id] = store.get_org(vendor_id).get("name")
        except NotFound:
            names[vendor_id] = None
    by_stream = {a["streamId"]: a for a in assignments}
    out: list[dict[str, Any]] = []
    for stream in streams:
        q = quality(stream)
        facts = pool.add_stream(stream, private=True)
        kg_entry = pool.get(facts.get("monthlyKg")) if facts.get("monthlyKg") else None
        assigned = by_stream.get(stream["id"]) or {}
        # 지금 kg당 손익(+매입, −처리비): 배정 단가, 없으면 현행 판매 단가나 현행 처리비의 반대 부호
        base_value, base_evidence = None, []
        unit_price = assigned.get("unitPrice")
        if unit_price and isinstance(unit_price.get("value"), (int, float)):
            base_value, base_evidence = (
                float(unit_price["value"]),
                list(unit_price.get("evidence") or []),
            )
        elif facts.get("currentUnitPrice"):  # 지금 팔고 있다: 받는 단가가 기준
            entry = pool.get(facts["currentUnitPrice"])
            if entry is not None:
                claim = entry.claim.to_dict()
                base_value, base_evidence = float(claim["value"]), list(claim["evidence"])
        elif facts.get("currentUnitCost"):
            entry = pool.get(facts["currentUnitCost"])
            if entry is not None:
                claim = entry.claim.to_dict()
                base_value, base_evidence = -float(claim["value"]), list(claim["evidence"])
        best: dict[str, Any] | None = None
        for vendor_id, profile in profiles.items():
            if permit_match(stream, profile)[0] is not True:
                continue
            for criterion in profile.get("criteria") or []:
                if criterion.get("material") != stream.get("material"):
                    continue
                price = num(criterion.get("unitPriceKrwPerKg"))
                if price is None or base_value is None or price <= base_value:
                    continue
                checks = criterion_checks(stream, criterion, q)
                unmet = [c for c in checks if c[1] == "unmet"]
                if not unmet or any(c[0] not in FIXABLE for c in unmet):
                    continue
                gain = (price - base_value) * float(stream["monthlyKg"]) if kg_entry else None
                if best is not None and (gain or 0) <= (best["gain"] or 0):
                    continue
                best = {
                    "vendorId": vendor_id,
                    "criterion": criterion,
                    "need": {c[0] for c in unmet},
                    "gain": gain,
                }
        need = best["need"] if best else set()
        actions = _actions(stream, q, need)
        option = None
        if best is not None:
            claims = criterion_claims.get(best["vendorId"], {}).get(
                str(best["criterion"].get("id")), {}
            )

            def claim_of(field: str, claims: dict[str, str] = claims) -> dict[str, Any] | None:
                entry = pool.get(claims.get(field)) if claims.get(field) else None
                return entry.claim.to_dict() if entry else None

            price_claim = claim_of("unitPrice")
            gain_claim = None
            if best["gain"] is not None and price_claim and kg_entry:
                kg_claim = kg_entry.claim.to_dict()
                inputs = list(
                    dict.fromkeys([*price_claim["evidence"], *base_evidence, *kg_claim["evidence"]])
                )
                formula = (
                    f"(기준 단가 {price_claim['value']:g}원/kg − 지금 kg당 {base_value:g}원) × "
                    f"월 {float(stream['monthlyKg']):g}kg"
                )
                evidence = registry.put(
                    Evidence(
                        id=ids.calc_id(
                            "improve_gain",
                            {
                                "stream": stream["id"],
                                "criterion": best["criterion"].get("id"),
                                "base": base_value,
                                "kg": stream["monthlyKg"],
                            },
                            "monthlyGain",
                        ),
                        kind="calc",
                        label=f"{stream_label(stream)} 분리 개선 뒤 한 달 차이 계산",
                        value=round(best["gain"]),
                        unit="원/월",
                        excerpt=formula,
                        source="분리 개선 안내",
                        as_of=today,
                        access=(emitter_id,),
                        meta={"tool": "improve_gain", "formula": formula, "inputEvidence": inputs},
                    )
                )
                gain_claim = {
                    "id": f"{stream['id']}_improveGain",
                    "kind": "number",
                    "value": evidence.value,
                    "unit": "원/월",
                    "evidence": [evidence.id, *inputs],
                    "label": "분리 개선 뒤 한 달 차이",
                }
            option = {
                "vendorOrgId": best["vendorId"],
                "vendorName": names.get(best["vendorId"]),
                "grade": best["criterion"].get("grade"),
                "needs": [
                    FIXABLE[k] for k in ("contaminationPct", "moisturePct") if k in best["need"]
                ],
                "unitPrice": price_claim,
                "maxContamination": claim_of("maxContaminationPct")
                if "contaminationPct" in best["need"]
                else None,
                "maxMoisture": claim_of("maxMoisturePct")
                if "moisturePct" in best["need"]
                else None,
                "monthlyGain": gain_claim,
            }
        split = _split_options(stream, profiles, criterion_claims, pool, names)
        if actions or option or split:
            out.append(
                {"streamId": stream["id"], "actions": actions, "option": option, "split": split}
            )
    return out
