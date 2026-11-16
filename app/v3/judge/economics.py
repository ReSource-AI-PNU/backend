"""⑤ 비용·탄소 계산과 업체 노선 추가 운송비."""

from __future__ import annotations

import itertools
import math

from app.v3.judge.common import Recorder, best_criterion, distance_matrix, num, quality
from app.v3.judge.route import baseline

try:  # 큰 문제에서만 쓴다. 작은 문제는 순수 파이썬 완전 탐색이 정답을 낸다.
    from ortools.constraint_solver import pywrapcp  # noqa: F401

    HAS_ORTOOLS = True
except Exception:  # noqa: BLE001
    HAS_ORTOOLS = False

EXACT_LIMIT = 8


def cost_carbon(
    registry,
    *,
    org_id,
    today,
    stream,
    vendor,
    origin=None,
    pickupsPerMonth=None,
    transportPaidBy="vendor",
    distance_provider=None,
):
    args = {
        "stream": stream,
        "vendor": vendor,
        "origin": origin,
        "pickups": pickupsPerMonth,
        "paidBy": transportPaidBy,
    }
    rec = Recorder("cost_carbon", registry, org_id, today, args, [vendor.get("orgId")])
    output = {
        "streamId": stream.get("id"),
        "vendorOrgId": vendor.get("orgId"),
        "transportAssumed": None,
        "carbon": None,
        "kau": None,
        "current": None,
    }
    kg = num(stream.get("monthlyKg"))
    if kg is None or kg <= 0:
        rec.check(
            "monthly_kg", "conditional", "월 발생량을 몰라 금액과 탄소를 계산하지 않았습니다."
        )
        return rec.result("needs_information", output)
    criterion, checks = best_criterion(stream, vendor, quality(stream))
    if criterion is None or any(c[1] == "unmet" for c in checks):
        rec.check("criteria", "unmet", "충족하는 매입 기준이 없어 계산하지 않았습니다.")
        return rec.result("needs_information", output)
    crit_ev = list(criterion.get("evidence") or [])
    price = float(criterion["unitPriceKrwPerKg"])
    rec.number("monthlyKg", kg, "kg/월", "입력한 월 발생량")
    rec.number("unitPrice", price, "원/kg", "업체 매입 기준표의 단가(음수면 처리비)", crit_ev)
    revenue = price * kg
    rec.number("monthlyRevenue", revenue, "원/월", "단가 × 월 발생량", crit_ev)

    transport = None
    fleet = vendor.get("fleet") or {}
    depot = fleet.get("depot")
    pickups = num(pickupsPerMonth) or num(
        (vendor.get("guardrails") or {}).get("minPickupsPerMonth")
    )
    per_km, per_km_ev = num(fleet.get("costPerKm")), list(fleet.get("evidence") or [])
    if per_km is None:
        assumed_cost = rec.basis("transport.costPerKm")
        if assumed_cost is not None:
            per_km, per_km_ev = float(assumed_cost.value), [assumed_cost.id]
    if origin and depot and pickups and per_km is not None:
        matrix, assumed, dist_ev = distance_matrix(rec, [depot, origin], distance_provider)
        if matrix is not None:
            round_trip = matrix[0][1] + matrix[1][0]
            rec.number("roundTripKm", round_trip, "km", "업체 차고지 ↔ 공장 왕복 거리", dist_ev)
            transport = round_trip * per_km * pickups
            rec.number(
                "monthlyTransport",
                transport,
                "원/월",
                "왕복 거리 × 운행 단가 × 월 수거 횟수",
                [*per_km_ev, *dist_ev],
            )
            output["transportAssumed"] = assumed or bool(
                per_km_ev and per_km_ev[0].startswith("assumption:")
            )
            if output["transportAssumed"]:
                rec.check(
                    "transport",
                    "conditional",
                    "운송비는 가정값으로 계산한 추정입니다. 견적이 아닙니다.",
                    "차량, 회차, 상하차와 포함 항목을 밝힌 운송 견적을 알려 주세요.",
                    [*per_km_ev, *dist_ev],
                )
    if transport is None:
        rec.check(
            "transport",
            "conditional",
            "거리나 운행 단가가 없어 운송비를 계산하지 않았습니다. 확인 필요.",
            "차량, 회차, 상하차와 포함 항목을 밝힌 운송 견적을 알려 주세요.",
        )
    borne = transport if (transport is not None and transportPaidBy == "emitter") else 0.0
    after = revenue - borne
    rec.number("monthlyAfter", after, "원/월", "월 매입액 − 배출 기업이 내는 운송비", crit_ev)
    output["current"], _ = baseline(rec, stream, kg, after)

    item = stream.get("wasteItem")
    now_ef, after_ef = (
        rec.basis(f"emission.incinerate.{item}"),
        rec.basis(f"emission.recycle.{item}"),
    )
    if now_ef is None or after_ef is None:
        rec.check("carbon", "conditional", "배출계수가 없어 탄소를 계산하지 않았습니다. 확인 필요.")
    else:
        delta = (float(after_ef.value) - float(now_ef.value)) * kg / 1000
        claim = rec.number(
            "carbonDifference",
            delta,
            "tCO2e/월",
            "(재활용 계수 − 소각 계수) × 월 발생량",
            [now_ef.id, after_ef.id],
        )
        output["carbon"] = claim["id"]
        kau = rec.basis("kau.price")
        if kau is None:
            rec.check("kau", "conditional", "KAU 시세 근거가 없어 환산하지 않았습니다. 확인 필요.")
        else:
            output["kau"] = rec.number(
                "kauValue", -delta * float(kau.value), "원/월", "감축량 × KAU 단가", [kau.id]
            )["id"]
    conditional = any(c["status"] == "conditional" for c in rec.checks)
    return rec.result("conditional" if conditional else "calculated", output)


