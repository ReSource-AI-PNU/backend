"""보관장 정리안: 더미를 폐기물별 구역으로 옮겨 놓는 배치를 코드가 정한다(모델이 그리지 않는다).

목적은 둘이다. (1) 서로 다른 폐기물 더미 사이를 gap_m 이상 띄워 섞임을 줄이고, (2) 상차 지점까지의
부피 가중 평균 거리를 줄인다. 제약: 바닥 다각형 안, 벽에서 wall_m 안쪽, 상차 지점 반경 pickup_clear_m은
비운다, 같은 폐기물끼리는 SAME_GAP_M 띄운다, 더미는 바닥의 주 방향에 맞춰 0°/90°로만 놓는다.

순서: 폐기물을 부피 합이 큰 것부터 상차 지점 가까이, 폐기물 안에서는 큰 더미부터 탐욕 배치한다(격자 GRID_M).
폐기물을 지정하지 않은 더미는 제자리에 두고 장애물로 본다. 같은 입력이면 같은 결과다(난수·시계 없음).

좌표는 바닥 좌표계(m): x·y는 바닥 위, 재구성 서버(infra/h100/recon)가 정한다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

Point = tuple[float, float]

GRID_M = 0.1  # 후보 자리 간격
SAME_GAP_M = 0.2  # 같은 폐기물 더미 사이
DEFAULT_RULES = {"gapM": 1.0, "wallM": 0.3, "pickupClearM": 1.5}
MAX_CANDIDATES = 12000  # 넓은 바닥에서는 격자를 넓힌다


# ── 기하 ─────────────────────────────────────────────────────────────
def polygon_area(poly: Sequence[Point]) -> float:
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def contains(poly: Sequence[Point], p: Point) -> bool:
    """점이 다각형 안에 있는가(경계 포함 여부는 가리지 않는다. 짝홀 규칙)."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            t = (y - y1) / (y2 - y1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def polygon_distance(p: Sequence[Point], q: Sequence[Point]) -> float:
    """두 단순 다각형 사이의 최소 거리. 겹치거나 한쪽이 다른 쪽 안에 있으면 0."""
    if any(contains(q, v) for v in p) or any(contains(p, v) for v in q):
        return 0.0
    np_, nq = len(p), len(q)
    for i in range(np_):
        for j in range(nq):
            if _segments_cross(p[i], p[(i + 1) % np_], q[j], q[(j + 1) % nq]):
                return 0.0
    best = math.inf
    for v in p:
        for j in range(nq):
            best = min(best, _segment_distance(v, q[j], q[(j + 1) % nq]))
    for v in q:
        for i in range(np_):
            best = min(best, _segment_distance(v, p[i], p[(i + 1) % np_]))
    return best


def rectangle(center: Point, size: Sequence[float], yaw_deg: float) -> list[Point]:
    """가운데, [긴 변, 짧은 변], 긴 변의 방향(도)으로 네 꼭짓점(반시계)."""
    cx, cy = center
    hl, hw = size[0] / 2.0, size[1] / 2.0
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    corners = [(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)]
    return [(cx + x * c - y * s, cy + x * s + y * c) for x, y in corners]


def convex_hull(points: Sequence[Point]) -> list[Point]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return list(pts)

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def main_axis_deg(poly: Sequence[Point]) -> float:
    """바닥의 주 방향: 볼록 껍질의 변 방향 가운데 감싸는 사각형 넓이가 가장 작은 것(0~90도)."""
    hull = convex_hull(poly)
    if len(hull) < 3:
        return 0.0
    best_area, best = math.inf, 0.0
    for i in range(len(hull)):
        (x1, y1), (x2, y2) = hull[i], hull[(i + 1) % len(hull)]
        angle = math.atan2(y2 - y1, x2 - x1)
        c, s = math.cos(-angle), math.sin(-angle)
        xs = [x * c - y * s for x, y in hull]
        ys = [x * s + y * c for x, y in hull]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if area < best_area - 1e-9:
            best_area, best = area, math.degrees(angle) % 90.0
    return round(best, 3)


