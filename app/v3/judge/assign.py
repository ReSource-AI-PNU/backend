"""⑥ 흐름과 업체 배정. 회사 측 월 순비용을 최소화한다."""

from __future__ import annotations

import itertools
import math

from app.v3.judge.common import Recorder, best_criterion, num, permit_match, quality
from app.v3.judge.route import current_cost

MAX_COMBINATIONS = 200_000


def _carbon_per_kg(rec: Recorder, item: str):
    now_ef, after_ef = (
        rec.basis(f"emission.incinerate.{item}"),
        rec.basis(f"emission.recycle.{item}"),
    )
    if now_ef is None or after_ef is None:
        return None, []
    return (float(after_ef.value) - float(now_ef.value)) / 1000, [now_ef.id, after_ef.id]


def optimize_assignment(registry, *, org_id, today, streams, vendors, carbonWeight=0, exclude=None):
    """exclude는 [{"streamId", "vendorOrgId"}]. 업체가 거절했거나 제안하지 않은 짝을 뺀다."""
    orgs = sorted({str(s.get("orgId")) for s in streams})
    if len(orgs) > 1:
        raise ValueError("한 번에 한 배출 기업의 흐름만 배정할 수 있다")
    if org_id is not None and orgs and orgs[0] != org_id:
        raise ValueError("다른 조직의 흐름은 배정할 수 없다")
    weight = num(carbonWeight) or 0.0
    vendors = sorted(vendors, key=lambda v: str(v.get("orgId")))  # 입력 순서가 달라도 같은 근거 ID
    args = {"streams": streams, "vendors": vendors, "carbonWeight": weight}
    excluded = sorted({(str(x.get("streamId")), str(x.get("vendorOrgId"))) for x in exclude or []})
    if excluded:
        args["exclude"] = [list(pair) for pair in excluded]
    rec = Recorder("optimize_assignment", registry, org_id, today, args)
    vendors = sorted(vendors, key=lambda v: str(v.get("orgId")))
    output = {"assignments": [], "totals": {}, "carbonInObjective": weight > 0, "skipped": []}

    usable, options = [], []
    for stream in streams:
        kg = num(stream.get("monthlyKg"))
        if kg is None or kg <= 0:
            output["skipped"].append(
                {"streamId": stream.get("id"), "reason": "월 발생량 확인 필요"}
            )
            rec.check(
                f"monthly_kg:{stream.get('id')}",
                "conditional",
                "월 발생량을 몰라 배정에서 뺐습니다.",
            )
            continue
        cost, cost_ev, assumed, _ = current_cost(rec, stream)
        if cost is None:
            output["skipped"].append(
                {"streamId": stream.get("id"), "reason": "현행 처리 단가 확인 필요"}
            )
            rec.check(
                f"current_cost:{stream.get('id')}",
                "conditional",
                "현행 처리 단가가 없어 배정에서 뺐습니다.",
            )
            continue
        carbon, carbon_ev = _carbon_per_kg(rec, str(stream.get("wasteItem")))
        q = quality(stream)
        choices = [
            {"vendor": None, "value": -cost * kg, "ev": cost_ev, "criterion": None, "carbon": 0.0}
        ]
        for vendor in vendors:
            if (str(stream.get("id")), str(vendor.get("orgId"))) in excluded:
                continue
            if permit_match(stream, vendor)[0] is not True:
                continue
            criterion, checks = best_criterion(stream, vendor, q)
            if criterion is None or any(c[1] == "unmet" for c in checks):
                continue
            choices.append(
                {
                    "vendor": vendor,
                    "value": float(criterion["unitPriceKrwPerKg"]) * kg,
                    "ev": list(criterion.get("evidence") or []),
                    "criterion": criterion,
                    "carbon": (carbon or 0.0) * kg,
                    "conditional": any(c[1] == "conditional" for c in checks),
                }
            )
        usable.append(
            {
                "stream": stream,
                "kg": kg,
                "cost": cost,
                "cost_ev": cost_ev,
                "assumed": assumed,
                "carbon_ev": carbon_ev,
            }
        )
        options.append(choices)

    if not usable:
        return rec.result("needs_information", output)
    if math.prod(len(o) for o in options) > MAX_COMBINATIONS:
        raise ValueError("배정 조합이 너무 많다. 흐름이나 업체 수를 줄인다")

    def feasible(combo) -> bool:
        groups: dict[str, list[int]] = {}
        for i, choice in enumerate(combo):
            if choice["vendor"] is not None:
                groups.setdefault(str(choice["vendor"].get("orgId")), []).append(i)
        for members in groups.values():
            vendor = combo[members[0]]["vendor"]
            total = math.fsum(usable[i]["kg"] for i in members)
            mins = [num(combo[i]["criterion"].get("minMonthlyKg")) or 0.0 for i in members]
            if total + 1e-9 < max(mins):
                return False
            fleet = vendor.get("fleet") or {}
            pickups = num((vendor.get("guardrails") or {}).get("minPickupsPerMonth"))
            cap_kg, cap_m3 = num(fleet.get("capacityKg")), num(fleet.get("capacityM3"))
            if pickups and cap_kg is not None and total / pickups > cap_kg + 1e-9:
                return False
            dens = [num(usable[i]["stream"].get("bulkDensityKgPerM3")) for i in members]
            if pickups and cap_m3 is not None and all(d for d in dens):
                volume = math.fsum(usable[i]["kg"] / d for i, d in zip(members, dens, strict=True))
                if volume / pickups > cap_m3 + 1e-9:
                    return False
        return True

    best, best_key = None, None
    for combo in itertools.product(*options):
        if not feasible(combo):
            continue
        money = math.fsum(c["value"] for c in combo)
        carbon = math.fsum(c["carbon"] for c in combo)
        objective = money - weight * carbon
        names = tuple(str(c["vendor"].get("orgId")) if c["vendor"] else "" for c in combo)
        key = (-round(objective, 6), names)  # 동점이면 업체 ID 순서가 앞선 배정
        if best_key is None or key < best_key:
            best, best_key = combo, key

    now_total = after_total = carbon_total = 0.0
    carbon_known = True
    for i, (item, choice) in enumerate(zip(usable, best, strict=True)):
        stream, kg = item["stream"], item["kg"]
        now = -item["cost"] * kg
        tag = f"s{i}"
        rec.number(f"{tag}MonthlyNow", now, "원/월", "−현행 처리비 × 월 발생량", item["cost_ev"])
        rec.number(
            f"{tag}MonthlyAfter", choice["value"], "원/월", "배정한 경로의 월 손익", choice["ev"]
        )
        rec.number(
            f"{tag}Difference",
            choice["value"] - now,
            "원/월",
            "배정 뒤 월 손익 − 현행 월 손익",
            [*choice["ev"], *item["cost_ev"]],
        )
        entry = {
            "streamId": stream.get("id"),
            "vendorOrgId": choice["vendor"].get("orgId") if choice["vendor"] else None,
            "route": "keep"
            if choice["vendor"] is None
            else ("sell" if choice["value"] >= 0 else "treat"),
            "criterionId": choice["criterion"].get("id") if choice["criterion"] else None,
            "status": "conditional" if choice.get("conditional") else "calculated",
            "monthlyNow": f"{tag}MonthlyNow",
            "monthlyAfter": f"{tag}MonthlyAfter",
            "difference": f"{tag}Difference",
            "currentAssumed": item["assumed"],
            "carbon": None,
        }
        if choice["vendor"] is not None:
            if item["carbon_ev"]:
                entry["carbon"] = rec.number(
                    f"{tag}Carbon",
                    choice["carbon"],
                    "tCO2e/월",
                    "(재활용 계수 − 소각 계수) × 월 발생량",
                    item["carbon_ev"],
                )["id"]
                carbon_total += choice["carbon"]
            else:
                carbon_known = False
        output["assignments"].append(entry)
        now_total, after_total = now_total + now, after_total + choice["value"]
    rec.number("totalNow", now_total, "원/월", "흐름별 현행 월 손익의 합")
    rec.number("totalAfter", after_total, "원/월", "흐름별 배정 뒤 월 손익의 합")
    rec.number("totalDifference", after_total - now_total, "원/월", "배정 뒤 합 − 현행 합")
    output["totals"] = {
        "monthlyNow": "totalNow",
        "monthlyAfter": "totalAfter",
        "difference": "totalDifference",
        "carbon": None,
    }
    if carbon_known and any(a["carbon"] for a in output["assignments"]):
        output["totals"]["carbon"] = rec.number(
            "totalCarbon", carbon_total, "tCO2e/월", "흐름별 탄소 차이의 합"
        )["id"]
    elif not carbon_known:
        rec.check(
            "carbon",
            "conditional",
            "배출계수가 없는 품목이 있어 탄소 합계를 내지 않았습니다. 확인 필요.",
        )
    if any(a["currentAssumed"] for a in output["assignments"]):
        rec.check(
            "current_assumed",
            "met",
            "현행 처리를 입력하지 않은 흐름은 혼합 소각 가정값을 썼습니다.",
        )
    conditional = output["skipped"] or any(
        a["status"] == "conditional" for a in output["assignments"]
    )
    return rec.result("conditional" if conditional else "calculated", output)
