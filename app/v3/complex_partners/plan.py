"""같은 단지 자원순환 후보의 2D 평면도 자료. 화면을 열 때마다 실시간으로 만든다.

- 회사 위치: 허가 명부에 있는 곳은 명부 좌표(지자체 공공데이터)를 쓰고, 등록공장에만 있는 곳은 주소를 카카오 주소
  검색(없거나 실패하면 VWorld 지오코더)으로 바꾼다. 배출 기업은 조직의 위치를 쓰고, 없으면 주소를 바꾼다.
- 건물: VWorld 2D 데이터 API의 도로명주소 건물(LT_C_SPBD) 도형을 회사들이 든 범위에서 받는다. 회사 좌표가 든
  건물(없으면 가까운 건물)을 그 회사의 건물로 본다.
- 두 서비스 모두 결과를 실시간으로 쓰고 저장하지 말라고 정해 두었다. 그래서 이 모듈은 받은 좌표·도형을 저장소나
  파일에 쓰지 않고, 한 요청 안에서만 쓴다(같은 주소를 두 번 묻지 않는 정도만 한다).
- 키가 없거나 호출이 실패하면 그 부분만 빼고 이유를 notes에 적는다(평면도를 지어내지 않는다).
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.v3 import public_vendors

# transport(url, headers) -> (status, body)
Transport = Callable[[str, dict[str, str]], tuple[int, bytes]]

KAKAO_ADDRESS = "https://dapi.kakao.com/v2/local/search/address.json"
VWORLD_ADDRESS = "https://api.vworld.kr/req/address"
VWORLD_DATA = "https://api.vworld.kr/req/data"
BUILDING_LAYER = "LT_C_SPBD"  # 도로명주소 건물
PAGE_SIZE = 1000  # VWorld 데이터 API 한 쪽 최대
MAX_PAGES = 3
MARGIN_M = 120.0  # 회사들이 든 범위 둘레에 더하는 여백
MIN_SPAN_M = 500.0  # 회사가 한두 곳이면 이 폭은 보이게 한다
NEAR_M = 40.0  # 좌표가 건물 밖이면 이 거리 안의 가장 가까운 건물을 그 회사 건물로 본다
WORKERS = 6
SOURCES = {
    "registry": "공공데이터 허가 명부 좌표",
    "kakao": "카카오 주소 검색",
    "vworld": "VWorld 지오코더",
    "org": "회사 정보의 위치",
}
NO_KEY_BUILDINGS = "건물 도형을 받을 열쇠가 없어 건물 없이 위치만 그렸습니다."
FAILED_BUILDINGS = "건물 도형을 받지 못해 건물 없이 위치만 그렸습니다."
NO_KEY_LOCATIONS = "주소를 위치로 바꿀 열쇠가 없어, 허가 명부에 좌표가 있는 곳만 그렸습니다."


class PlanError(RuntimeError):
    """지도 서비스에 닿지 못했다. 문장에 열쇠를 넣지 않는다."""


def urllib_get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise PlanError("https 주소만 호출한다")
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise PlanError(f"지도 서비스에 연결하지 못했다: {type(error).__name__}") from error


def _clean_address(address: str) -> str:
    """ "부산광역시 강서구 녹산산단381로12번길 13-25 (송정동)" -> 괄호의 법정동을 뗀다."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(address or "")).strip()


