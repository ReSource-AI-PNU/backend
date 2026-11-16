"""⑥ 흐름과 업체 배정. 회사 측 월 순비용을 최소화한다."""

from __future__ import annotations

import itertools
import logging
import math

from app.v3 import names
from app.v3.judge.common import Recorder, best_criterion, num, permit_match, quality
from app.v3.judge.route import current_cost

try:  # 없으면 순수 파이썬 완전 탐색으로만 돈다
    from ortools.sat.python import cp_model  # noqa: F401

    HAS_ORTOOLS = True
except Exception:  # noqa: BLE001
    HAS_ORTOOLS = False

logger = logging.getLogger(__name__)

MAX_COMBINATIONS = 200_000
SOLVER_SCALE = 1000  # CP-SAT는 정수만 받는다: kg→g, m3→L, 원→1/1000원
_AUTO = object()


def _name(choice) -> str:
    return str(choice["vendor"].get("orgId")) if choice["vendor"] else ""


def cp_sat_pick(usable, options, weight):
    """CP-SAT로 흐름마다 고를 선택지의 번호를 구한다. 못 풀면 None.

    로컬 미실행: 작성한 환경에서는 ortools를 불러올 수 없었다. 결과는 optimize_assignment가
    순수 파이썬 검사(feasible)와 완전 탐색으로 다시 확인한 뒤에만 쓴다.

    완전 탐색과 같은 규칙이다: 업체별 합계 물량이 고른 매입 기준의 최소 물량 이상, 회당 무게와
    부피가 차량 용량 이하(부피는 밀도를 모르는 흐름이 섞이면 보지 않는다), 목적은
    월 손익 − 가중치 × 탄소의 최대화, 동점이면 업체 ID 순서가 앞선 배정.
    """
    from ortools.sat.python import cp_model

    grams = [round(item["kg"] * SOLVER_SCALE) for item in usable]
    by_vendor: dict[str, list[tuple[int, int]]] = {}
    for i, choices in enumerate(options):
        for j, choice in enumerate(choices):
            if choice["vendor"] is not None:
                by_vendor.setdefault(_name(choice), []).append((i, j))
    gain = [
        [round((c["value"] - weight * c["carbon"]) * SOLVER_SCALE) for c in choices]
        for choices in options
    ]

    def build():
        model = cp_model.CpModel()
        x = [
            [model.new_bool_var(f"x_{i}_{j}") for j in range(len(choices))]
            for i, choices in enumerate(options)
        ]
        for row in x:
            model.add(sum(row) == 1)
        for members in by_vendor.values():
            vendor = options[members[0][0]][members[0][1]]["vendor"]
            total = sum(grams[i] * x[i][j] for i, j in members)
            for i, j in members:
                minimum = num(options[i][j]["criterion"].get("minMonthlyKg")) or 0.0
                if minimum > 0:
                    # 이 선택지를 고르면(x=1) 합계가 최소 물량 이상이어야 한다
                    model.add(total >= math.ceil(minimum * SOLVER_SCALE - 1e-6) * x[i][j])
            fleet = vendor.get("fleet") or {}
            pickups = num((vendor.get("guardrails") or {}).get("minPickupsPerMonth"))
            cap_kg, cap_m3 = num(fleet.get("capacityKg")), num(fleet.get("capacityM3"))
            if pickups and cap_kg is not None:
                model.add(total <= math.floor(cap_kg * pickups * SOLVER_SCALE + 1e-6))
            if pickups and cap_m3 is not None:
                density = {
                    i: num(usable[i]["stream"].get("bulkDensityKgPerM3")) for i, _ in members
                }
                liters = {
                    i: round(usable[i]["kg"] / density[i] * SOLVER_SCALE)
                    for i, _ in members
                    if density[i]
                }
                unknown = [x[i][j] for i, j in members if not density[i]]
                volume = sum(liters[i] * x[i][j] for i, j in members if i in liters)
                limit = math.floor(cap_m3 * pickups * SOLVER_SCALE + 1e-6)
                # 밀도를 모르는 흐름이 하나라도 들어가면 부피 검사를 하지 않는다(완전 탐색과 같다)
                if liters and unknown:
                    model.add(volume <= limit + sum(liters.values()) * sum(unknown))
                elif liters:
                    model.add(volume <= limit)
        return model, x

    def solve(model):
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1  # 같은 입력이면 같은 답
        return solver if solver.solve(model) == cp_model.OPTIMAL else None

    def objective(x):
        return sum(gain[i][j] * x[i][j] for i in range(len(options)) for j in range(len(x[i])))

    model, x = build()
    model.maximize(objective(x))
    solver = solve(model)
    if solver is None:
        return None
    best = sum(
        gain[i][j] for i in range(len(options)) for j in range(len(x[i])) if solver.value(x[i][j])
    )
    picked: list[int] = []
    for i, choices in enumerate(options):  # 동점 정리: 앞 흐름부터 업체 ID가 앞선 것을 고정한다
        if len(choices) == 1:
            picked.append(0)
            continue
        order = sorted({_name(c) for c in choices})
        model, x = build()
        model.add(objective(x) == best)
        for done, j in enumerate(picked):
            model.add(x[done][j] == 1)
        model.minimize(sum(order.index(_name(c)) * x[i][j] for j, c in enumerate(choices)))
        solver = solve(model)
        if solver is None:
            return None
        picked.append(next(j for j in range(len(choices)) if solver.value(x[i][j])))
    return tuple(picked)


