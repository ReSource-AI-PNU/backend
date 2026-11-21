"""⑥ 흐름과 업체 배정. 회사 측 월 순비용을 최소화한다."""

from __future__ import annotations

import itertools
import logging
import math

from app.v3 import names
from app.v3.judge import circular
from app.v3.judge.common import (
    CARBON_FORMULA,
    TOTAL_CARBON_FORMULA,
    TRANSPORT_CARBON_FORMULA,
    Recorder,
    after_process,
    best_criterion,
    carbon_per_kg,
    grade_of,
    labor_cost,
    num,
    permit_match,
    quality,
    transport_carbon,
    transport_charge,
    vendor_distance,
)
from app.v3.judge.permits import permit_level, short_view
from app.v3.judge.pickup import UNCONFIRMED
from app.v3.judge.route import current_cost, now_formula, reuse_checks, reuse_numbers

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
    """동점일 때의 순서: 현행 유지, 재투입, 업체 ID."""
    if choice["vendor"]:
        return str(choice["vendor"].get("orgId"))
    return "!reuse" if choice.get("reuse") else ""


def cp_sat_pick(usable, options, weight):
    """CP-SAT로 흐름마다 고를 선택지의 번호를 구한다. 못 풀면 None.

    로컬 미실행: 작성한 환경에서는 ortools를 불러올 수 없었다. 결과는 optimize_assignment가
    순수 파이썬 검사(feasible)와 완전 탐색으로 다시 확인한 뒤에만 쓴다.

    완전 탐색과 같은 규칙이다: 업체별 합계 물량이 고른 매입 기준의 최소 물량 이상, 회당 무게와
    부피가 차량 용량 이하(부피는 밀도를 모르는 흐름이 섞이면 보지 않는다), 목적은
    월 손익 − 가중치 × 탄소(처리 배출 변화 + 운송 배출)의 최대화, 동점이면 업체 ID 순서가 앞선 배정.
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
    # 업체에 하나라도 맡기면 한 번 드는 월 운반비(배출 기업 부담일 때). 완전 탐색과 같은 규칙이다
    fixed = {
        name: round(
            _fixed_transport(options[members[0][0]][members[0][1]]["vendor"]) * SOLVER_SCALE
        )
        for name, members in by_vendor.items()
    }

    def build():
        model = cp_model.CpModel()
        x = [
            [model.new_bool_var(f"x_{i}_{j}") for j in range(len(choices))]
            for i, choices in enumerate(options)
        ]
        for row in x:
            model.add(sum(row) == 1)
        used = {name: model.new_bool_var(f"used_{k}") for k, name in enumerate(sorted(by_vendor))}
        for name, members in by_vendor.items():
            vendor = options[members[0][0]][members[0][1]]["vendor"]
            total = sum(grams[i] * x[i][j] for i, j in members)
            for i, j in members:
                model.add(used[name] >= x[i][j])
            capacity = num(vendor.get("monthlyCapacityKg"))
            if capacity is not None:
                model.add(total <= math.floor(capacity * SOLVER_SCALE + 1e-6))
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
        return model, x, used

    def solve(model):
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1  # 같은 입력이면 같은 답
        return solver if solver.solve(model) == cp_model.OPTIMAL else None

    def objective(x, used):
        money = sum(gain[i][j] * x[i][j] for i in range(len(options)) for j in range(len(x[i])))
        return money - sum(fixed[name] * used[name] for name in used)

    model, x, used = build()
    model.maximize(objective(x, used))
    solver = solve(model)
    if solver is None:
        return None
    best = round(solver.objective_value)
    picked: list[int] = []
    for i, choices in enumerate(options):  # 동점 정리: 앞 흐름부터 업체 ID가 앞선 것을 고정한다
        if len(choices) == 1:
            picked.append(0)
            continue
        order = sorted({_name(c) for c in choices})
        model, x, used = build()
        model.add(objective(x, used) == best)
        for done, j in enumerate(picked):
            model.add(x[done][j] == 1)
        model.minimize(sum(order.index(_name(c)) * x[i][j] for j, c in enumerate(choices)))
        solver = solve(model)
        if solver is None:
            return None
        picked.append(next(j for j in range(len(choices)) if solver.value(x[i][j])))
    return tuple(picked)


def _fixed_transport(vendor: dict) -> float:
    """이 업체에 맡기면 배출 기업이 따로 내는 월 운반비. 조건이나 금액을 모르면 0으로 둔다."""
    return float(transport_charge(vendor)["monthly"] or 0.0)


def _tagged(label: str, tail: str) -> str:
    """calc 근거 이름의 " 계산" 앞에 꼬리표를 넣는다. "변경 뒤 월 손익(별도 운반비 반영 전) 계산"."""
    if not tail:
        return label
    head = label.removesuffix(" 계산")
    return f"{head}{tail} 계산" if head != label else f"{label}{tail}"


def _claim_id(made: dict | None, key: str) -> str | None:
    found = (made or {}).get(key)
    return found["id"] if found else None


def _pending_text(charge: dict) -> str:
    """운반비를 정하지 못한 이유. 숫자를 쓰지 않는다."""
    if charge["status"] == "check":
        why = "업체가 밝힌 수거 노선에 이 공장 지역이 없어 운반비가 정해지지 않았습니다"
    elif charge["basis"] == "separate" and charge["perPickup"] is not None:
        why = "회당 운반비는 있지만 월 수거 횟수가 없어 월 운반비를 계산하지 못했습니다(월 수거 횟수 확인 필요)"
    elif charge["basis"] == "separate":
        why = "운반비를 따로 내야 하는데 금액이 없어 계산에 넣지 못했습니다"
    else:
        why = "업체가 운반비 조건을 밝히지 않았습니다"
    return f"{why}. 월 손익은 별도 운반비 반영 전 값입니다. 확인 필요."


def _money_carbon(combo) -> tuple[float, float]:
    """배정 하나의 (월 손익, 탄소 변화). 월 손익은 흐름별 값에서 업체마다 한 번 드는 운반비를 뺀 값이다."""
    hired = {str(c["vendor"].get("orgId")): c["vendor"] for c in combo if c["vendor"]}
    money = math.fsum(c["value"] for c in combo) - math.fsum(
        _fixed_transport(v) for v in hired.values()
    )
    return money, math.fsum(c["carbon"] for c in combo)


def _route(choice) -> str:
    if choice.get("reuse"):
        return "reuse"
    if choice["vendor"] is None:
        return "keep"
    return "sell" if choice["price"] >= 0 else "treat"


def _greenest(rec: Recorder, usable, best, green) -> dict | None:
    """탄소를 가장 많이 줄이는 배정과 고른 배정의 차이. 완전 탐색을 하지 않았으면 None.

    고른 배정이 탄소도 가장 많이 줄이면 same True다. 다르면 바뀌는 흐름과, 탄소 우선 배정의 월 손익·탄소와
    고른 배정과의 차이를 계산 근거로 남긴다. 고른 배정은 "월 손익 − 내부 탄소 가격 × 탄소"가 가장 크므로
    돈 차이가 그 가격으로 환산한 탄소 차이보다 크다.
    """
    if green is None:
        return None
    if all(a is b for a, b in zip(green, best, strict=True)):
        return {"same": True, "changes": []}
    money, carbon = _money_carbon(green)
    chosen_money, chosen_carbon = _money_carbon(best)
    evidence = list(
        dict.fromkeys(
            e
            for c in green
            for e in [
                *c["ev"],
                *c["carbon_ev"],
                *([c["moved"]["factor"]] if (c.get("moved") or {}).get("known") else []),
            ]
        )
    )
    after = rec.number(
        "greenMonthlyAfter",
        money,
        "원/월",
        "탄소를 가장 많이 줄이는 배정의 월 손익 합(업체마다 한 번 드는 운반비를 뺀 값)",
        evidence,
    )
    total = rec.number(
        "greenCarbon",
        carbon,
        "tCO2e/월",
        "탄소를 가장 많이 줄이는 배정의 탄소 변화 합(처리 방식 + 운송)",
        evidence,
    )
    money_gap = rec.number(
        "greenMoneyGap",
        money - chosen_money,
        "원/월",
        "탄소 우선 배정의 월 손익 − 고른 배정의 월 손익",
        [after["evidence"][0]],
    )
    carbon_gap = rec.number(
        "greenCarbonGap",
        carbon - chosen_carbon,
        "tCO2e/월",
        "탄소 우선 배정의 탄소 변화 − 고른 배정의 탄소 변화",
        [total["evidence"][0]],
    )
    changes = [
        {
            "streamId": item["stream"].get("id"),
            "vendorOrgId": g["vendor"].get("orgId") if g["vendor"] else None,
            "route": _route(g),
        }
        for item, g, b in zip(usable, green, best, strict=True)
        if g is not b
    ]
    return {
        "same": False,
        "changes": changes,
        "monthlyAfter": after["id"],
        "carbon": total["id"],
        "moneyGap": money_gap["id"],
        "carbonGap": carbon_gap["id"],
    }


def optimize_assignment(
    registry,
    *,
    org_id,
    today,
    streams,
    vendors,
    carbonWeight=None,
    exclude=None,
    origin=None,
    cp_solver=_AUTO,
    distance_provider=None,
):
    """exclude는 [{"streamId", "vendorOrgId"}]. 업체가 거절했거나 제안하지 않은 짝을 뺀다.

    탄소: 처리 방식이 바뀌어 달라지는 배출과 업체로 실어 가는 운송 배출(트럭 운송 계수 × 월 발생량 × 공장 → 업체
    거리, origin이 공장 좌표 [경도, 위도])을 함께 본다. carbonWeight(원/tCO2e)를 주지 않으면 배출권(KAU) 시세를
    내부 탄소 가격으로 써서 목적에 넣는다(0을 주면 탄소를 목적에서 뺀다). 화면의 월 손익은 실제로 오가는 돈만이고,
    탄소 환산액은 따로 보인다. 완전 탐색으로 풀 때는 탄소를 가장 많이 줄이는 배정도 구해(output.greenest) 고른
    배정과의 돈·탄소 차이를 남긴다.

    허가: 배정 후보(업체 · 폐기물 · 처리 방식)마다 허가 확인 상태(permits.permit_level)를 본다. 담당자의 원문 대조가
    품목·처리 방식이 다르다고 했거나 확인한 허가증 업종·공공 명부가 처리 방식과 어긋나면(mismatch) 그 업체에는
    배정하지 않는다(진행 가능한 합계에도 들지 않는다). 대조 완료(verified)가 아니면 배정은 조건부다.

    output["solver"]에 쓴 풀이를 남긴다. OR-Tools(CP-SAT)가 있으면 그것으로도 풀고, 조합이
    MAX_COMBINATIONS 이하이면 완전 탐색과 같은 답일 때만 "ortools", 아니면 완전 탐색의 답을 쓰고
    "exact"라고 남긴다(어느 쪽이든 배정은 같다). 조합이 그보다 많으면 CP-SAT의 답만 쓸 수 있다.
    cp_solver는 테스트에서 가짜로 바꾼다(도구 입력이 아니다): (usable, options, weight) -> 번호들.
    """
    orgs = sorted({str(s.get("orgId")) for s in streams})
    if len(orgs) > 1:
        raise ValueError("한 번에 한 배출 기업의 폐기물만 배정할 수 있다")
    if org_id is not None and orgs and orgs[0] != org_id:
        raise ValueError("다른 조직의 폐기물은 배정할 수 없다")
    vendors = sorted(vendors, key=lambda v: str(v.get("orgId")))  # 입력 순서가 달라도 같은 근거 ID
    args = {"streams": streams, "vendors": vendors, "carbonWeight": num(carbonWeight)}
    excluded = sorted({(str(x.get("streamId")), str(x.get("vendorOrgId"))) for x in exclude or []})
    if excluded:
        args["exclude"] = [list(pair) for pair in excluded]
    if origin:
        args["origin"] = list(origin)
    rec = Recorder("optimize_assignment", registry, org_id, today, args)
    output = {
        "assignments": [],
        "totals": {},
        "carbonInObjective": False,
        "carbonPrice": None,
        "skipped": [],
        "solver": None,
    }
    weight, price_ev = num(carbonWeight), []
    if carbonWeight is None:
        # 탄소를 배출권 시세로 환산해 목적에 넣는다(내부 탄소 가격). 시세 근거가 없으면 탄소는 참고 값이다
        kau = rec.basis("kau.price")
        if kau is not None and num(kau.value):
            weight, price_ev = float(kau.value), [kau.id]
        else:
            weight = 0.0
            rec.check(
                "kau",
                "conditional",
                "배출권 시세 근거가 없어 탄소를 배정 목적에 넣지 못했습니다. 확인 필요.",
            )
    weight = max(weight or 0.0, 0.0)
    if weight > 0:
        output["carbonInObjective"] = True
        output["carbonPrice"] = rec.number(
            "carbonPrice",
            weight,
            "원/tCO2e",
            "배출권(KAU) 시세를 내부 탄소 가격으로 씀" if price_ev else "도구 입력의 탄소 가격",
            price_ev,
        )["id"]
    # 공장 → 업체 거리(업체마다 한 번). 운송 배출은 흐름마다 이 거리와 월 발생량으로 낸다
    distances = {
        str(v.get("orgId")): vendor_distance(rec, v, origin, distance_provider) for v in vendors
    }
    output["permitExcluded"] = []
    # 수거 불가라고 밝힌 업체에는 배정하지 않는다(진행 가능한 추천과 합계에서 뺀다)
    output["pickupExcluded"] = []
    charges_of = {str(v.get("orgId")): transport_charge(v) for v in vendors}

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
        cost = current_cost(rec, stream)[0]
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
        cost, cost_ev, assumed, method = current_cost(rec, stream)
        labor, labor_ev, hours, rate = labor_cost(rec, stream)
        q = quality(stream)
        keep = {
            "vendor": None,
            "value": -cost * kg,
            "revenue": -cost * kg,
            "labor": 0.0,
            "ev": cost_ev,
            "criterion": None,
            "carbon": 0.0,
            "process": 0.0,
            "carbon_ev": [],
            "carbon_known": True,
            "moved": None,
            "permit": None,
        }
        choices = [keep]
        # 공정 재투입: 후보 조건을 모두 채우고 신재 단가와 다시 넣을 양이 있을 때만 선택지가 된다
        grade = None
        if q["contaminationPct"] is not None and q["moisturePct"] is not None:
            grade, _ = grade_of(
                rec, stream.get("material"), q["contaminationPct"], q["moisturePct"]
            )
        reuse_ok = all(c[1] == "met" for c in reuse_checks(stream, grade))
        numbers = reuse_numbers(stream, kg, cost) if reuse_ok else None
        if numbers is not None:
            choices.append(
                {
                    **keep,
                    "reuse": True,
                    "value": numbers[3],
                    "revenue": numbers[1],
                    "ev": [*list((stream.get("reuse") or {}).get("evidence") or []), *cost_ev],
                }
            )
        for vendor in vendors:
            if (str(stream.get("id")), str(vendor.get("orgId"))) in excluded:
                continue
            if permit_match(stream, vendor)[0] is not True:
                continue
            criterion, checks = best_criterion(stream, vendor, q)
            if criterion is None or any(c[1] == "unmet" for c in checks):
                continue
            level = permit_level(registry, stream, vendor, today)
            if level is not None and level["status"] == "unmet":
                # 허가 근거가 서로 맞지 않으면 그 업체에는 배정하지 않는다(허가 기관 확인 뒤 다시 계산)
                output["permitExcluded"].append(
                    {
                        "streamId": stream.get("id"),
                        "vendorOrgId": vendor.get("orgId"),
                        "text": level["text"],
                        "evidence": rec.visible(level["evidence"]),
                        "permit": {**short_view(level), "evidence": rec.visible(level["evidence"])},
                    }
                )
                rec.check(
                    f"permit_level:{stream.get('id')}:{vendor.get('orgId')}",
                    "unmet",
                    level["text"],
                    None,
                    level["evidence"],
                )
                continue
            if charges_of[str(vendor.get("orgId"))]["unavailable"]:
                gone = charges_of[str(vendor.get("orgId"))]
                text = "업체가 이 공장까지 수거하러 올 수 없다고 밝혀 배정하지 않았습니다."
                output["pickupExcluded"].append(
                    {
                        "streamId": stream.get("id"),
                        "vendorOrgId": vendor.get("orgId"),
                        "text": text,
                        "evidence": rec.visible(gone["evidence"]),
                    }
                )
                rec.check(
                    f"pickup:{stream.get('id')}:{vendor.get('orgId')}",
                    "unmet",
                    text,
                    None,
                    gone["evidence"],
                )
                continue
            price = float(criterion["unitPriceKrwPerKg"])
            per_kg, carbon_ev, _ = carbon_per_kg(rec, stream, vendor, price, method)
            moved = transport_carbon(rec, distances[str(vendor.get("orgId"))], kg)
            process = (per_kg or 0.0) * kg
            choices.append(
                {
                    "vendor": vendor,
                    "price": price,
                    "revenue": price * kg,
                    "labor": labor or 0.0,
                    # 흐름 하나의 값: 매각 수익(처리비면 음수) − 전처리 인건비. 운반비는 업체 단위로 뺀다
                    "value": price * kg - (labor or 0.0),
                    "ev": list(criterion.get("evidence") or []),
                    "criterion": criterion,
                    # 목적에 넣는 탄소: 처리 방식 탄소(배출계수가 없으면 0) + 운송 배출(모르면 0)
                    "carbon": process + (moved["tco2e"] or 0.0),
                    "process": process,
                    "carbon_ev": carbon_ev,
                    "carbon_known": per_kg is not None and moved["known"],
                    "moved": moved,
                    "permit": level,
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
                "method": method,
                "labor_ev": labor_ev,
                "hours": hours,
                "rate": rate,
            }
        )
        options.append(choices)

    if not usable:
        return rec.result("needs_information", output)
    if weight and any(
        c["vendor"] and not c["carbon_known"] for choices in options for c in choices
    ):
        weight = 0.0
        output["carbonInObjective"] = False
        output["carbonPrice"] = None
        rec.check(
            "carbon_objective",
            "conditional",
            "후보의 탄소 경계가 모두 확인되지 않아 미산정을 무배출로 비교하지 않고 금액 기준으로만 배정했습니다.",
        )
    too_many = math.prod(len(o) for o in options) > MAX_COMBINATIONS
    if cp_solver is _AUTO:
        cp_solver = cp_sat_pick if HAS_ORTOOLS else None
    if too_many and cp_solver is None:
        raise ValueError("배정 조합이 너무 많다. 폐기물이나 업체 수를 줄인다")

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
            capacity = num(vendor.get("monthlyCapacityKg"))
            if capacity is not None and total > capacity + 1e-9:
                return False  # 업체의 월 처리 용량
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

    greenest = None
    unbundled = None  # 시나리오 B: 같은 업체로 합쳐 최소 물량을 채우는 묶음 없이 고른 배정

    def alone(combo) -> bool:
        """업체로 가는 폐기물마다 혼자서 고른 매입 기준의 최소 물량을 채우는가."""
        return all(
            choice["vendor"] is None
            or usable[i]["kg"] + 1e-9 >= (num(choice["criterion"].get("minMonthlyKg")) or 0.0)
            for i, choice in enumerate(combo)
        )

    def exhaustive():
        nonlocal greenest, unbundled
        best, best_key, green_key, alone_key = None, None, None, None
        for combo in itertools.product(*options):
            if not feasible(combo):
                continue
            money, carbon = _money_carbon(combo)
            objective = money - weight * carbon
            names = tuple(_name(c) for c in combo)
            # Unknown transport cannot win by being treated as free. Keep such offers
            # in negotiation, but prefer a feasible fully costed allocation (including keep).
            pending = sum(
                bool(c["vendor"] and transport_charge(c["vendor"])["pending"]) for c in combo
            )
            key = (pending, -round(objective, 6), names)
            if best_key is None or key < best_key:
                best, best_key = combo, key
            if (alone_key is None or key < alone_key) and alone(combo):
                unbundled, alone_key = combo, key
        if best is None:
            return None
        # 탄소를 가장 많이 줄이는 배정(탄소가 같으면 돈이 나은 쪽, 그다음 업체 ID 순서). 처리 방식 배출계수를
        # 모르는 선택지(열분해 등, 탄소 0으로 들어 있다)로 바꾸거나 거기서 빼는 비교는 하지 않는다. 모르는 값을
        # 0으로 견주면 "소각으로 되돌리면 운송 배출만큼 덜 낸다" 같은 거짓 비교가 나온다
        for combo in itertools.product(*options):
            if not feasible(combo) or not all(
                c is b or (c["carbon_known"] and b["carbon_known"])
                for c, b in zip(combo, best, strict=True)
            ):
                continue
            money, carbon = _money_carbon(combo)
            green = (round(carbon, 9), -round(money, 6), tuple(_name(c) for c in combo))
            if green_key is None or green < green_key:
                greenest, green_key = combo, green
        return best

    found = None
    if any(
        c["vendor"] and transport_charge(c["vendor"])["pending"]
        for choices in options
        for c in choices
    ):
        cp_solver = None  # The integer objective has no unknown-cost ordering.
        if too_many:
            rec.check(
                "cost_pending",
                "conditional",
                "운반비 미정 후보가 많아 확정 배정을 보류합니다. 견적을 입력해 주세요.",
            )
            return rec.result("needs_information", output)
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
            raise ValueError("배정 조합이 너무 많다. 폐기물이나 업체 수를 줄인다")
        best, output["solver"] = found, "ortools"
    else:
        best = exhaustive()
        same = found is not None and all(a is b for a, b in zip(found, best, strict=True))
        output["solver"] = "ortools" if same else "exact"

    now_total = after_total = carbon_total = 0.0
    no_factor: list[str] = []  # 배출계수가 없어 탄소를 내지 못한 흐름의 이름
    # 업체별 월 운반비를 그 업체로 가는 흐름에 물량 비율로 나눈다(합이 운반비와 같게 마지막 흐름이 나머지를 받는다)
    groups: dict[str, list[int]] = {}
    for i, choice in enumerate(best):
        if choice["vendor"] is not None:
            groups.setdefault(str(choice["vendor"].get("orgId")), []).append(i)
    share: dict[int, float] = {}
    charges: dict[int, dict] = {}
    # 공장 → 업체 거리: 배정한 업체마다 한 번 계산 근거로 남기고, 그 업체로 가는 흐름의 운송 배출이 이 근거를 잇는다
    distance_claims: dict[str, dict] = {}
    no_distance: list[str] = []
    for k, vendor_id in enumerate(sorted(groups)):
        distance = distances.get(vendor_id) or {}
        if not distance.get("known"):
            no_distance.append(distance.get("reason") or "자료가 없어")
            continue
        distance_claims[vendor_id] = rec.number(
            f"v{k}DistanceKm",
            distance["km"],
            "km",
            "공장 → 업체 차고지 거리"
            + (" (직선 거리 × 우회 계수, 가정값)" if distance["assumed"] else " (도로 거리)"),
            distance["evidence"],
        )
    # 업체마다 한 번 드는 월 운반비(견적 금액 · 수거 횟수 · 월 합계)를 계산 근거로 남긴다. 같은 수거 차량에 함께
    # 싣는 폐기물에는 한 번만 들고, 흐름별 몫은 이 월 합계를 물량 비율로 나눈 값이다(합이 월 합계와 같다)
    hauls: dict[str, dict] = {}
    for k, vendor_id in enumerate(sorted(groups)):
        members = groups[vendor_id]
        charge = transport_charge(best[members[0]]["vendor"])
        made: dict[str, dict | None] = {"fee": None, "pickups": None, "monthly": None}
        if charge["basis"] == "separate" and charge["monthly"] is not None:
            per_month = charge["unit"] == "per_month"
            made["fee"] = rec.number(
                f"v{k}TransportFee",
                charge["perMonth"] if per_month else charge["perPickup"],
                "원/월" if per_month else "원/회",
                "업체가 밝힌 월 운반비" if per_month else "업체가 밝힌 회당 운반비",
                charge["feeEvidence"],
            )
            inputs = [made["fee"]["evidence"][0]]
            if not per_month:
                made["pickups"] = rec.number(
                    f"v{k}TransportPickups",
                    charge["pickups"],
                    "회/월",
                    "업체가 밝힌 월 수거 횟수"
                    if charge["pickupsEvidence"]
                    else "업체가 정한 월 최소 수거 횟수",
                    charge["pickupsEvidence"],
                )
                inputs.append(made["pickups"]["evidence"][0])
            made["monthly"] = rec.number(
                f"v{k}TransportMonthly",
                charge["monthly"],
                "원/월",
                "업체가 밝힌 월 운반비. 수거 횟수를 곱하지 않습니다"
                if per_month
                else "회당 운반비 × 월 수거 횟수. 같은 수거 차량에 함께 싣는 폐기물에는 한 번만 듭니다",
                inputs,
            )
        hauls[vendor_id] = made
        total_kg = math.fsum(usable[i]["kg"] for i in members)
        left = float(charge["monthly"] or 0.0)
        for i in members[:-1]:
            share[i] = round(float(charge["monthly"] or 0.0) * usable[i]["kg"] / total_kg)
            left -= share[i]
        share[members[-1]] = left
        for i in members:
            charges[i] = charge

    def room_of(i: int) -> tuple[float, dict] | None:
        """(고른 배정의 월 손익 − 이 흐름만 다음으로 나은 선택지로 바꾼 배정의 월 손익, 그 선택지).

        운반비가 정해지지 않은 업체가 이 흐름 하나만 맡을 때만 낸다. 바꾼 배정도 최소 물량·용량을 지켜야 하고,
        운반비를 모르거나 수거 불가인 다른 업체는 견주지 않는다. 월 손익은 업체마다 한 번 드는 운반비를 뺀 값이다.
        """
        choice = best[i]
        if len(groups.get(str(choice["vendor"].get("orgId"))) or []) != 1:
            return None
        base, _ = _money_carbon(best)
        found = None
        for other in options[i]:
            if other is choice:
                continue
            if other["vendor"] is not None:
                terms = transport_charge(other["vendor"])
                if terms["pending"] or terms["unavailable"]:
                    continue
            combo = tuple(other if k == i else c for k, c in enumerate(best))
            if not feasible(combo):
                continue
            gap = base - _money_carbon(combo)[0]
            if found is None or gap < found[0] - 1e-9:
                found = (gap, other)
        return found if found and found[0] > 0 else None

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
            now_formula(stream),
            item["cost_ev"],
            label=names.calc_label("monthlyNow", who),
        )
        parts["now"].append(made["evidence"][0])
        now_id = made["evidence"][0]
        breakdown = {"revenue": None, "transport": None, "labor": None}
        after, after_ev, after_formula = choice["value"], list(choice["ev"]), "현행 유지의 월 손익"
        if choice.get("reuse"):
            made = rec.number(
                f"{tag}Revenue",
                choice["revenue"],
                "원/월",
                "다시 넣는 양 × (신재 구입 단가 − 분쇄 비용)",
                choice["ev"],
                label=names.calc_label("revenue", who),
            )
            breakdown["revenue"] = made["id"]
            after_ev = [made["evidence"][0], *choice["ev"]]
            after_formula = "신재 절감액 − 다시 넣지 못하는 양의 현행 처리비"
        charge = charges.get(i)
        if choice["vendor"] is not None:
            made = rec.number(
                f"{tag}Revenue",
                choice["revenue"],
                "원/월",
                "업체 단가 × 월 발생량(처리비면 음수)",
                choice["ev"],
                label=names.calc_label("revenue", who),
            )
            breakdown["revenue"] = made["id"]
            after_ev = [made["evidence"][0], *choice["ev"]]
            after_formula = "매각 수익(처리비면 음수) − 운반비 − 전처리 인건비"
            haul = hauls.get(str(choice["vendor"].get("orgId"))) or {}
            if charge and charge["monthly"] is not None:
                made = rec.number(
                    f"{tag}Transport",
                    share[i],
                    "원/월",
                    "운반비는 업체 단가에 들어 있습니다(수거 포함)"
                    if charge["basis"] == "included"
                    else "이 업체의 월 운반비를 이 업체로 가는 폐기물의 물량 비율로 나눈 값",
                    [haul["monthly"]["evidence"][0]] if haul.get("monthly") else charge["evidence"],
                    label=names.calc_label("transport", who),
                )
                breakdown["transport"] = made["id"]
                after_ev.append(made["evidence"][0])
            elif charge and charge["pending"]:
                # 운반비를 정하지 못했다. 0원으로 확정하지 않고 월 손익을 "별도 운반비 반영 전"으로 둔다
                after_formula = "매각 수익(처리비면 음수) − 전처리 인건비. 별도 운반비 반영 전"
                rec.check(
                    f"transport:{stream.get('id')}",
                    "conditional",
                    _pending_text(charge),
                    None,
                    charge["evidence"],
                )
            if charge and charge["status"] in UNCONFIRMED:
                rec.check(
                    f"pickup:{stream.get('id')}",
                    "conditional",
                    "업체가 밝힌 수거 노선에 이 공장 지역이 없어 따로 수거하러 올 수 있는지 확인이 필요합니다."
                    if charge["status"] == "check"
                    else "업체가 수거 노선을 밝히지 않아 이 공장까지 수거하러 오는지 확인이 필요합니다.",
                    None,
                    charge["evidence"],
                )
            if choice["labor"]:
                made = rec.number(
                    f"{tag}Labor",
                    choice["labor"],
                    "원/월",
                    "월 전처리 작업 시간 × 시간당 인건비",
                    item["labor_ev"],
                    label=names.calc_label("labor", who),
                )
                breakdown["labor"] = made["id"]
                after_ev.append(made["evidence"][0])
            after = choice["value"] - share.get(i, 0.0)
        pending = bool(choice["vendor"] and charge and charge["pending"])
        cost_pending = bool(choice["vendor"] and item["hours"] is None) or bool(
            choice["vendor"] and item["hours"] and item["rate"] is None
        )
        tail = "(미확인 비용 반영 전 잠정 금액)" if pending or cost_pending else ""
        if cost_pending:
            after_formula += ". 전처리·선별·세척 비용 미확인으로 순손익을 확정하지 않았습니다"
        made = rec.number(
            f"{tag}MonthlyAfter",
            after,
            "원/월",
            after_formula,
            after_ev,
            label=_tagged(names.calc_label("monthlyAfter", who), tail),
        )
        parts["after"].append(made["evidence"][0])
        after_id = made["evidence"][0]
        made = rec.number(
            f"{tag}Difference",
            after - now,
            "원/월",
            "배정 뒤 월 손익 − 현행 월 손익" + (". 별도 운반비 반영 전" if pending else ""),
            [*choice["ev"], *item["cost_ev"]],
            label=_tagged(names.calc_label("difference", who), tail),
        )
        parts["difference"].append(made["evidence"][0])
        unconfirmed = bool(choice["vendor"] and charge and charge["status"] in UNCONFIRMED)
        haul = hauls.get(str(choice["vendor"].get("orgId"))) if choice["vendor"] else None
        entry = {
            "streamId": stream.get("id"),
            "vendorOrgId": choice["vendor"].get("orgId") if choice["vendor"] else None,
            "route": "reuse"
            if choice.get("reuse")
            else "keep"
            if choice["vendor"] is None
            else ("sell" if choice["price"] >= 0 else "treat"),
            "criterionId": choice["criterion"].get("id") if choice["criterion"] else None,
            "status": "conditional"
            if choice.get("conditional") or pending or unconfirmed
            else "calculated",
            "monthlyNow": f"{tag}MonthlyNow",
            "monthlyAfter": f"{tag}MonthlyAfter",
            "difference": f"{tag}Difference",
            "revenue": breakdown["revenue"],
            "transport": breakdown["transport"],
            "labor": breakdown["labor"],
            "transportPaidBy": charge["paidBy"] if charge and charge["known"] else None,
            # 수거 상태와 운반비 조건(judge/pickup.py). 업체마다 한 번 드는 월 운반비의 견적 근거를 잇는다
            "pickupStatus": charge["status"] if charge else None,
            "transportBasis": charge["basis"] if charge else None,
            "transportUnit": charge["unit"] if charge else None,
            "transportPending": pending,
            "costPending": cost_pending,
            "transportFee": _claim_id(haul, "fee"),
            "transportPickups": _claim_id(haul, "pickups"),
            "transportMonthly": _claim_id(haul, "monthly"),
            "currentAssumed": item["assumed"],
            "carbon": None,
            "carbonProcess": None,
            "carbonTransport": None,
            "carbonValue": None,
            "transportKm": None,
            "permit": None,
            "transportRoom": None,
            "transportRoomAgainst": None,
        }
        if pending:
            # 운반비가 정해지지 않은 배정: 운반비가 한 달 얼마를 넘으면 다음으로 나은 선택지가 나은지 남긴다
            room = room_of(i)
            if room is not None:
                gap, other = room
                entry["transportRoom"] = rec.number(
                    f"{tag}TransportRoom",
                    gap,
                    "원/월",
                    "별도 운반비 반영 전 월 손익 − 이 폐기물만 다음으로 나은 선택지로 바꿨을 때의 월 손익. "
                    "이 업체의 월 운반비가 이보다 크면 다음 선택지가 낫습니다",
                    [after_id, *(other["ev"] if other["vendor"] is not None else [now_id])],
                    label=f"{who} 운반비 손익분기 계산" if who else "운반비 손익분기 계산",
                )["id"]
                entry["transportRoomAgainst"] = {
                    "vendorOrgId": other["vendor"].get("orgId") if other["vendor"] else None,
                    "route": _route(other),
                }
        if choice["vendor"] is not None:
            vendor_id = str(choice["vendor"].get("orgId"))
            moved = choice["moved"] if (choice["moved"] or {}).get("known") else None
            transport = None
            if moved is not None and vendor_id in distance_claims:
                distance = distance_claims[vendor_id]
                transport = rec.number(
                    f"{tag}CarbonTransport",
                    moved["tco2e"],
                    "tCO2e/월",
                    TRANSPORT_CARBON_FORMULA,
                    [distance["evidence"][0], moved["factor"]],
                    label=names.calc_label("carbonTransport", who),
                )
                entry["carbonTransport"] = transport["id"]
                entry["transportKm"] = distance["id"]
            if choice["carbon_known"]:
                process = rec.number(
                    f"{tag}CarbonProcess",
                    choice["process"],
                    "tCO2e/월",
                    CARBON_FORMULA,
                    choice["carbon_ev"],
                    label=names.calc_label("carbonProcess", who),
                )
                entry["carbonProcess"] = process["id"]
                total = choice["process"] + (moved["tco2e"] if transport else 0.0)
                made = rec.number(
                    f"{tag}Carbon",
                    total,
                    "tCO2e/월",
                    TOTAL_CARBON_FORMULA if transport else CARBON_FORMULA,
                    [process["evidence"][0], *([transport["evidence"][0]] if transport else [])],
                    label=names.calc_label("carbon", who),
                )
                entry["carbon"] = made["id"]
                parts["carbon"].append(made["evidence"][0])
                carbon_total += made[
                    "value"
                ]  # 합계가 흐름별 근거 값의 합과 같게 반올림한 값을 더한다
                if weight > 0:
                    entry["carbonValue"] = rec.number(
                        f"{tag}CarbonValue",
                        -total * weight,
                        "원/월",
                        "처리 단계 배출 변화 × 내부 탄소 가격(배출권 시세). 배정을 고를 때만 쓰는 판단용 "
                        "환산이며 받거나 팔 수 있는 돈이 아닙니다",
                        [made["evidence"][0], *price_ev],
                        label=names.calc_label("carbonValue", who),
                    )["id"]
            else:
                no_factor.append(str(who or stream.get("id")))
                if transport is not None:
                    # 처리 방식 배출계수가 없어도 운송 배출은 합계에 넣는다
                    parts["carbon"].append(transport["evidence"][0])
                    carbon_total += transport["value"]
            level = choice["permit"]
            if level is not None:
                entry["permit"] = {**short_view(level), "evidence": rec.visible(level["evidence"])}
                rec.check(
                    f"permit_level:{stream.get('id')}",
                    level["status"],
                    level["text"],
                    None,
                    level["evidence"],
                )
                if level["status"] != "met":
                    entry["status"] = "conditional"
        output["assignments"].append(entry)
        now_total, after_total = now_total + now, after_total + after
    # 운반비를 정하지 못한 배정이 섞인 합계는 잠정값이다(이름과 계산식에 밝힌다)
    pending_ids = [
        a["streamId"]
        for a in output["assignments"]
        if a.get("transportPending") or a.get("costPending")
    ]
    note = (
        ". 미확인 운반비·작업비 반영 전 잠정 금액이 포함되어 순손익이 아닙니다"
        if pending_ids
        else ""
    )
    tail = "(미확인 비용 반영 전 포함)" if pending_ids else ""
    rec.number("totalNow", now_total, "원/월", "폐기물별 현행 월 손익의 합", parts["now"])
    rec.number(
        "totalAfter",
        after_total,
        "원/월",
        "폐기물별 배정 뒤 월 손익의 합" + note,
        parts["after"],
        label=_tagged(names.calc_label("totalAfter"), tail),
    )
    rec.number(
        "totalDifference",
        after_total - now_total,
        "원/월",
        "배정 뒤 합 − 현행 합" + note,
        parts["difference"],
        label=_tagged(names.calc_label("totalDifference"), tail),
    )
    output["totals"] = {
        "monthlyNow": "totalNow",
        "monthlyAfter": "totalAfter",
        "difference": "totalDifference",
        "carbon": None,
        "pending": pending_ids,
    }
    if parts["carbon"]:
        output["totals"]["carbon"] = rec.number(
            "totalCarbon",
            carbon_total,
            "tCO2e/월",
            "폐기물별 탄소 변화(처리 방식 + 운송)의 합. 배출계수가 없는 처리는 운송 배출만 든다",
            parts["carbon"],
        )["id"]
    if no_distance:
        rec.check(
            "transport_carbon",
            "conditional",
            f"{'·'.join(dict.fromkeys(no_distance))} 운송 배출을 계산하지 못한 업체가 있습니다. 확인 필요.",
        )
    output["greenest"] = (
        _greenest(rec, usable, best, greenest)
        if all(c["carbon_known"] for choices in options for c in choices)
        else None
    )
    # 자원순환 효과(현행 대비 감축량·에너지 절감·신재 대체)와 네 시나리오. 월 손익과 섞지 않는다
    money_b = None
    if unbundled is not None:
        money_b = rec.number(
            "ceB_totalDifference",
            _money_carbon(unbundled)[0] - now_total,
            "원/월",
            "묶음 없이 고른 배정의 월 손익 합(업체마다 한 번 드는 운반비를 뺀 값) − 현행 합",
            parts["now"],
            label="묶음 없이 고른 배정의 현행 대비 차이 합계 계산",
        )["id"]
    output["circular"] = circular.build(
        rec,
        usable,
        best,
        unbundled,
        after_process,
        money_a="totalDifference",
        money_b=money_b,
        skipped=output["skipped"],
    )
    if no_factor:
        rec.check(
            "carbon",
            "conditional",
            f"배출계수가 없는 처리 방식으로 가는 폐기물({', '.join(no_factor)})은 "
            "탄소 계산에서 뺐습니다. 확인 필요.",
        )
    if any(a["currentAssumed"] for a in output["assignments"]):
        rec.check(
            "current_assumed",
            "met",
            "현행 처리를 입력하지 않은 폐기물은 혼합 소각 가정값을 썼습니다.",
        )
    conditional = output["skipped"] or any(
        a["status"] == "conditional" for a in output["assignments"]
    )
    return rec.result("conditional" if conditional else "calculated", output)