class Geo:
    """주소 → 좌표, 범위 → 건물 도형. 열쇠가 없는 서비스는 부르지 않는다."""

    def __init__(
        self,
        *,
        kakao_key: str = "",
        vworld_key: str = "",
        vworld_domain: str = "",
        transport: Transport = urllib_get,
    ) -> None:
        self.kakao_key = kakao_key.strip()
        self.vworld_key = vworld_key.strip()
        self.vworld_domain = vworld_domain.strip()
        self.transport = transport

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Geo:
        return cls(
            kakao_key=str(env.get("KAKAO_REST_API_KEY") or ""),
            vworld_key=str(env.get("VWORLD_API_KEY") or ""),
            vworld_domain=str(env.get("VWORLD_DOMAIN") or ""),
        )

    @property
    def can_locate(self) -> bool:
        return bool(self.kakao_key or self.vworld_key)

    @property
    def can_draw(self) -> bool:
        return bool(self.vworld_key)

    def _json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        status, body = self.transport(url, headers or {})
        if status != 200:
            raise PlanError(f"지도 서비스가 {status}로 답했다")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PlanError("지도 서비스의 답을 읽지 못했다") from error

    def _kakao(self, address: str) -> tuple[float, float] | None:
        data = self._json(
            KAKAO_ADDRESS + "?" + urllib.parse.urlencode({"query": address}),
            {"Authorization": f"KakaoAK {self.kakao_key}"},
        )
        for doc in data.get("documents") or []:
            road = doc.get("road_address") or {}
            x, y = road.get("x") or doc.get("x"), road.get("y") or doc.get("y")
            if x and y:
                return float(x), float(y)
        return None

    def _vworld_point(self, address: str) -> tuple[float, float] | None:
        for kind in ("road", "parcel"):
            params = {
                "service": "address",
                "request": "getcoord",
                "version": "2.0",
                "crs": "epsg:4326",
                "type": kind,
                "address": address,
                "format": "json",
                "key": self.vworld_key,
            }
            if self.vworld_domain:
                params["domain"] = self.vworld_domain
            data = self._json(VWORLD_ADDRESS + "?" + urllib.parse.urlencode(params))
            response = data.get("response") or {}
            point = (response.get("result") or {}).get("point") or {}
            if response.get("status") == "OK" and point.get("x") and point.get("y"):
                return float(point["x"]), float(point["y"])
        return None

    def locate(self, address: str) -> tuple[float, float, str] | None:
        """(경도, 위도, 출처). 찾지 못하면 None. 한 서비스가 실패하면 다른 서비스를 묻는다."""
        query = _clean_address(address)
        if not query:
            return None
        for name, ready, ask in (
            ("kakao", self.kakao_key, self._kakao),
            ("vworld", self.vworld_key, self._vworld_point),
        ):
            if not ready:
                continue
            try:
                found = ask(query)
            except PlanError:
                continue
            if found:
                return found[0], found[1], name
        return None

    def buildings(self, bbox: list[float]) -> list[dict[str, Any]]:
        """범위 안 도로명주소 건물. [{"id", "rings", "floors", "address"}]. 실패하면 PlanError."""
        out: list[dict[str, Any]] = []
        x0, y0, x1, y1 = bbox
        for page in range(1, MAX_PAGES + 1):
            params = {
                "service": "data",
                "request": "GetFeature",
                "data": BUILDING_LAYER,
                "geomFilter": f"BOX({x0:.6f},{y0:.6f},{x1:.6f},{y1:.6f})",
                "size": str(PAGE_SIZE),
                "page": str(page),
                "format": "json",
                "crs": "EPSG:4326",
                "geometry": "true",
                "attribute": "true",
                "key": self.vworld_key,
            }
            if self.vworld_domain:
                params["domain"] = self.vworld_domain
            data = self._json(VWORLD_DATA + "?" + urllib.parse.urlencode(params))
            response = data.get("response") or {}
            if response.get("status") == "NOT_FOUND":
                break
            if response.get("status") != "OK":
                raise PlanError("건물 도형을 받지 못했다")
            features = ((response.get("result") or {}).get("featureCollection") or {}).get(
                "features"
            ) or []
            for feature in features:
                building = _building(feature)
                if building:
                    out.append(building)
            total = int((response.get("page") or {}).get("total") or 1)
            if page >= total:
                break
        return out


def _building(feature: dict[str, Any]) -> dict[str, Any] | None:
    geometry = feature.get("geometry") or {}
    props = feature.get("properties") or {}
    polygons = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polygons = [polygons]
    rings = [
        [[round(float(x), 6), round(float(y), 6)] for x, y, *_ in polygon[0]]
        for polygon in polygons
        if polygon and len(polygon[0]) >= 3
    ]
    if not rings:
        return None
    road, number = str(props.get("rd_nm") or ""), str(props.get("buld_no") or "")
    floors = props.get("gro_flo_co")
    return {
        "id": str(props.get("bd_mgt_sn") or feature.get("id") or ""),
        "rings": rings,
        "floors": int(floors) if str(floors or "").isdigit() else None,
        "address": f"{road} {number}".strip() or None,
        "name": str(props.get("buld_nm") or "").strip() or None,
    }


def _meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    dx = (a[0] - b[0]) * 111_320 * math.cos(lat)
    dy = (a[1] - b[1]) * 110_540
    return math.hypot(dx, dy)


def _inside(point: tuple[float, float], ring: list[list[float]]) -> bool:
    x, y = point
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            hit = not hit
        j = i
    return hit


def building_of(point: tuple[float, float], buildings: list[dict[str, Any]]) -> str | None:
    """좌표가 든 건물, 없으면 NEAR_M 안의 가장 가까운 건물(꼭짓점 평균 기준)."""
    for building in buildings:
        if any(_inside(point, ring) for ring in building["rings"]):
            return building["id"]
    best, best_m = None, NEAR_M
    for building in buildings:
        ring = building["rings"][0]
        center = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
        meters = _meters(point, center)
        if meters < best_m:
            best, best_m = building["id"], meters
    return best


