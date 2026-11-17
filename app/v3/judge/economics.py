"""⑤ 비용·탄소 계산과 업체 노선 추가 운송비."""

from __future__ import annotations

import itertools
import logging
import math

from app.v3.judge.common import (
    CARBON_FORMULA,
    Recorder,
    best_criterion,
    carbon_per_kg,
    distance_matrix,
    labor_cost,
    num,
    quality,
    transport_charge,
)
from app.v3.judge.route import baseline, current_cost

try:  # 없으면 순수 파이썬 풀이로만 돈다
    from ortools.constraint_solver import pywrapcp  # noqa: F401

    HAS_ORTOOLS = True
except Exception:  # noqa: BLE001
    HAS_ORTOOLS = False

logger = logging.getLogger(__name__)

EXACT_LIMIT = 8
SOLVER_SCALE = 1000  # OR-Tools는 정수만 받는다: km→m, kg→g, m3→L
SAME_KM = 1e-6  # 두 풀이의 거리가 같다고 보는 차이
_AUTO = object()


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
    paid_by = transportPaidBy
    charge = transport_charge(vendor, pickupsPerMonth)
    if charge["known"]:
        # 업체가 운반 조건을 밝혔다: 단가에 포함이거나, 1회 운반비를 따로 받는다
        paid_by, terms_ev = charge["paidBy"], charge["evidence"]
        if paid_by == "vendor":
            transport = 0.0
            rec.check(
                "transport", "met", "운반비는 업체 단가에 들어 있습니다(수거 포함).", None, terms_ev
            )
        elif charge["monthly"] is not None:
            transport = charge["monthly"]
            rec.number(
                "transportPerPickup",
                charge["perPickup"],
                "원/회",
                "업체가 밝힌 1회 운반비",
                terms_ev,
            )
            rec.number(
                "monthlyTransport",
                transport,
                "원/월",
                "1회 운반비 × 월 수거 횟수 (이 흐름만 보낼 때)",
                terms_ev,
            )
            output["transportAssumed"] = False
        else:
            rec.check(
                "transport",
                "conditional",
                "운반비를 따로 내야 하는데 금액이 없습니다. 확인 필요.",
                "1회 운반비와 월 수거 횟수를 알려 주세요.",
                terms_ev,
            )
    else:
        transport = _estimated_transport(
            rec, output, vendor, origin, pickupsPerMonth, distance_provider
        )
    borne = transport if (transport is not None and paid_by == "emitter") else 0.0
    labor, labor_ev, hours, rate = labor_cost(rec, stream)
    if labor is not None:
        rec.number("pretreatHours", hours, "시간/월", "담당자가 넣은 월 전처리 작업 시간", labor_ev)
        rec.number("laborRate", rate, "원/시간", "전처리 작업의 시간당 인건비", labor_ev)
        rec.number("monthlyLabor", labor, "원/월", "월 작업 시간 × 시간당 인건비", labor_ev)
    after = revenue - borne - (labor or 0.0)
    rec.number(
        "monthlyAfter",
        after,
        "원/월",
        "월 매입액 − 배출 기업이 내는 운반비 − 전처리 인건비",
        [*crit_ev, *labor_ev],
    )
    output["current"], _ = baseline(rec, stream, kg, after)

    method = current_cost(rec, stream)[3]
    per_kg, carbon_ev, why = carbon_per_kg(rec, stream, vendor, price, method)
    if per_kg is None:
        rec.check("carbon", "conditional", why)
    else:
        delta = per_kg * kg
        claim = rec.number("carbonDifference", delta, "tCO2e/월", CARBON_FORMULA, carbon_ev)
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


