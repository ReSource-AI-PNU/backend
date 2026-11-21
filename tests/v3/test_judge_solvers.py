"""OR-Tools 경로와 순수 파이썬 풀이의 선택, 검증, 같은 답.

OR-Tools가 없는 곳에서도 도는 부분(가짜 풀이를 넣어 선택과 검증을 본다)과, OR-Tools가 있어야
도는 비교(importorskip)를 나눴다. 회사와 수치는 모두 가상이다.
"""

import itertools

import pytest

from app.v3.evidence.registry import InMemoryRegistry
from app.v3.judge import assign, economics
from app.v3.judge.defaults import register_defaults
from app.v3.judge.toolbox import call_tool
from tests.v3.judge_fixtures import EMITTER, TODAY, VENDOR, vendor
from tests.v3.judge_fixtures import answered_stream as stream

LINE = [[0, 1, 2, 3], [1, 0, 1, 2], [2, 1, 0, 1], [3, 2, 1, 0]]  # 차고지와 세 지점이 한 줄에
UNIT = [(1, 0), (1, 0), (1, 0)]


@pytest.fixture
def reg():
    registry = InMemoryRegistry()
    register_defaults(registry, TODAY)
    return registry


def run(registry, name, org=EMITTER, **arguments):
    return call_tool(name, arguments, registry=registry, org_id=org, today=TODAY)


def ring(count):
    """원 위에 놓인 지점들의 거리 행렬(차고지 포함 count + 1개). 정수 거리라 비교가 쉽다."""
    points = [(0, 0)] + [(10 * (i + 1), 7 * ((i * 3) % 5)) for i in range(count)]
    return [[abs(a[0] - b[0]) + abs(a[1] - b[1]) for b in points] for a in points]


# ── 회차 나누기: 풀이 선택과 검증 (순수 파이썬) ──────────────────────
def test_without_ortools_the_pure_solution_is_used():
    assert economics.plan_trips(LINE, UNIT, 2, None) == (8.0, 2)
    assert economics.solve_trips(LINE, UNIT, 2, None, ortools_solver=None) == (8.0, 2, "exact")
    big = ring(economics.EXACT_LIMIT + 1)
    loads = [(1, 0)] * (economics.EXACT_LIMIT + 1)
    km, trips, solver = economics.solve_trips(big, loads, 3, None, ortools_solver=None)
    assert solver == "greedy" and (km, trips) == economics.plan_trips(big, loads, 3, None)
    assert economics.solve_trips([[0]], [], 2, None, ortools_solver=None) == (0.0, 0, "exact")


def test_small_problems_keep_the_exact_numbers_whatever_ortools_says():
    exact = economics.plan_trips(LINE, UNIT, 2, None)
    same = economics.solve_trips(LINE, UNIT, 2, None, ortools_solver=lambda *_: [[1], [2, 3]])
    assert same == (*exact, "ortools")  # 같은 답일 때만 ortools라고 남긴다
    worse = economics.solve_trips(LINE, UNIT, 2, None, ortools_solver=lambda *_: [[1], [2], [3]])
    assert worse == (*exact, "exact")
    for broken in (
        lambda *_: [[1, 2, 3]],  # 용량 초과
        lambda *_: [[1], [2]],  # 빠진 지점
        lambda *_: [[1, 2], [2, 3]],  # 겹친 지점
        lambda *_: [[1, 2], [], [3]],  # 빈 회차
        lambda *_: 1 / 0,  # 풀이가 죽음
    ):
        assert economics.solve_trips(LINE, UNIT, 2, None, ortools_solver=broken) == (
            *exact,
            "exact",
        )


def test_large_problems_use_ortools_only_when_it_beats_greedy():
    count = economics.EXACT_LIMIT + 2
    matrix, loads = ring(count), [(1, 1)] * count
    greedy = economics.plan_trips(matrix, loads, 4, 4)
    nodes = list(range(1, count + 1))
    # 가까운 지점끼리 묶어 도는 답(탐욕 분할보다 짧거나 같다)
    by_row = sorted(nodes, key=lambda n: (matrix[0][n] - 10 * n, n))
    better = [by_row[i : i + 4] for i in range(0, count, 4)]
    km, trips, solver = economics.solve_trips(matrix, loads, 4, 4, ortools_solver=lambda *_: better)
    cost = economics._routes_cost(matrix, loads, 4, 4, better)
    if cost[0] < greedy[0]:
        assert (km, trips, solver) == (*cost, "ortools")
    else:
        assert (km, trips, solver) == (*greedy, "greedy")
    longer = [[n] for n in nodes]  # 지점마다 따로 도는 답은 더 길다
    assert economics.solve_trips(matrix, loads, 4, 4, ortools_solver=lambda *_: longer) == (
        *greedy,
        "greedy",
    )
    over = [nodes]  # 용량을 넘는다
    assert economics.solve_trips(matrix, loads, 4, 4, ortools_solver=lambda *_: over) == (
        *greedy,
        "greedy",
    )