# ── 지표 ─────────────────────────────────────────────────────────────
def metrics(
    piles: Sequence[dict[str, Any]],
    footprints: dict[str, list[Point]],
    zone_of: dict[str, str | None],
    pickup: Point,
    gap_m: float,
) -> dict[str, Any]:
    """mixedPairs, minMixedGapM, weightedDistanceM, footprintM2(더미들이 퍼져 있는 넓이: 자국 전체의 볼록 껍질)."""
    mixed, min_gap = 0, math.inf
    ids = [p["id"] for p in piles]
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            za, zb = zone_of.get(a), zone_of.get(b)
            if za is None or zb is None or za == zb:
                continue
            gap = polygon_distance(footprints[a], footprints[b])
            min_gap = min(min_gap, gap)
            if gap < gap_m - 1e-6:
                mixed += 1
    total = sum(float(p["volumeM3"]) for p in piles)
    weighted = (
        sum(float(p["volumeM3"]) * math.dist(pickup, _centroid(footprints[p["id"]])) for p in piles)
        / total
        if total > 0
        else 0.0
    )
    every = [v for fid in ids for v in footprints[fid]]
    return {
        "mixedPairs": mixed,
        "minMixedGapM": None if min_gap is math.inf else round(min_gap, 2),
        "weightedDistanceM": round(weighted, 2),
        "footprintM2": round(polygon_area(convex_hull(every)), 2) if every else 0.0,
    }


def _centroid(poly: Sequence[Point]) -> Point:
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


# ── 배치 ─────────────────────────────────────────────────────────────
class _Grid:
    """바닥을 주 방향으로 돌린 좌표(u, v)의 격자. 벽 여유만큼 깎은 칸을 쓸 수 있는 칸으로 둔다."""

    def __init__(self, floor: Sequence[Point], axis_deg: float, wall_m: float) -> None:
        self.c, self.s = math.cos(math.radians(axis_deg)), math.sin(math.radians(axis_deg))
        local = [self.to_local(p) for p in floor]
        self.floor_local = local
        us, vs = [p[0] for p in local], [p[1] for p in local]
        span = (max(us) - min(us)) * (max(vs) - min(vs))
        self.cell = max(GRID_M, math.sqrt(span / MAX_CANDIDATES)) if span > 0 else GRID_M
        self.u0, self.v0 = min(us), min(vs)
        self.nu = max(1, math.ceil((max(us) - self.u0) / self.cell))
        self.nv = max(1, math.ceil((max(vs) - self.v0) / self.cell))
        inside = [
            [contains(local, self.center(i, j)) for j in range(self.nv)] for i in range(self.nu)
        ]
        # 벽 여유: 바닥 밖 칸에서 wall_m 안의 칸을 지운다(4이웃 침식을 k번)
        for _ in range(math.ceil(wall_m / self.cell)):
            inside = [
                [
                    inside[i][j]
                    and i > 0
                    and j > 0
                    and i < self.nu - 1
                    and j < self.nv - 1
                    and inside[i - 1][j]
                    and inside[i + 1][j]
                    and inside[i][j - 1]
                    and inside[i][j + 1]
                    for j in range(self.nv)
                ]
                for i in range(self.nu)
            ]
        # 넓이 합 표: 사각형 안의 칸이 모두 쓸 수 있는지를 한 번에 본다
        self.sat = [[0] * (self.nv + 1) for _ in range(self.nu + 1)]
        for i in range(self.nu):
            row = 0
            for j in range(self.nv):
                row += 0 if inside[i][j] else 1
                self.sat[i + 1][j + 1] = self.sat[i][j + 1] + row

    def to_local(self, p: Point) -> Point:
        x, y = p
        return (x * self.c + y * self.s, -x * self.s + y * self.c)

    def to_world(self, p: Point) -> Point:
        u, v = p
        return (u * self.c - v * self.s, u * self.s + v * self.c)

    def center(self, i: int, j: int) -> Point:
        return (self.u0 + (i + 0.5) * self.cell, self.v0 + (j + 0.5) * self.cell)

    def free(self, u: float, v: float, hu: float, hv: float) -> bool:
        """(u, v)를 가운데로 반폭 hu·hv인 사각형이 쓸 수 있는 칸만 덮는가."""
        i0 = math.floor((u - hu - self.u0) / self.cell)
        i1 = math.ceil((u + hu - self.u0) / self.cell)
        j0 = math.floor((v - hv - self.v0) / self.cell)
        j1 = math.ceil((v + hv - self.v0) / self.cell)
        if i0 < 0 or j0 < 0 or i1 > self.nu or j1 > self.nv:
            return False
        blocked = self.sat[i1][j1] - self.sat[i0][j1] - self.sat[i1][j0] + self.sat[i0][j0]
        return blocked == 0