def _tour(matrix, nodes) -> float:
    """차고지(0)에서 나가 nodes를 돌고 돌아오는 가장 짧은 거리. 완전 탐색."""
    if not nodes:
        return 0.0
    best = math.inf
    for order in itertools.permutations(nodes):
        d = matrix[0][order[0]] + matrix[order[-1]][0]
        d += sum(matrix[a][b] for a, b in itertools.pairwise(order))
        best = min(best, d)
    return best


def _nearest(matrix, nodes) -> float:
    left, at, total = list(nodes), 0, 0.0
    while left:
        nxt = min(left, key=lambda n: (matrix[at][n], n))
        total, at = total + matrix[at][nxt], nxt
        left.remove(nxt)
    return total + matrix[at][0]


def plan_trips(matrix, loads, cap_kg, cap_m3):
    """(총 거리, 회차 수). 무게와 부피를 함께 지키는 최단 분할. 지점이 적으면 정확한 답이다."""
    n = len(loads)
    if n == 0:
        return 0.0, 0

    def fits(mask):
        kg = sum(loads[i][0] for i in range(n) if mask >> i & 1)
        m3 = sum(loads[i][1] for i in range(n) if mask >> i & 1)
        return kg <= cap_kg + 1e-9 and (cap_m3 is None or m3 <= cap_m3 + 1e-9)

    if any(not fits(1 << i) for i in range(n)):
        raise ValueError("한 지점의 물량이 차량 용량을 넘는다")
    exact = n <= EXACT_LIMIT
    cost = {}
    for mask in range(1, 1 << n):
        if fits(mask):
            nodes = [i + 1 for i in range(n) if mask >> i & 1]
            cost[mask] = _tour(matrix, nodes) if exact else _nearest(matrix, nodes)
    full = (1 << n) - 1
    if not exact:  # 큰 문제: 용량이 차는 대로 끊는 탐욕 분할
        total, trips, mask = 0.0, 0, 0
        for i in range(n):
            if not fits(mask | 1 << i):
                total, trips, mask = total + cost[mask], trips + 1, 0
            mask |= 1 << i
        return total + cost[mask], trips + 1
    best = {0: (0.0, 0)}
    for mask in range(1, full + 1):
        low = mask & -mask
        options = []
        sub = mask
        while sub:
            if sub & low and sub in cost:
                prev = best[mask ^ sub]
                options.append((round(prev[0] + cost[sub], 9), prev[1] + 1))
            sub = (sub - 1) & mask
        best[mask] = min(options)
    return best[full]


