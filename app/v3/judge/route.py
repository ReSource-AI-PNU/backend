"""② 경로 판단, 최소 물량과 적재 검사."""

from __future__ import annotations

import math

from app.v3.judge import defaults
from app.v3.judge.common import (
    Recorder,
    ambiguous_forms,
    best_criterion,
    num,
    permit_match,
    quality,
    stream_cautions,
)


def current_cost(rec: Recorder, stream: dict):
    """(kg당 처리비, 근거 ID, 가정 여부, 방식). 입력이 없으면 혼합 소각 가정값."""
    current = stream.get("currentTreatment") or {}
    cost = num(current.get("unitCostKrwPerKg"))
    if cost is not None:
        return cost, list(current.get("evidence") or []), False, current.get("method") or "현행"
    assumed = rec.basis(f"current.cost.{stream.get('wasteItem')}")
    if assumed is None:
        return None, [], True, "혼합 소각"
    return float(assumed.value), [assumed.id], True, "혼합 소각"


def baseline(rec: Recorder, stream: dict, kg: float, after: float | None, prefix=""):
    """현행 월 손익과 차이, 현행이 가정값이면 민감도까지 claim으로 남긴다."""
    cost, cost_ev, assumed, method = current_cost(rec, stream)
    info = {"method": method, "assumed": assumed, "sensitivity": []}
    if cost is None:
        rec.check(
            "current_cost", "conditional", "현행 처리 단가가 없어 현행 대비를 내지 못했습니다."
        )
        return info, None
    # 현행 처리 단가와 거기서 나온 값은 상대 업체에 열지 않는다
    rec.number(
        f"{prefix}currentUnitCost",
        cost,
        "원/kg",
        f"현행({method}) kg당 처리비",
        cost_ev,
        private=True,
    )
    now = -cost * kg
    rec.number(
        f"{prefix}monthlyNow", now, "원/월", "−현행 처리비 × 월 발생량", cost_ev, private=True
    )
    if assumed:
        rec.check(
            "current_assumed",
            "met",
            "현행 처리를 입력하지 않아 혼합 소각 가정값을 썼습니다. 가정값입니다.",
            None,
            cost_ev,
        )
    if after is not None:
        rec.number(
            f"{prefix}difference",
            after - now,
            "원/월",
            "추천 경로 월 손익 − 현행 월 손익",
            cost_ev,
            private=True,
        )
        if assumed:
            for i, scenario in enumerate(defaults.sensitivity_scenarios()):
                alt = rec.basis(scenario["key"])
                if alt is None:
                    continue
                claim = rec.number(
                    f"{prefix}sensitivity{chr(ord('A') + i)}",
                    after + float(alt.value) * kg,
                    "원/월",
                    f"{scenario['label']}의 현행 대비 차이",
                    [alt.id],
                    private=True,
                )
                info["sensitivity"].append({"label": scenario["label"], "difference": claim["id"]})
    return info, now