def _inside_with_margin(corners: Sequence[Point], poly: Sequence[Point], margin: float) -> bool:
    """사각형이 다각형 안에 있고 모든 변이 경계에서 margin 이상 떨어져 있는가(비스듬한 벽도 정확히 잰다)."""
    if not all(contains(poly, c) for c in corners):
        return False
    n = len(poly)
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        for j in range(n):
            c, d = poly[j], poly[(j + 1) % n]
            if _segments_cross(a, b, c, d):
                return False
            if (
                min(
                    _segment_distance(a, c, d),
                    _segment_distance(b, c, d),
                    _segment_distance(c, a, b),
                    _segment_distance(d, a, b),
                )
                < margin - 1e-9
            ):
                return False
    return True


def _box_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """주 방향 좌표의 두 축 정렬 사각형(u, v, hu, hv) 사이 거리."""
    du = max(0.0, abs(a[0] - b[0]) - a[2] - b[2])
    dv = max(0.0, abs(a[1] - b[1]) - a[3] - b[3])
    return math.hypot(du, dv)


def _local_box(grid: _Grid, footprint: Sequence[Point]) -> tuple[float, float, float, float]:
    """회전된 자국을 주 방향 좌표의 축 정렬 사각형으로 감싼다(장애물을 넉넉히 본다)."""
    local = [grid.to_local(p) for p in footprint]
    us, vs = [p[0] for p in local], [p[1] for p in local]
    return ((max(us) + min(us)) / 2, (max(vs) + min(vs)) / 2, (max(us) - min(us)) / 2,
            (max(vs) - min(vs)) / 2)  # fmt: skip


