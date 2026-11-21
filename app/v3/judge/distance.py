"""도로 거리 제공자 둘: Azure Maps Route Matrix와 카카오모빌리티 자동차 길찾기.

Azure Maps (api-version 2025-01-01, 동기 요청). 로컬 미실행(실제 호출): 요청과 응답 모양은 공식 문서의
예시에 맞췄고 가짜 transport로만 확인했다.
https://learn.microsoft.com/rest/api/maps/route/post-route-matrix?view=rest-maps-2025-01-01

카카오모빌리티 자동차 길찾기 (GET /v1/directions). 실제 호출로 응답 모양을 확인했다(2026-11-18, 시연
좌표 두 지점). 읽기만 하는 거리 조회다: 좌표 두 개를 보내고 거리를 받는다. 조직 이름이나 물량은 보내지
않는다. 한 번에 한 쌍만 묻는 API라서 n개 지점의 행렬에는 n×(n-1)번 부른다(같은 쌍은 한 번만).
https://developers.kakaomobility.com/docs/navi-api/directions/

어느 것을 쓸지는 distance_provider_from_env가 환경 변수로 정한다: V3_DISTANCE=kakao|azure|none.
비어 있으면 AZURE_MAPS_KEY가 있을 때 Azure Maps, 없으면 제공자 없음(직선 거리 가정값)이다.

판단 도구는 distance_provider(points) -> km 행렬 | None 을 주입받는다. 여기서 None을 돌려주면
도구는 직선 거리에 우회 계수를 곱한 가정값으로 계산하고 결과에 "가정값"이라고 남긴다.

- points는 [경도, 위도] 목록이다(GeoJSON 순서).
- 키는 인자로만 받고, 머리글 subscription-key로 보낸다. 예외 문구와 기록에 넣지 않는다.
- 호출이 실패하거나 닿지 못하는 지점이 있으면 None을 돌려주고 last_error에 이유를 남긴다
  (strict=True면 DistanceError를 낸다). 일부만 도로 거리이고 나머지는 직선인 행렬은 만들지 않는다.
- 같은 지점 목록은 프로세스 안에서 한 번만 묻는다(같은 입력이면 같은 결과가 나오게 한다).
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from app.v3 import ids
from app.v3.evidence.model import PUBLIC, Evidence

logger = logging.getLogger(__name__)

# transport(method, url, headers, body) -> (status, body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]

ENDPOINT = "https://atlas.microsoft.com"
API_VERSION = "2025-01-01"
MAX_POINTS = 50  # 동기 요청의 행렬 크기 상한은 2500(출발 × 도착)이다
TRAVEL_MODES = ("driving", "truck")


class DistanceError(RuntimeError):
    """도로 거리를 받지 못했다."""


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    if not url.lower().startswith("https://"):
        raise DistanceError("https 주소만 호출한다")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DistanceError(
            f"도로 거리 서비스에 연결하지 못했다: {type(error).__name__}"
        ) from error


def _point(raw: Any) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise DistanceError("지점은 [경도, 위도]여야 한다")
    lon, lat = raw
    for value in (lon, lat):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise DistanceError("지점은 [경도, 위도]여야 한다")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise DistanceError("경도와 위도의 범위를 벗어났다")
    return float(lon), float(lat)


def matrix_request(points: Sequence[Sequence[float]], travel_mode: str = "truck") -> dict[str, Any]:
    """Route Matrix 요청 본문. 출발지와 도착지에 같은 지점 목록을 넣는다."""
    coordinates = [list(_point(p)) for p in points]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "MultiPoint", "coordinates": coordinates},
                "properties": {"pointType": kind},
            }
            for kind in ("origins", "destinations")
        ],
        "travelMode": travel_mode,
        "traffic": "historical",
    }


def parse_matrix(payload: Any, count: int) -> list[list[float]]:
    """응답의 properties.matrix를 count × count km 행렬로 바꾼다. 빠진 칸이 있으면 DistanceError."""
    properties = payload.get("properties") if isinstance(payload, dict) else None
    cells = properties.get("matrix") if isinstance(properties, dict) else None
    if not isinstance(cells, list):
        raise DistanceError("도로 행렬 응답의 형식이 다르다")
    km: list[list[float | None]] = [[None] * count for _ in range(count)]
    for cell in cells:
        if not isinstance(cell, dict):
            raise DistanceError("도로 행렬 칸의 형식이 다르다")
        i, j = cell.get("originIndex"), cell.get("destinationIndex")
        if not (isinstance(i, int) and isinstance(j, int) and 0 <= i < count and 0 <= j < count):
            raise DistanceError("도로 행렬 칸의 번호가 다르다")
        meters = cell.get("distanceInMeters")
        if (
            cell.get("statusCode") != 200
            or isinstance(meters, bool)
            or not isinstance(meters, (int, float))
            or not math.isfinite(meters)
            or meters < 0
        ):
            if i == j:
                continue  # 같은 지점끼리는 아래에서 0으로 둔다
            raise DistanceError("도로로 닿지 못하는 지점이 있다")
        if km[i][j] is not None:
            raise DistanceError("도로 행렬에 같은 칸이 두 번 있다")
        km[i][j] = float(meters) / 1000
    for i in range(count):
        km[i][i] = 0.0
    if any(value is None for row in km for value in row):
        raise DistanceError("도로 행렬에 빠진 칸이 있다")
    return [[float(value) for value in row] for row in km]  # type: ignore[arg-type]


class AzureMapsDistance:
    """distance_provider 구현. 인스턴스를 그대로 service.App(distance_provider=...)에 넣는다."""

    def __init__(
        self,
        key: str,
        *,
        transport: Transport | None = None,
        travel_mode: str = "truck",
        endpoint: str = ENDPOINT,
        strict: bool = False,
    ) -> None:
        if travel_mode not in TRAVEL_MODES:
            raise ValueError(f"travel_mode는 {', '.join(TRAVEL_MODES)} 가운데 하나다")
        self._key = key
        self._transport = transport or urllib_transport
        self.travel_mode = travel_mode
        self.endpoint = endpoint.rstrip("/")
        self.strict = strict
        self.last_error: str | None = None
        self.calls = 0
        self._cache: dict[tuple[tuple[float, float], ...], list[list[float]]] = {}

    def __repr__(self) -> str:
        return f"AzureMapsDistance(travel_mode={self.travel_mode!r})"

    @property
    def configured(self) -> bool:
        return bool(self._key)

    @property
    def url(self) -> str:
        return f"{self.endpoint}/route/matrix?api-version={API_VERSION}"

    def __call__(self, points: Sequence[Sequence[float]]) -> list[list[float]] | None:
        try:
            return self.matrix(points)
        except DistanceError as error:
            self.last_error = str(error)
            if self.strict:
                raise
            logger.warning("도로 거리를 받지 못해 거리를 보류한다: %s", error)
            return None

    def matrix(self, points: Sequence[Sequence[float]]) -> list[list[float]]:
        """points 순서 그대로의 km 행렬. 실패하면 DistanceError."""
        if not self._key:
            raise DistanceError("Azure Maps 키가 없다")
        wanted = [_point(p) for p in points]
        unique = list(dict.fromkeys(wanted))
        if not unique:
            return []
        if len(unique) > MAX_POINTS:
            raise DistanceError(f"도로 행렬은 고유 지점 {MAX_POINTS}개까지다")
        key = tuple(unique)
        if key not in self._cache:
            self._cache[key] = self._fetch(unique)
        km = self._cache[key]
        index = [unique.index(p) for p in wanted]
        return [[km[i][j] for j in index] for i in index]

    def _fetch(self, unique: list[tuple[float, float]]) -> list[list[float]]:
        if len(unique) == 1:
            return [[0.0]]
        body = json.dumps(matrix_request(unique, self.travel_mode)).encode("utf-8")
        headers = {"Content-Type": "application/geo+json", "subscription-key": self._key}
        self.calls += 1
        status, raw = self._transport("POST", self.url, headers, body)
        if status != 200:
            raise DistanceError(f"Azure Maps 호출 실패 (HTTP {status})")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DistanceError("Azure Maps 응답이 JSON이 아니다") from None
        return parse_matrix(payload, len(unique))


# ── 카카오모빌리티 자동차 길찾기 ─────────────────────────────────────
KAKAO_ENDPOINT = "https://apis-navi.kakaomobility.com"
KAKAO_MAX_POINTS = 8  # 고유 지점 8개면 56번 부른다. 그보다 많으면 묻지 않고 거리를 보류한다
KAKAO_PRIORITIES = ("RECOMMEND", "TIME", "DISTANCE")
KAKAO_SOURCE = "카카오모빌리티 자동차 길찾기 API (GET /v1/directions)"


def parse_kakao_route(payload: Any) -> float:
    """응답의 routes[0].summary.distance(m)를 km로 바꾼다. 길을 찾지 못했으면 DistanceError."""
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise DistanceError("길찾기 응답의 형식이 다르다")
    route = routes[0]
    if route.get("result_code") != 0:
        raise DistanceError("도로로 닿지 못하는 지점이 있다")
    summary = route.get("summary")
    meters = summary.get("distance") if isinstance(summary, dict) else None
    if (
        isinstance(meters, bool)
        or not isinstance(meters, (int, float))
        or not math.isfinite(meters)
        or meters < 0
    ):
        raise DistanceError("길찾기 응답에 거리가 없다")
    return float(meters) / 1000


class KakaoDistance:
    """distance_provider 구현. 인스턴스를 그대로 service.App(distance_provider=...)에 넣는다.

    - 키는 인자로만 받고 머리글 Authorization: KakaoAK <키>로 보낸다. 예외 문구와 기록에 넣지 않는다.
    - 호출이 실패하거나 닿지 못하는 지점이 있으면 None을 돌려주고 last_error에 이유를 남긴다. 판단
      도구는 그때 직선 거리 × 우회 계수(가정값)로 계산하고 결과에 "가정값"이라고 남긴다. 일부만 도로
      거리이고 나머지는 직선인 행렬은 만들지 않는다.
    - 같은 쌍은 프로세스 안에서 한 번만 묻는다(같은 입력이면 같은 결과가 나오게 한다).
    - evidence(points, as_of)는 물어서 받은 쌍마다 근거 레코드(ref)를 만든다. 가정값이 아니라 조회한
      값이고, 기준일은 조회한 날이다. 근거 저장소에 넣는 일은 판단 도구가 한다.
    """

    def __init__(
        self,
        key: str,
        *,
        transport: Transport | None = None,
        priority: str = "RECOMMEND",
        endpoint: str = KAKAO_ENDPOINT,
        strict: bool = False,
    ) -> None:
        if priority not in KAKAO_PRIORITIES:
            raise ValueError(f"priority는 {', '.join(KAKAO_PRIORITIES)} 가운데 하나다")
        self._key = key
        self._transport = transport or urllib_transport
        self.priority = priority
        self.endpoint = endpoint.rstrip("/")
        self.strict = strict
        self.last_error: str | None = None
        self.calls = 0
        self._pairs: dict[tuple[tuple[float, float], tuple[float, float]], float] = {}

    def __repr__(self) -> str:
        return f"KakaoDistance(priority={self.priority!r})"

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def url(self, origin: tuple[float, float], destination: tuple[float, float]) -> str:
        query = urlencode(
            {
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
                "priority": self.priority,
                "summary": "true",
            }
        )
        return f"{self.endpoint}/v1/directions?{query}"

    def __call__(self, points: Sequence[Sequence[float]]) -> list[list[float]] | None:
        try:
            return self.matrix(points)
        except DistanceError as error:
            self.last_error = str(error)
            if self.strict:
                raise
            logger.warning("도로 거리를 받지 못해 거리를 보류한다: %s", error)
            return None

    def matrix(self, points: Sequence[Sequence[float]]) -> list[list[float]]:
        """points 순서 그대로의 km 행렬. 실패하면 DistanceError."""
        if not self._key:
            raise DistanceError("카카오 REST API 키가 없다")
        wanted = [_point(p) for p in points]
        unique = list(dict.fromkeys(wanted))
        if len(unique) > KAKAO_MAX_POINTS:
            raise DistanceError(f"길찾기는 고유 지점 {KAKAO_MAX_POINTS}개까지 묻는다")
        found: dict[tuple[tuple[float, float], tuple[float, float]], float] = {}
        for a in unique:
            for b in unique:
                if a != b:
                    found[(a, b)] = self._pair(a, b)
        return [[0.0 if a == b else found[(a, b)] for b in wanted] for a in wanted]

    def _pair(self, origin: tuple[float, float], destination: tuple[float, float]) -> float:
        key = (origin, destination)
        if key not in self._pairs:
            headers = {"Authorization": f"KakaoAK {self._key}", "Content-Type": "application/json"}
            self.calls += 1
            try:
                status, raw = self._transport("GET", self.url(origin, destination), headers, None)
            except DistanceError:
                raise DistanceError("카카오 길찾기에 연결하지 못했다") from None
            if status != 200:
                raise DistanceError(f"카카오 길찾기 호출 실패 (HTTP {status})")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise DistanceError("카카오 길찾기 응답이 JSON이 아니다") from None
            self._pairs[key] = parse_kakao_route(payload)
        return self._pairs[key]

    def evidence(
        self,
        points: Sequence[Sequence[float]],
        as_of: str,
        access: Sequence[str] = (PUBLIC,),
    ) -> list[Evidence]:
        """points의 쌍 가운데 이미 물어서 받은 것의 근거 레코드. 출발·도착 순서대로, 같은 쌍은 한 번."""
        wanted = list(dict.fromkeys(_point(p) for p in points))
        records = []
        for a in wanted:
            for b in wanted:
                km = self._pairs.get((a, b))
                if a == b or km is None:
                    continue
                pair = {"origin": list(a), "destination": list(b), "priority": self.priority}
                records.append(
                    Evidence(
                        id=ids.ref_id("도로거리", f"카카오길찾기-{ids.digest(pair)}", as_of),
                        kind="ref",
                        label="도로 거리 (카카오모빌리티 길찾기)",
                        value=round(km, 3),
                        unit="km",
                        excerpt=(
                            f"출발 [{a[0]}, {a[1]}] → 도착 [{b[0]}, {b[1]}] (경도, 위도). "
                            f"자동차 길찾기 거리 {round(km * 1000)} m. 조회한 날의 추천 경로 기준이며 "
                            "교통 상황에 따라 달라질 수 있다."
                        ),
                        source=KAKAO_SOURCE,
                        as_of=as_of,
                        access=tuple(access),
                        meta={**pair, "provider": "kakao", "queriedOn": as_of},
                    )
                )
        return records


# ── 환경 변수로 고르기 ───────────────────────────────────────────────
DISTANCE_MODES = ("", "none", "azure", "kakao")


def distance_provider_from_env(env: Mapping[str, Any], *, strict: bool = False) -> Any:
    """V3_DISTANCE와 키로 distance_provider를 만든다. 없으면 None(직선 거리 가정값 경로).

    | V3_DISTANCE | 쓰는 것 | 필요한 값 |
    |---|---|---|
    | (비움) | AZURE_MAPS_KEY가 있으면 Azure Maps, 없으면 없음 | |
    | none | 없음 | |
    | azure | Azure Maps Route Matrix | AZURE_MAPS_KEY |
    | kakao | 카카오모빌리티 자동차 길찾기 | KAKAO_REST_API_KEY |

    고른 제공자의 키가 없으면: strict면 ValueError, 아니면 경고를 남기고 None이다. 키 값은 싣지 않는다.
    """
    mode = str(env.get("V3_DISTANCE") or "").strip().lower()
    if mode not in DISTANCE_MODES:
        raise ValueError(f"V3_DISTANCE는 kakao | azure | none 가운데 하나여야 한다: {mode!r}")
    azure = str(env.get("AZURE_MAPS_KEY") or "").strip()
    kakao = str(env.get("KAKAO_REST_API_KEY") or "").strip()
    if mode == "none":
        return None
    if mode == "kakao":
        if kakao:
            return KakaoDistance(kakao)
        missing = "KAKAO_REST_API_KEY"
    elif mode == "azure":
        if azure:
            return AzureMapsDistance(azure)
        missing = "AZURE_MAPS_KEY"
    else:
        return AzureMapsDistance(azure) if azure else None
    if strict:
        raise ValueError(f"V3_DISTANCE={mode}에는 환경 변수 {missing}가 필요합니다.")
    logger.warning("V3_DISTANCE=%s인데 %s가 비어 있어 직선 거리 가정값으로 돈다", mode, missing)
    return None