def test_volume_capacity_is_checked_in_the_ortools_answer():
    loads = [(1, 2), (1, 2), (1, 2)]
    exact = economics.plan_trips(LINE, loads, 3, 2)
    assert exact == (12.0, 3)  # 부피 때문에 지점마다 따로 돈다
    merged = economics.solve_trips(LINE, loads, 3, 2, ortools_solver=lambda *_: [[1, 2, 3]])
    assert merged == (*exact, "exact")  # 무게는 되지만 부피가 넘는다


def road_km(points):
    """가상 도로 거리 제공자(시험 전용). 직선 × 우회 계수 가정값 없이 노선 계산을 보려고 쓴다."""
    return [[round((abs(a[0] - b[0]) + abs(a[1] - b[1])) * 100, 3) for b in points] for a in points]


def test_route_extra_cost_records_the_solver(reg, monkeypatch):
    v = vendor()
    stops = [{"location": [128.86, 35.10], "kg": 1000, "m3": 4}]
    new = {"location": [128.85, 35.09], "kg": 600, "m3": 3}

    def route():
        return call_tool(
            "route_extra_cost",
            {"vendor": v, "stops": stops, "newStop": new},
            registry=reg,
            org_id=VENDOR,
            today=TODAY,
            distance_provider=road_km,
        )

    monkeypatch.setattr(economics, "HAS_ORTOOLS", False)
    # 도로 거리가 없으면 노선을 풀지 않는다(풀이 이름도 남기지 않는다)
    held = run(reg, "route_extra_cost", org=VENDOR, vendor=v, stops=stops, newStop=new)
    assert held["status"] == "needs_information" and held["output"]["solver"] is None
    pure = route()
    assert pure["output"]["solver"] == "exact" and pure["output"]["exact"] is True

    def fake(matrix, loads, cap_kg, cap_m3):
        km, _trips = economics.plan_trips(matrix, loads, cap_kg, cap_m3)
        nodes = list(range(1, len(loads) + 1))
        for order in itertools.permutations(nodes):  # 한 회차로 도는 가장 짧은 순서
            if abs(economics._routes_cost(matrix, loads, cap_kg, cap_m3, [list(order)])[0] - km) < (
                economics.SAME_KM
            ):
                return [list(order)]
        raise AssertionError("이 시험은 한 회차로 끝나는 물량을 쓴다")

    monkeypatch.setattr(economics, "HAS_ORTOOLS", True)
    monkeypatch.setattr(economics, "ortools_routes", fake)
    with_solver = route()
    assert with_solver["output"]["solver"] == "ortools"

    # 풀이 이름 말고는 수치, 근거 ID, 판정이 모두 같다
    def strip(result):
        return {**result, "output": {**result["output"], "solver": None}}

    assert strip(with_solver) == strip(pure)

    monkeypatch.setattr(economics, "ortools_routes", lambda *_: 1 / 0)
    broken = route()
    assert broken == pure


# ── 배정: 풀이 선택과 검증 (순수 파이썬) ─────────────────────────────
def twin_vendor():
    return vendor(orgId="org_aaa", criteria=vendor()["criteria"])


def assignments(result):
    return [(a["streamId"], a["vendorOrgId"]) for a in result["output"]["assignments"]]


def test_assignment_keeps_the_exhaustive_answer_whatever_cp_sat_says(reg, monkeypatch):
    streams = [stream(), stream(id="st_b", monthlyKg=700)]
    vendors = [vendor(), twin_vendor()]
    monkeypatch.setattr(assign, "HAS_ORTOOLS", False)
    pure = run(reg, "optimize_assignment", streams=streams, vendors=vendors)
    assert pure["output"]["solver"] == "exact"
    args = {"org_id": EMITTER, "today": TODAY, "streams": streams, "vendors": vendors}

    def right(usable, options, weight):
        best = None
        for combo in itertools.product(*[range(len(o)) for o in options]):
            key = (
                -round(sum(options[i][j]["value"] for i, j in enumerate(combo)), 6),
                tuple(assign._name(options[i][j]) for i, j in enumerate(combo)),
            )
            if best is None or key < best[0]:
                best = (key, combo)
        return best[1]

    agreed = assign.optimize_assignment(reg, **args, cp_solver=right)
    assert agreed["output"]["solver"] == "ortools"
    assert assignments(agreed) == assignments(pure) and agreed["claims"] == pure["claims"]

    for wrong in (
        lambda usable, options, weight: tuple(0 for _ in options),  # 전부 그대로 둔다(최적이 아님)
        lambda usable, options, weight: (0,),  # 길이가 다르다
        lambda usable, options, weight: None,  # 못 풀었다
        lambda usable, options, weight: 1 / 0,  # 풀이가 죽음
    ):
        fallback = assign.optimize_assignment(reg, **args, cp_solver=wrong)
        assert fallback["output"]["solver"] == "exact"
        assert fallback == pure


