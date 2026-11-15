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