def judge_route(registry, *, org_id, today, stream, vendor=None):
    share = [vendor.get("orgId")] if vendor else []
    rec = Recorder(
        "judge_route", registry, org_id, today, {"stream": stream, "vendor": vendor}, share
    )
    output = {
        "streamId": stream.get("id"),
        "vendorOrgId": vendor.get("orgId") if vendor else None,
        "route": None,
        "criterionId": None,
        "acceptable": None,
        "reason": "",
        "current": None,
    }
    kg = num(stream.get("monthlyKg"))
    if kg is None or kg <= 0:
        rec.check(
            "monthly_kg",
            "conditional",
            "월 발생량을 몰라 금액을 계산하지 않았습니다.",
            None,
        )
        output["reason"] = "발생량 확인이 필요합니다. 계량 기록을 확보하면 다시 계산합니다."
        return rec.result("needs_information", output)
    rec.number(
        "monthlyKg", kg, "kg/월", "입력한 월 발생량", (stream.get("measure") or {}).get("evidence")
    )
    if vendor is None:
        output["current"], _ = baseline(rec, stream, kg, None)
        rec.check("vendor", "conditional", "대조할 업체 매입 기준이 없습니다.")
        output["reason"] = "업체 매입 기준이 있어야 경로를 판정할 수 있습니다."
        return rec.result("needs_information", output)

    matched, permit_ev = permit_match(stream, vendor)
    if matched is False:
        rec.check(
            "permit", "unmet", "업체의 허가 품목에 이 흐름의 품목·재질이 없습니다.", None, permit_ev
        )
        output.update(
            acceptable=False, route="keep", reason="허가 품목이 맞지 않아 현행을 유지합니다."
        )
        output["current"], _ = baseline(rec, stream, kg, None)
        return rec.result("calculated", output)
    if matched is None:
        rec.check(
            "permit",
            "conditional",
            "업체 허가 품목이 올라와 있지 않습니다.",
            "이 품목과 재질이 허가 품목에 들어 있는지 허가증으로 확인해 주세요.",
        )
    else:
        rec.check("permit", "met", "허가 품목이 맞습니다.", None, permit_ev)

    q = quality(stream)
    by_form = ambiguous_forms(stream, vendor, q)
    if by_form:
        # 형태를 모르는데 형태마다 기준이 다르다. 한 형태(가장 비싼 기준)를 임의로 고르지 않는다
        forms = ", ".join(dict.fromkeys(str(c.get("form")) for c in by_form))
        rec.check(
            "form",
            "conditional",
            "흐름의 형태가 확인되지 않았고, 형태에 따라 매입 기준이 다릅니다.",
            f"형태를 모르는 상태입니다. 매입 기준이 형태({forms})에 따라 다릅니다. 형태를 확인하는 "
            "방법이나 형태와 관계없이 받는 조건이 있는지 알려 주세요.",
            [e for c in by_form for e in (c.get("evidence") or [])] + q["evidence"],
        )
        for key, status, text, ask in stream_cautions(stream):
            rec.check(key, status, text, ask)
        output["reason"] = "형태에 따라 매입 기준이 달라, 형태를 확인한 뒤에 단가를 정합니다."
        output["current"], _ = baseline(rec, stream, kg, None)
        return rec.result("conditional", output)
    criterion, checks = best_criterion(stream, vendor, q)
    if criterion is None:
        rec.check("criteria", "unmet", "이 재질의 매입 기준이 없습니다.")
        output.update(acceptable=False, route="keep", reason="매입 기준이 없어 현행을 유지합니다.")
        output["current"], _ = baseline(rec, stream, kg, None)
        return rec.result("calculated", output)
    crit_ev = list(criterion.get("evidence") or [])
    for key, status, text, ask in checks:
        rec.check(key, status, text, ask, crit_ev + q["evidence"])
    for key, status, text, ask in stream_cautions(stream):
        rec.check(key, status, text, ask)
    output["criterionId"] = criterion.get("id")
    if any(c["status"] == "unmet" for c in rec.checks):
        output.update(
            acceptable=False, route="keep", reason="매입 기준을 넘는 항목이 있어 현행을 유지합니다."
        )
        output["current"], _ = baseline(rec, stream, kg, None)
        return rec.result("calculated", output)

    price = float(criterion["unitPriceKrwPerKg"])
    rec.number("unitPrice", price, "원/kg", "업체 매입 기준표의 단가(음수면 처리비)", crit_ev)
    after = price * kg
    rec.number("monthlyAfter", after, "원/월", "단가 × 월 발생량 (운송비 제외)", crit_ev)
    output["current"], _ = baseline(rec, stream, kg, after)
    output.update(acceptable=True, route="sell" if price >= 0 else "treat")
    conditional = any(c["status"] == "conditional" for c in rec.checks)
    output["reason"] = (
        "확인할 것이 남아 조건부입니다. 업체 답변을 받으면 확정합니다."
        if conditional
        else "매입 기준을 모두 충족합니다."
    )
    return rec.result("conditional" if conditional else "calculated", output)