def test_infeasible_cp_sat_answer_is_rejected(reg):
    """최소 물량에 못 미치는 배정을 CP-SAT가 내면 쓰지 않는다."""
    small = stream(monthlyKg=100)  # 매입 기준의 최소 물량보다 적다
    args = {"org_id": EMITTER, "today": TODAY, "streams": [small], "vendors": [vendor()]}
    pure = assign.optimize_assignment(reg, **args, cp_solver=None)
    assert assignments(pure) == [(small["id"], None)]
    forced = assign.optimize_assignment(
        reg, **args, cp_solver=lambda usable, options, weight: (len(options[0]) - 1,)
    )
    assert forced == pure and forced["output"]["solver"] == "exact"


def test_too_many_combinations_need_cp_sat(reg, monkeypatch):
    monkeypatch.setattr(assign, "MAX_COMBINATIONS", 1)
    args = {"org_id": EMITTER, "today": TODAY, "streams": [stream()], "vendors": [vendor()]}
    with pytest.raises(ValueError, match="조합이 너무 많다"):
        assign.optimize_assignment(reg, **args, cp_solver=None)
    with pytest.raises(ValueError, match="조합이 너무 많다"):
        assign.optimize_assignment(reg, **args, cp_solver=lambda *_: None)
    solved = assign.optimize_assignment(
        reg, **args, cp_solver=lambda usable, options, weight: (len(options[0]) - 1,)
    )
    assert solved["output"]["solver"] == "ortools"
    assert assignments(solved) == [(stream()["id"], VENDOR)]


def test_solver_cannot_be_chosen_through_tool_input(reg):
    with pytest.raises(ValueError, match="알 수 없는 입력"):
        run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()], cp_solver=None)


# ── OR-Tools가 있을 때만: 두 풀이의 답이 같은지 ──────────────────────
def test_ortools_cvrp_matches_exact_search():
    pytest.importorskip("ortools")
    cases = [
        (LINE, UNIT, 2, None),
        (LINE, UNIT, 3, None),
        (LINE, [(1, 2), (1, 2), (1, 2)], 3, 2),
        (ring(5), [(2, 1), (1, 1), (3, 2), (1, 1), (2, 2)], 5, 4),
        (ring(7), [(1, 1)] * 7, 3, 3),
    ]
    for matrix, loads, cap_kg, cap_m3 in cases:
        exact = economics.plan_trips(matrix, loads, cap_kg, cap_m3)
        routes = economics.ortools_routes(matrix, loads, cap_kg, cap_m3)
        found = economics._routes_cost(matrix, loads, cap_kg, cap_m3, routes)
        assert abs(found[0] - exact[0]) <= economics.SAME_KM, (matrix, loads, routes)
        assert economics.solve_trips(matrix, loads, cap_kg, cap_m3) == (*exact, "ortools")


def test_ortools_cvrp_is_no_worse_than_greedy_on_large_problems():
    pytest.importorskip("ortools")
    count = economics.EXACT_LIMIT + 4
    matrix, loads = ring(count), [(1, 1)] * count
    greedy = economics.plan_trips(matrix, loads, 4, 4)
    km, trips, solver = economics.solve_trips(matrix, loads, 4, 4)
    assert km <= greedy[0] and solver in ("ortools", "greedy") and trips >= 3


def test_cp_sat_matches_exhaustive_search(reg):
    pytest.importorskip("ortools")
    payer = vendor(orgId="org_payer")
    payer["criteria"] = [{**payer["criteria"][0], "unitPriceKrwPerKg": -50, "minMonthlyKg": 0}]
    cases = [
        ([stream()], [vendor()], 0),
        ([stream(), stream(id="st_b", monthlyKg=700)], [vendor(), twin_vendor()], 0),
        ([stream(monthlyKg=100)], [vendor()], 0),  # 최소 물량 미달
        ([stream(monthlyKg=300), stream(id="st_b", monthlyKg=300)], [vendor()], 0),  # 합쳐서 충족
        ([stream()], [payer], 0),
        ([stream()], [payer], 100000),  # 탄소 가중치
        ([stream(bulkDensityKgPerM3=None), stream(id="st_b", monthlyKg=900)], [vendor()], 0),
    ]
    for streams, vendors, weight in cases:
        args = {
            "org_id": EMITTER,
            "today": TODAY,
            "streams": streams,
            "vendors": vendors,
            "carbonWeight": weight,
        }
        pure = assign.optimize_assignment(reg, **args, cp_solver=None)
        both = assign.optimize_assignment(reg, **args)
        assert both["output"]["solver"] == "ortools", (streams, vendors, weight)
        assert assignments(both) == assignments(pure) and both["claims"] == pure["claims"]