def route_extra_cost(registry, *, org_id, today, vendor, stops, newStop, distance_provider=None):
    args = {"vendor": vendor, "stops": stops, "newStop": newStop}
    rec = Recorder("route_extra_cost", registry, org_id, today, args, [vendor.get("orgId")])
    fleet = vendor.get("fleet") or {}
    fleet_ev = list(fleet.get("evidence") or [])
    depot, cap_kg, cap_m3 = (
        fleet.get("depot"),
        num(fleet.get("capacityKg")),
        num(fleet.get("capacityM3")),
    )
    per_km = num(fleet.get("costPerKm"))
    output = {"vendorOrgId": vendor.get("orgId"), "distanceAssumed": None, "exact": None}
    if not depot or cap_kg is None or per_km is None:
        rec.check(
            "fleet", "conditional", "차고지, 적재 용량, 운행 단가가 있어야 계산합니다. 확인 필요."
        )
        return rec.result("needs_information", output)
    every = [*stops, newStop]
    if any(num(s.get("kg")) is None or not s.get("location") for s in every):
        rec.check("stops", "conditional", "위치나 물량이 없는 정류장이 있습니다. 확인 필요.")
        return rec.result("needs_information", output)
    matrix, assumed, dist_ev = distance_matrix(
        rec, [depot, *[s["location"] for s in every]], distance_provider
    )
    if matrix is None:
        rec.check("distance", "conditional", "거리를 계산할 근거가 없습니다. 확인 필요.")
        return rec.result("needs_information", output)
    loads = [(float(s["kg"]), num(s.get("m3")) or 0.0) for s in every]
    volume_known = cap_m3 is not None and all(num(s.get("m3")) is not None for s in every)
    cap = cap_m3 if volume_known else None
    try:
        base_km, base_trips = plan_trips([r[:-1] for r in matrix[:-1]], loads[:-1], cap_kg, cap)
        new_km, new_trips = plan_trips(matrix, loads, cap_kg, cap)
    except ValueError:
        rec.check(
            "loading",
            "unmet",
            "한 지점의 물량이 차량 용량을 넘어 노선에 넣을 수 없습니다.",
            None,
            fleet_ev,
        )
        return rec.result("calculated", {**output, "feasible": False})
    ev = [*fleet_ev, *dist_ev]
    rec.number("baseKm", base_km, "km", "기존 노선의 최단 거리", ev)
    rec.number("newKm", new_km, "km", "정류장을 더한 노선의 최단 거리", ev)
    rec.number("extraKm", new_km - base_km, "km", "더한 노선 거리 − 기존 노선 거리", ev)
    rec.number("extraCost", (new_km - base_km) * per_km, "원/회", "늘어난 거리 × 운행 단가", ev)
    rec.number("trips", new_trips, "회", "용량을 지키는 데 필요한 회차 수", ev)
    output.update(feasible=True, distanceAssumed=assumed, exact=len(every) <= EXACT_LIMIT)
    output["extraTrips"] = new_trips - base_trips
    if not volume_known:
        rec.check("loading_volume", "conditional", "부피 정보가 없어 무게만으로 적재를 봤습니다.")
    if assumed:
        rec.check(
            "distance",
            "conditional",
            "거리는 직선 거리에 우회 계수를 곱한 가정값입니다.",
            None,
            dist_ev,
        )
    conditional = any(c["status"] == "conditional" for c in rec.checks)
    return rec.result("conditional" if conditional else "calculated", output)