def propose(
    floor: Sequence[Point] | None,
    piles: Sequence[dict[str, Any]],
    zone_of: dict[str, str | None],
    zone_names: dict[str, str],
    pickup: Point,
    rules: dict[str, float] | None = None,
) -> dict[str, Any]:
    """현재 지표와 정리안. piles: [{"id", "footprint", "sizeM", "yawDeg", "volumeM3"}].

    돌려주는 값: {"current": 지표, "proposal": {...} | None, "unavailable": 까닭 | None}.
    """
    rules = {**DEFAULT_RULES, **(rules or {})}
    gap, wall, clear = rules["gapM"], rules["wallM"], rules["pickupClearM"]
    footprints = {p["id"]: [tuple(v) for v in p["footprint"]] for p in piles}
    current = metrics(piles, footprints, zone_of, pickup, gap)
    zones = sorted({z for z in zone_of.values() if z is not None})
    if not floor or len(floor) < 3:
        return {
            "current": current,
            "proposal": None,
            "unavailable": "바닥 경계를 찾지 못해 정리안을 그리지 못했습니다.",
        }
    if len(zones) < 2:
        return {
            "current": current,
            "proposal": None,
            "unavailable": "더미에 폐기물을 두 가지 이상 지정하면 정리안을 그립니다.",
        }
    axis = main_axis_deg(floor)
    grid = _Grid(floor, axis, wall)
    pick = grid.to_local(pickup)
    volume = {p["id"]: float(p["volumeM3"]) for p in piles}
    placed: list[tuple[str, str | None, tuple[float, float, float, float]]] = []
    # 지정하지 않은 더미는 제자리(장애물)
    for p in piles:
        if zone_of.get(p["id"]) is None:
            placed.append((p["id"], None, _local_box(grid, footprints[p["id"]])))
    order_zones = sorted(
        zones, key=lambda z: (-sum(volume[i] for i, zz in zone_of.items() if zz == z), z)
    )
    moved_boxes: dict[str, tuple[float, float, float, float, float]] = {}
    for zone in order_zones:
        members = sorted(
            (p for p in piles if zone_of.get(p["id"]) == zone),
            key=lambda p: (-volume[p["id"]], p["id"]),
        )
        anchor: Point | None = None
        for p in members:
            length, width = float(p["sizeM"][0]), float(p["sizeM"][1])
            # 비용(상차 거리 + 같은 폐기물 구역의 중심까지 거리)이 낮은 자리부터 보고 처음 되는 자리를 쓴다
            candidates = []
            for turn in (0.0, 90.0):
                hu, hv = (length / 2, width / 2) if turn == 0.0 else (width / 2, length / 2)
                for i in range(grid.nu):
                    for j in range(grid.nv):
                        u, v = grid.center(i, j)
                        cost = math.dist((u, v), pick)
                        if anchor is not None:
                            cost += 0.6 * math.dist((u, v), anchor)
                        candidates.append((round(cost, 9), turn, i, j, u, v, hu, hv))
            candidates.sort()
            best: tuple[float, float, float, float, float] | None = None
            for _, turn, _, _, u, v, hu, hv in candidates:
                if not grid.free(u, v, hu, hv):
                    continue
                corners = [(u - hu, v - hv), (u + hu, v - hv), (u + hu, v + hv), (u - hu, v + hv)]
                if not _inside_with_margin(corners, grid.floor_local, wall):
                    continue
                box = (u, v, hu, hv)
                if _box_gap(box, (pick[0], pick[1], 0.0, 0.0)) < clear:
                    continue
                if all(
                    _box_gap(box, other) >= (SAME_GAP_M if other_zone == zone else gap) - 1e-9
                    for _, other_zone, other in placed
                ):
                    best = (u, v, hu, hv, turn)
                    break
            if best is None:
                return {
                    "current": current,
                    "proposal": None,
                    "unavailable": "바닥이 좁아 모든 더미를 옮길 자리를 찾지 못했습니다. 지정하지 않은 더미를 줄이거나 상차 지점을 바꿔 보세요.",
                }
            u, v, hu, hv, turn = best
            placed.append((p["id"], zone, (u, v, hu, hv)))
            moved_boxes[p["id"]] = best
            mates = [(b[0], b[1]) for pid, b in moved_boxes.items() if zone_of.get(pid) == zone]
            anchor = (sum(m[0] for m in mates) / len(mates), sum(m[1] for m in mates) / len(mates))
    out_piles, new_footprints = [], dict(footprints)
    moved = 0
    for p in piles:
        pid = p["id"]
        if pid not in moved_boxes:
            still = {
                "id": pid,
                "center": list(_centroid(footprints[pid])),
                "footprint": [list(v) for v in footprints[pid]],
                "yawDeg": float(p.get("yawDeg") or 0.0),
                "zone": None,
            }
            out_piles.append(still)
            continue
        u, v, _, _, turn = moved_boxes[pid]
        center = grid.to_world((u, v))
        yaw = (axis + turn) % 180.0
        rect = rectangle(center, p["sizeM"], yaw)
        new_footprints[pid] = rect
        if math.dist(center, _centroid(footprints[pid])) > 0.3:
            moved += 1
        out_piles.append(
            {
                "id": pid,
                "center": [round(center[0], 3), round(center[1], 3)],
                "footprint": [[round(x, 3), round(y, 3)] for x, y in rect],
                "yawDeg": round(yaw, 2),
                "zone": zone_of[pid],
            }
        )
    zone_polys = []
    for zone in order_zones:
        pts = [v for pid in moved_boxes if zone_of.get(pid) == zone for v in new_footprints[pid]]
        hull = convex_hull(pts)
        zone_polys.append(
            {
                "streamId": zone,
                "name": zone_names.get(zone, zone),
                "polygon": [[round(x, 3), round(y, 3)] for x, y in hull],
            }
        )
    after = metrics(piles, new_footprints, zone_of, pickup, gap)
    return {
        "current": current,
        "proposal": {"piles": out_piles, "zones": zone_polys, "metrics": after, "moved": moved},
        "unavailable": None,
    }