def check_loading(registry, *, org_id, today, streams, vendor, pickupsPerMonth=None):
    """출처(PIR·PCR)를 보지 않는다. 같은 공장의 흐름을 합친 뒤 최소 물량과 적재를 검사한다."""
    orgs = sorted({str(s.get("orgId")) for s in streams})
    if len(orgs) > 1:
        raise ValueError("한 번에 한 공장의 흐름만 합칠 수 있다")
    if org_id is not None and orgs and orgs[0] != org_id and vendor.get("orgId") != org_id:
        raise ValueError("다른 조직의 흐름은 검사할 수 없다")
    args = {"streams": streams, "vendor": vendor, "pickups": pickupsPerMonth}
    rec = Recorder("check_loading", registry, org_id, today, args, [vendor.get("orgId"), *orgs])
    output = {"vendorOrgId": vendor.get("orgId"), "streamIds": [s.get("id") for s in streams]}
    kgs = [num(s.get("monthlyKg")) for s in streams]
    if not streams or any(k is None or k <= 0 for k in kgs):
        rec.check(
            "monthly_kg", "conditional", "월 발생량을 모르는 흐름이 있어 검사하지 못했습니다."
        )
        return rec.result("needs_information", output)
    total = math.fsum(kgs)
    rec.number("totalKg", total, "kg/월", "같은 업체로 가는 흐름의 월 발생량 합")

    materials = {s.get("material") for s in streams}
    crits = [c for c in vendor.get("criteria") or [] if c.get("material") in materials]
    mins = [
        (num(c.get("minMonthlyKg")), c) for c in crits if num(c.get("minMonthlyKg")) is not None
    ]
    min_kg, min_ev = None, []
    if mins:
        min_kg, crit = max(mins, key=lambda m: (m[0], str(m[1].get("id"))))
        min_ev = list(crit.get("evidence") or [])
    else:
        items = sorted({str(s.get("wasteItem")) for s in streams})
        found = [rec.basis(f"min.monthlyKg.{item}") for item in items]
        found = [f for f in found if f is not None]
        if found:
            top = max(found, key=lambda f: (float(f.value), f.id))
            min_kg, min_ev = float(top.value), [top.id]
    if min_kg is None:
        rec.check(
            "min_volume",
            "conditional",
            "최소 물량 기준이 없습니다.",
            "최소 수거 물량을 알려 주세요.",
        )
    else:
        rec.number("minMonthlyKg", min_kg, "kg/월", "업체 최소 물량(없으면 가정값)", min_ev)
        if total >= min_kg:
            rec.check("min_volume", "met", "합친 물량이 최소 물량 이상입니다.", None, min_ev)
        else:
            rec.check(
                "min_volume",
                "conditional",
                "합친 물량이 최소 물량에 못 미칩니다.",
                "최소 물량에 못 미칩니다. 기존 노선에 묶어 수거할 수 있는지 알려 주세요.",
                min_ev,
            )

    fleet = vendor.get("fleet") or {}
    fleet_ev = list(fleet.get("evidence") or [])
    pickups = num(pickupsPerMonth) or num(
        (vendor.get("guardrails") or {}).get("minPickupsPerMonth")
    )
    cap_kg, cap_m3 = num(fleet.get("capacityKg")), num(fleet.get("capacityM3"))
    densities = [num(s.get("bulkDensityKgPerM3")) for s in streams]
    if not pickups or cap_kg is None:
        rec.check(
            "loading",
            "conditional",
            "차량 용량이나 수거 횟수가 없습니다.",
            "차량 적재 용량과 월 수거 횟수를 알려 주세요.",
        )
    else:
        rec.number("pickupsPerMonth", pickups, "회/월", "월 수거 횟수", fleet_ev)
        per_kg = total / pickups
        rec.number("perPickupKg", per_kg, "kg", "합친 월 물량 ÷ 월 수거 횟수", fleet_ev)
        rec.number("capacityKg", cap_kg, "kg", "차량 적재 중량", fleet_ev)
        ok_weight = per_kg <= cap_kg
        if cap_m3 is None or any(d is None or d <= 0 for d in densities):
            ok_volume = None
            rec.check(
                "loading_volume",
                "conditional",
                "겉보기 밀도나 차량 부피 용량이 없어 부피 적재를 확인하지 못했습니다.",
                "한 번에 실을 수 있는 부피와 압축·포장 조건을 알려 주세요.",
                fleet_ev,
            )
        else:
            volume = math.fsum(k / d for k, d in zip(kgs, densities, strict=True))
            rec.number("totalM3", volume, "m3", "흐름별 월 발생량 ÷ 겉보기 밀도의 합")
            per_m3 = volume / pickups
            rec.number("perPickupM3", per_m3, "m3", "합친 월 부피 ÷ 월 수거 횟수", fleet_ev)
            rec.number("capacityM3", cap_m3, "m3", "차량 적재 부피", fleet_ev)
            ok_volume = per_m3 <= cap_m3
        if ok_weight and ok_volume is not False:
            rec.check(
                "loading", "met", "한 번 수거량이 차량 중량 용량 안에 있습니다.", None, fleet_ev
            )
        else:
            rec.check(
                "loading",
                "conditional",
                "한 번 수거량이 차량 용량을 넘습니다.",
                "한 번에 싣기 어렵습니다. 수거 횟수를 늘릴 수 있는지 알려 주세요.",
                fleet_ev,
            )
    conditional = any(c["status"] != "met" for c in rec.checks)
    return rec.result("conditional" if conditional else "calculated", output)