def view_box(points: list[tuple[float, float]]) -> list[float]:
    """점들을 덮고 여백을 둔 범위 [서, 남, 동, 북]. 폭이 MIN_SPAN_M보다 좁으면 넓힌다."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    lat = sum(ys) / len(ys)
    per_x, per_y = 111_320 * math.cos(math.radians(lat)), 110_540
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half_x = max((max(xs) - min(xs)) / 2 + MARGIN_M / per_x, MIN_SPAN_M / 2 / per_x)
    half_y = max((max(ys) - min(ys)) / 2 + MARGIN_M / per_y, MIN_SPAN_M / 2 / per_y)
    return [
        round(cx - half_x, 6),
        round(cy - half_y, 6),
        round(cx + half_x, 6),
        round(cy + half_y, 6),
    ]


def _registry_point(vendor_id: str | None) -> tuple[float, float] | None:
    vendor = public_vendors.get(vendor_id) if vendor_id else None
    if vendor and vendor.get("lon") and vendor.get("lat"):
        return float(vendor["lon"]), float(vendor["lat"])
    return None


SCALE = 100_000  # 건물 꼭짓점은 범위의 서남쪽 모서리에서 1e-5도(약 1m) 단위 정수로 보낸다


def _compact(
    buildings: list[dict[str, Any]], bbox: list[float], owned: set[str]
) -> list[dict[str, Any]]:
    """건물 도형을 작게: 꼭짓점은 [x, y, x, y, ...] 정수(범위 서남쪽 모서리 기준, SCALE 단위)로 펴고,
    회사가 든 건물만 ID·주소·이름을 남긴다(나머지는 바탕 건물이라 층수만)."""
    x0, y0 = bbox[0], bbox[1]
    out = []
    for building in buildings:
        item: dict[str, Any] = {
            "rings": [
                [v for x, y in ring for v in (round((x - x0) * SCALE), round((y - y0) * SCALE))]
                for ring in building["rings"]
            ],
            "floors": building["floors"],
        }
        if building["id"] in owned:
            item.update(id=building["id"], address=building["address"], name=building["name"])
        out.append(item)
    return out


def build(partners: dict[str, Any], emitter: dict[str, Any], geo: Geo) -> dict[str, Any]:
    """결과 한 장의 complexPartners와 배출 기업으로 평면도 자료를 만든다.

    {"complex", "bbox", "scale", "self", "places": {후보 ID: 자리}, "buildings", "missing", "notes", "sources"}.
    자리는 {"location": [경도, 위도], "buildingId", "source"}다. 건물 꼭짓점은 _compact의 정수 꼴이다.
    """
    candidates: dict[str, dict[str, Any]] = {}
    for entry in partners.get("streams") or []:
        for item in [*(entry.get("takes") or []), *(entry.get("uses") or [])]:
            candidates.setdefault(item["id"], item)
    notes: dict[str, str | None] = {"buildings": None, "locations": None}
    places: dict[str, dict[str, Any]] = {}
    ask: dict[str, list[str]] = {}  # 주소 -> 그 주소를 쓰는 후보 ID
    for cid, item in candidates.items():
        vendor_id = (item.get("permit") or {}).get("vendorId") or (
            cid if cid.startswith("pv_") else None
        )
        point = _registry_point(vendor_id)
        if point:
            places[cid] = {"location": list(point), "buildingId": None, "source": "registry"}
        elif item.get("address"):
            ask.setdefault(_clean_address(item["address"]), []).append(cid)
    self_place: dict[str, Any] | None = None
    if emitter.get("location"):
        lon, lat = emitter["location"]
        self_place = {"location": [float(lon), float(lat)], "buildingId": None, "source": "org"}
    elif emitter.get("address"):
        ask.setdefault(_clean_address(emitter["address"]), []).append("__self__")
    if ask and not geo.can_locate:
        notes["locations"] = NO_KEY_LOCATIONS
    elif ask:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            found = dict(zip(ask, pool.map(geo.locate, list(ask)), strict=True))
        for address, ids in ask.items():
            hit = found.get(address)
            if not hit:
                continue
            for cid in ids:
                place = {"location": [hit[0], hit[1]], "buildingId": None, "source": hit[2]}
                if cid == "__self__":
                    self_place = place
                else:
                    places[cid] = place
    points = [tuple(p["location"]) for p in places.values()]
    if self_place:
        points.append(tuple(self_place["location"]))
    bbox = view_box(points) if points else None
    buildings: list[dict[str, Any]] = []
    if bbox and not geo.can_draw:
        notes["buildings"] = NO_KEY_BUILDINGS
    elif bbox:
        try:
            buildings = geo.buildings(bbox)
        except PlanError:
            notes["buildings"] = FAILED_BUILDINGS
    for place in [*places.values(), *([self_place] if self_place else [])]:
        place["buildingId"] = building_of(tuple(place["location"]), buildings)
    used = sorted({place["source"] for place in places.values()})
    owned = {p["buildingId"] for p in [*places.values(), self_place] if p and p["buildingId"]}
    return {
        "complex": partners.get("complex"),
        "bbox": bbox,
        "scale": SCALE,
        "self": self_place,
        "places": places,
        "buildings": _compact(buildings, bbox, owned) if bbox else [],
        "missing": sorted(cid for cid in candidates if cid not in places),
        "notes": notes,
        "sources": [SOURCES[s] for s in used]
        + (["VWorld 도로명주소 건물(화면을 열 때 조회하고 저장하지 않음)"] if buildings else []),
    }
