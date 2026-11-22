"""같은 단지 자원순환 후보의 2D 평면도 자료. 화면을 열 때마다 실시간으로 만든다.

- 회사 위치: 허가 명부에 있는 곳은 명부 좌표(지자체 공공데이터)를 쓰고, 등록공장에만 있는 곳은 주소를 카카오 주소
  검색(없거나 실패하면 VWorld 지오코더)으로 바꾼다. 배출 기업은 조직의 위치를 쓰고, 없으면 주소를 바꾼다.
- 건물: VWorld 2D 데이터 API의 도로명주소 건물(LT_C_SPBD) 도형은 서버가 받지 않고 사용자의 브라우저가 받는다.
  VWorld는 해외 IP(운영 VM은 미국 Central US)의 요청을 응답 없이 끊는다(2026-11-22 운영 컨테이너에서 확인). 그래서
  이 모듈은 브라우저가 부를 값(buildingLayer: 주소, 층 이름, 열쇠, 발급 도메인, 범위)만 돌려준다. VWorld 열쇠는
  발급 때 적은 서비스 주소에 묶여 있고 웹 페이지에서 쓰라고 내주는 열쇠라, 배출 기업 담당자의 브라우저에 보여도 된다.
  회사 좌표가 든 건물은 브라우저가 찾는다(frontend src/lib/v3/complex-plan.ts).
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
WORKERS = 6
SCALE = 100_000  # 예전 화면이 읽던 칸(건물 꼭짓점 단위). 건물을 보내지 않으므로 뜻은 없다
SOURCES = {
    "registry": "공공데이터 허가 명부 좌표",
    "kakao": "카카오 주소 검색",
    "vworld": "VWorld 지오코더",
    "org": "회사 정보의 위치",
}
NO_KEY_BUILDINGS = "건물 도형을 받을 열쇠가 없어 건물 없이 위치만 그렸습니다."
BROWSER_BUILDINGS = "VWorld 도로명주소 건물(화면을 열 때 브라우저가 조회하고 저장하지 않음)"
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


def build(partners: dict[str, Any], emitter: dict[str, Any], geo: Geo) -> dict[str, Any]:
    """결과 한 장의 complexPartners와 배출 기업으로 평면도 자료를 만든다.

    {"complex", "bbox", "scale", "self", "places": {후보 ID: 자리}, "buildings": [], "buildingLayer", "missing",
    "notes", "sources"}. 자리는 {"location": [경도, 위도], "buildingId": None, "source"}다. 건물과 회사 건물은
    브라우저가 buildingLayer로 받아 정한다.
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
    layer = None
    if bbox and geo.can_draw:
        layer = {
            "provider": "vworld",
            "url": VWORLD_DATA,
            "layer": BUILDING_LAYER,
            "key": geo.vworld_key,
            "domain": geo.vworld_domain or None,
            "pageSize": PAGE_SIZE,
            "maxPages": MAX_PAGES,
        }
    elif bbox:
        notes["buildings"] = NO_KEY_BUILDINGS
    used = sorted({place["source"] for place in places.values()})
    return {
        "complex": partners.get("complex"),
        "bbox": bbox,
        # 예전 화면(서버가 건물을 보내던 판)과 맞추는 칸: 건물은 이제 브라우저가 받으므로 늘 비어 있다
        "scale": SCALE,
        "self": self_place,
        "places": places,
        "buildings": [],
        "buildingLayer": layer,
        "missing": sorted(cid for cid in candidates if cid not in places),
        "notes": notes,
        "sources": [SOURCES[s] for s in used] + ([BROWSER_BUILDINGS] if layer else []),
    }