def _carbon_per_kg(rec: Recorder, item: str):
    now_ef, after_ef = (
        rec.basis(f"emission.incinerate.{item}"),
        rec.basis(f"emission.recycle.{item}"),
    )
    if now_ef is None or after_ef is None:
        return None, []
    return (float(after_ef.value) - float(now_ef.value)) / 1000, [now_ef.id, after_ef.id]


def optimize_assignment(
    registry,
    *,
    org_id,
    today,
    streams,
    vendors,
    carbonWeight=0,
    exclude=None,
    cp_solver=_AUTO,
):
    """exclude는 [{"streamId", "vendorOrgId"}]. 업체가 거절했거나 제안하지 않은 짝을 뺀다.

    output["solver"]에 쓴 풀이를 남긴다. OR-Tools(CP-SAT)가 있으면 그것으로도 풀고, 조합이
    MAX_COMBINATIONS 이하이면 완전 탐색과 같은 답일 때만 "ortools", 아니면 완전 탐색의 답을 쓰고
    "exact"라고 남긴다(어느 쪽이든 배정은 같다). 조합이 그보다 많으면 CP-SAT의 답만 쓸 수 있다.
    cp_solver는 테스트에서 가짜로 바꾼다(도구 입력이 아니다): (usable, options, weight) -> 번호들.
    """
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
    output = {
        "assignments": [],
        "totals": {},
        "carbonInObjective": weight > 0,
        "skipped": [],
        "solver": None,
    }

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
    too_many = math.prod(len(o) for o in options) > MAX_COMBINATIONS
    if cp_solver is _AUTO:
        cp_solver = cp_sat_pick if HAS_ORTOOLS else None
    if too_many and cp_solver is None:
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

    def exhaustive():
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
        return best

    found = None
    if cp_solver is not None:
        try:
            picked = cp_solver(usable, options, weight)
            if picked is not None:
                found = tuple(options[i][int(j)] for i, j in enumerate(picked, 0))
                if len(found) != len(options) or not feasible(found):
                    found = None
        except Exception as error:  # noqa: BLE001 - 어떤 실패든 완전 탐색으로 돌아간다
            logger.warning("CP-SAT를 쓰지 못해 완전 탐색으로 돈다: %s", type(error).__name__)
            found = None
    if too_many:
        if found is None:
            raise ValueError("배정 조합이 너무 많다. 흐름이나 업체 수를 줄인다")
        best, output["solver"] = found, "ortools"
    else:
        best = exhaustive()
        same = found is not None and all(a is b for a, b in zip(found, best, strict=True))
        output["solver"] = "ortools" if same else "exact"

    now_total = after_total = carbon_total = 0.0
    carbon_known = True
    # 합계 계산 근거가 흐름별 계산 근거를 입력으로 잇게 한다(계산 사슬: 합계 → 흐름별 → 원 근거)
    parts: dict[str, list[str]] = {"now": [], "after": [], "difference": [], "carbon": []}
    for i, (item, choice) in enumerate(zip(usable, best, strict=True)):
        stream, kg = item["stream"], item["kg"]
        now = -item["cost"] * kg
        tag = f"s{i}"
        who = stream.get("name") or None  # calc 근거의 이름에 흐름 이름을 붙인다
        made = rec.number(
            f"{tag}MonthlyNow",
            now,
            "원/월",
            "−현행 처리비 × 월 발생량",
            item["cost_ev"],
            label=names.calc_label("monthlyNow", who),
        )
        parts["now"].append(made["evidence"][0])
        made = rec.number(
            f"{tag}MonthlyAfter",
            choice["value"],
            "원/월",
            "배정한 경로의 월 손익",
            choice["ev"],
            label=names.calc_label("monthlyAfter", who),
        )
        parts["after"].append(made["evidence"][0])
        made = rec.number(
            f"{tag}Difference",
            choice["value"] - now,
            "원/월",
            "배정 뒤 월 손익 − 현행 월 손익",
            [*choice["ev"], *item["cost_ev"]],
            label=names.calc_label("difference", who),
        )
        parts["difference"].append(made["evidence"][0])
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
                made = rec.number(
                    f"{tag}Carbon",
                    choice["carbon"],
                    "tCO2e/월",
                    "(재활용 계수 − 소각 계수) × 월 발생량",
                    item["carbon_ev"],
                    label=names.calc_label("carbon", who),
                )
                entry["carbon"] = made["id"]
                parts["carbon"].append(made["evidence"][0])
                carbon_total += choice["carbon"]
            else:
                carbon_known = False
        output["assignments"].append(entry)
        now_total, after_total = now_total + now, after_total + choice["value"]
    rec.number("totalNow", now_total, "원/월", "흐름별 현행 월 손익의 합", parts["now"])
    rec.number("totalAfter", after_total, "원/월", "흐름별 배정 뒤 월 손익의 합", parts["after"])
    rec.number(
        "totalDifference",
        after_total - now_total,
        "원/월",
        "배정 뒤 합 − 현행 합",
        parts["difference"],
    )
    output["totals"] = {
        "monthlyNow": "totalNow",
        "monthlyAfter": "totalAfter",
        "difference": "totalDifference",
        "carbon": None,
    }
    if carbon_known and any(a["carbon"] for a in output["assignments"]):
        output["totals"]["carbon"] = rec.number(
            "totalCarbon", carbon_total, "tCO2e/월", "흐름별 탄소 차이의 합", parts["carbon"]
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
