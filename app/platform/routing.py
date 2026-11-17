"""CVRP with explicit capacities; distances are geodesic estimates, not road routes."""

import math

from ortools.constraint_solver import pywrapcp, routing_enums_pb2


def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return round(6371000 * 2 * math.asin(min(1, math.sqrt(h))))


def route_plan(depot, members, truck_kg, shift=(480, 1080), matrix_provider=None):
    # Split an oversized stop into multiple visits; never overload a vehicle.
    stops = []
    for m in members:
        remaining = round(m["kg"] * 1000)
        while remaining > 0:
            kg = min(remaining, math.floor(truck_kg * 1000))
            if kg <= 0:
                raise ValueError("차량 적재량은 0.001kg 이상이어야 합니다")
            stops.append({**m, "grams": kg})
            remaining -= kg
            if len(stops) > 200:
                raise ValueError("수거 지점이 너무 많습니다. 물량 또는 차량 용량을 조정하세요")
    if not stops:
        return []
    points = [depot] + [s["location"] for s in stops]
    road = matrix_provider(points) if matrix_provider else None
    matrix = road[0] if road else [[distance(a, b) for b in points] for a in points]
    minutes = road[1] if road else [[math.ceil(m / 500) for m in row] for row in matrix]
    manager = pywrapcp.RoutingIndexManager(len(points), len(stops), 0)
    routing = pywrapcp.RoutingModel(manager)
    callback = routing.RegisterTransitCallback(
        lambda a, b: matrix[manager.IndexToNode(a)][manager.IndexToNode(b)]
    )
    routing.SetArcCostEvaluatorOfAllVehicles(callback)
    demands = [0] + [s["grams"] for s in stops]
    demand = routing.RegisterUnaryTransitCallback(lambda i: demands[manager.IndexToNode(i)])
    routing.AddDimensionWithVehicleCapacity(
        demand, 0, [math.floor(truck_kg * 1000)] * len(stops), True, "Capacity"
    )
    routing.SetFixedCostOfAllVehicles(10_000_000)
    service = [0] + [s.get("serviceMinutes", 15) for s in stops]
    transit = routing.RegisterTransitCallback(
        lambda a, b: (
            minutes[manager.IndexToNode(a)][manager.IndexToNode(b)]
            + service[manager.IndexToNode(a)]
        )
    )
    routing.AddDimension(transit, 1440, 1440, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    for i, s in enumerate(stops, 1):
        window = s.get("window", shift)
        time_dim.CumulVar(manager.NodeToIndex(i)).SetRange(window[0], window[1])
    for v in range(len(stops)):
        time_dim.CumulVar(routing.Start(v)).SetRange(*shift)
        time_dim.CumulVar(routing.End(v)).SetRange(*shift)
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.time_limit.FromSeconds(1)
    solution = routing.SolveWithParameters(params)
    if solution is None:
        raise ValueError("적재 한도 안에서 순회 경로를 찾지 못했습니다")
    trips = []
    for vehicle in range(len(stops)):
        index, visits, meters, grams = routing.Start(vehicle), [], 0, 0
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node:
                stop = stops[node - 1]
                visits.append(
                    {
                        "companyId": stop["companyId"],
                        "kg": stop["grams"] / 1000,
                        "arrivalMinute": solution.Min(time_dim.CumulVar(index)),
                    }
                )
                grams += stop["grams"]
            nxt = solution.Value(routing.NextVar(index))
            meters += matrix[node][manager.IndexToNode(nxt)]
            index = nxt
        if visits:
            trips.append({"visits": visits, "kg": grams / 1000, "distanceM": meters})
    return trips


def capacity_routes(matrix, demands, capacities, vehicles=None, time_limit_s=1):
    """용량 차원이 여러 개(무게, 부피)인 CVRP. 설계안 v3 판단 도구(route_extra_cost)가 쓴다.

    로컬 미실행: 작성한 환경에서는 ortools를 불러올 수 없었다. route_plan과 같은 호출만 쓴다.

    - matrix: 정수 거리 행렬. 0번이 차고지, 1..n이 지점이다.
    - demands: 지점마다 차원별 정수 수요 [(무게, 부피), ...]. capacities: 차원별 차량 용량.
    - 돌려주는 값: 회차마다 방문 순서(지점 번호 목록). 총 거리만 줄이고 차량 고정비는 두지 않는다.
    - 메타휴리스틱을 켜지 않아(기본 지역 탐색) 같은 입력이면 같은 답이 나온다.
    """
    count = len(matrix) - 1
    if count <= 0:
        return []
    if len(demands) != count:
        raise ValueError("수요의 개수가 지점 수와 다릅니다")
    vehicles = vehicles or count
    manager = pywrapcp.RoutingIndexManager(len(matrix), vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)
    callback = routing.RegisterTransitCallback(
        lambda a, b: matrix[manager.IndexToNode(a)][manager.IndexToNode(b)]
    )
    routing.SetArcCostEvaluatorOfAllVehicles(callback)
    for dimension, capacity in enumerate(capacities):
        column = [0] + [int(demand[dimension]) for demand in demands]
        demand_callback = routing.RegisterUnaryTransitCallback(
            lambda index, column=column: column[manager.IndexToNode(index)]
        )
        routing.AddDimensionWithVehicleCapacity(
            demand_callback, 0, [int(capacity)] * vehicles, True, f"Capacity{dimension}"
        )
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.time_limit.FromSeconds(time_limit_s)
    solution = routing.SolveWithParameters(params)
    if solution is None:
        raise ValueError("적재 한도 안에서 순회 경로를 찾지 못했습니다")
    routes = []
    for vehicle in range(vehicles):
        index, visits = routing.Start(vehicle), []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node:
                visits.append(node)
            index = solution.Value(routing.NextVar(index))
        if visits:
            routes.append(visits)
    return routes