def _estimated_transport(rec, output, vendor, origin, pickups_per_month, distance_provider):
    """업체가 운반 조건을 밝히지 않았을 때: 거리 × 운행 단가 × 월 횟수로 어림한다(가정값)."""
    transport = None
    fleet = vendor.get("fleet") or {}
    depot = fleet.get("depot")
    pickups = num(pickups_per_month) or num(
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
    return transport


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


def ortools_routes(matrix, loads, cap_kg, cap_m3):
    """OR-Tools CVRP로 회차별 방문 순서를 구한다(app.platform.routing의 CVRP를 무게·부피로 확장).

    로컬 미실행: 작성한 환경에서는 ortools를 불러올 수 없었다. 결과는 solve_trips가 순수 파이썬
    검사(_routes_cost)로 다시 확인한 뒤에만 쓴다.
    """
    from app.platform.routing import capacity_routes

    scaled = [[round(float(v) * SOLVER_SCALE) for v in row] for row in matrix]
    capacities = [math.floor(cap_kg * SOLVER_SCALE + SAME_KM)]
    demands = [[round(kg * SOLVER_SCALE)] for kg, _ in loads]
    if cap_m3 is not None:
        capacities.append(math.floor(cap_m3 * SOLVER_SCALE + SAME_KM))
        demands = [[round(kg * SOLVER_SCALE), round(m3 * SOLVER_SCALE)] for kg, m3 in loads]
    return capacity_routes(scaled, demands, capacities)


def _routes_cost(matrix, loads, cap_kg, cap_m3, routes):
    """(총 거리, 회차 수). 모든 지점을 한 번씩 돌고 용량을 지키는지 확인한다. 아니면 ValueError."""
    seen: list[int] = []
    total = []
    for route in routes:
        nodes = [int(node) for node in route]
        if not nodes:
            raise ValueError("빈 회차가 있다")
        kg = math.fsum(loads[n - 1][0] for n in nodes if 1 <= n <= len(loads))
        m3 = math.fsum(loads[n - 1][1] for n in nodes if 1 <= n <= len(loads))
        if kg > cap_kg + 1e-9 or (cap_m3 is not None and m3 > cap_m3 + 1e-9):
            raise ValueError("용량을 넘는 회차가 있다")
        seen.extend(nodes)
        legs = [matrix[0][nodes[0]], matrix[nodes[-1]][0]]
        legs += [matrix[a][b] for a, b in itertools.pairwise(nodes)]
        total.append(math.fsum(legs))
    if sorted(seen) != list(range(1, len(loads) + 1)):
        raise ValueError("빠지거나 겹친 지점이 있다")
    return round(math.fsum(total), 9), len(routes)


def solve_trips(matrix, loads, cap_kg, cap_m3, *, ortools_solver=_AUTO):
    """(총 거리, 회차 수, 쓴 풀이). 풀이는 "ortools" | "exact" | "greedy".

    순수 풀이(plan_trips)는 그대로 돌린다. OR-Tools가 있으면 그것도 풀고, 순수 파이썬으로
    다시 확인한 답만 쓴다.
    - 지점이 EXACT_LIMIT 이하: 완전 탐색이 정답이다. OR-Tools가 같은 답을 냈을 때만 "ortools",
      다르거나 실패하면 완전 탐색의 답을 쓰고 "exact"라고 남긴다(어느 쪽이든 수치는 같다).
    - 그보다 많으면: OR-Tools의 답이 탐욕 분할보다 짧을 때만 그 답을 쓴다. 아니면 "greedy".
    ortools_solver는 테스트에서 가짜로 바꾼다: (matrix, loads, cap_kg, cap_m3) -> 회차별 방문 순서.
    """
    km, trips = plan_trips(matrix, loads, cap_kg, cap_m3)
    pure = "exact" if len(loads) <= EXACT_LIMIT else "greedy"
    if ortools_solver is _AUTO:
        ortools_solver = ortools_routes if HAS_ORTOOLS else None
    if ortools_solver is None or not loads:
        return km, trips, pure
    try:
        routes = ortools_solver(matrix, loads, cap_kg, cap_m3)
        found_km, found_trips = _routes_cost(matrix, loads, cap_kg, cap_m3, routes)
    except Exception as error:  # noqa: BLE001 - 어떤 실패든 순수 풀이의 답으로 돌아간다
        logger.warning("OR-Tools CVRP를 쓰지 못해 순수 풀이로 돈다: %s", type(error).__name__)
        return km, trips, pure
    if pure == "exact":
        same = abs(found_km - km) <= SAME_KM and found_trips == trips
        return km, trips, "ortools" if same else "exact"
    if found_km < km - SAME_KM:
        return found_km, found_trips, "ortools"
    return km, trips, pure


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
    output = {
        "vendorOrgId": vendor.get("orgId"),
        "distanceAssumed": None,
        "exact": None,
        "solver": None,
    }
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
        base_km, base_trips, base_solver = solve_trips(
            [r[:-1] for r in matrix[:-1]], loads[:-1], cap_kg, cap
        )
        new_km, new_trips, solver = solve_trips(matrix, loads, cap_kg, cap)
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
    # 정류장을 더한 노선을 푼 풀이. 기존 노선의 풀이가 다르면 따로 남긴다
    output["solver"] = solver
    if stops and base_solver != solver:
        output["baseSolver"] = base_solver
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
