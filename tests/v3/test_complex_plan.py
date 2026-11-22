"""같은 단지 평면도(complex_partners/plan.py, GET /sessions/{id}/complex-plan). 지도 서비스는 가짜 전송으로 대신한다."""

import json
import urllib.parse

import pytest

from app.v3 import service
from app.v3.complex_partners import plan
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, make_app, start

SQUARE = 0.0002  # 가짜 건물 한 변(약 20m)


def _square(lon, lat):
    h = SQUARE / 2
    return [
        [
            [lon - h, lat - h],
            [lon + h, lat - h],
            [lon + h, lat + h],
            [lon - h, lat + h],
            [lon - h, lat - h],
        ]
    ]


class FakeMaps:
    """카카오는 주소마다 정해진 좌표를, VWorld 건물 층은 알려 준 점마다 정사각형 건물을 돌려준다."""

    def __init__(self, *, kakao_fail=False, vworld_fail=False):
        self.calls = []
        self.kakao_fail = kakao_fail
        self.vworld_fail = vworld_fail
        self.points = {}

    def point_for(self, address):
        if address not in self.points:
            n = len(self.points)
            self.points[address] = (128.84 + 0.001 * n, 35.09)
        return self.points[address]

    def __call__(self, url, headers):
        self.calls.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if url.startswith(plan.KAKAO_ADDRESS):
            assert headers["Authorization"] == "KakaoAK kakao-key"
            if self.kakao_fail:
                return 401, b'{"msg":"ip mismatched"}'
            x, y = self.point_for(query["query"])
            doc = {"x": str(x), "y": str(y), "road_address": {"x": str(x), "y": str(y)}}
            return 200, json.dumps({"documents": [doc]}).encode()
        if url.startswith(plan.VWORLD_ADDRESS):
            x, y = self.point_for(query["address"])
            body = {"response": {"status": "OK", "result": {"point": {"x": str(x), "y": str(y)}}}}
            return 200, json.dumps(body).encode()
        if url.startswith(plan.VWORLD_DATA):
            assert query["domain"] == "https://example.test" and query["key"] == "vworld-key"
            if self.vworld_fail:
                return 200, b'{"response":{"status":"ERROR"}}'
            features = [
                {
                    "geometry": {"type": "MultiPolygon", "coordinates": [_square(x, y)]},
                    "properties": {
                        "bd_mgt_sn": f"b{i}",
                        "rd_nm": "녹산산업중로",
                        "buld_no": str(i),
                        "gro_flo_co": "2",
                    },
                }
                for i, (x, y) in enumerate([*self.points.values(), *self.extra])
            ]
            body = {
                "response": {
                    "status": "OK",
                    "page": {"total": "1", "current": "1"},
                    "result": {"featureCollection": {"features": features}},
                }
            }
            return 200, json.dumps(body).encode()
        raise AssertionError(url)

    extra = ((128.86007, 35.09237),)  # 동양피앤씨 허가 명부 좌표 근처 건물


def _geo(maps, **keys):
    defaults = {
        "kakao_key": "kakao-key",
        "vworld_key": "vworld-key",
        "vworld_domain": "https://example.test",
    }
    return plan.Geo(**{**defaults, **keys}, transport=maps)


@pytest.fixture(scope="module")
def waiting():
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    return app, view


def _ids(view):
    partners = view["result"]["complexPartners"]
    return {c["id"] for s in partners["streams"] for c in [*s["takes"], *s["uses"]]}


def test_plan_places_every_partner_and_draws_buildings(waiting):
    app, view = waiting
    maps = FakeMaps()
    app.geo = _geo(maps)
    try:
        out = service.complex_plan(app, view["session"]["id"], HANBIT)
    finally:
        del app.geo
    assert out["complex"]["id"] == "noksan" and out["missing"] == []
    assert set(out["places"]) == _ids(view)
    sources = {p["source"] for p in out["places"].values()}
    assert sources == {"registry", "kakao"}  # 허가 명부 좌표가 있는 곳은 주소를 묻지 않는다
    assert out["self"]["source"] == "org"
    assert out["notes"] == {"buildings": None, "locations": None}
    # 등록공장에만 있는 곳은 모두 자기 건물(가짜 정사각형) 안에 있다
    for place in out["places"].values():
        if place["source"] == "kakao":
            assert place["buildingId"] is not None
    # 건물 꼭짓점은 범위 서남쪽 모서리 기준 정수이고, 회사 건물만 ID가 있다
    owned = {p["buildingId"] for p in out["places"].values() if p["buildingId"]}
    for building in out["buildings"]:
        assert all(isinstance(v, int) and v >= -1 for ring in building["rings"] for v in ring)
        assert ("id" in building) == (building.get("id") in owned)
    assert out["scale"] == plan.SCALE
    # 같은 주소는 한 번만 묻는다
    asked = [u for u in maps.calls if u.startswith(plan.KAKAO_ADDRESS)]
    assert len(asked) == len(set(asked))


def test_plan_without_keys_says_why_and_does_not_invent(waiting):
    app, view = waiting
    maps = FakeMaps()
    app.geo = plan.Geo(transport=maps)
    try:
        out = service.complex_plan(app, view["session"]["id"], HANBIT)
    finally:
        del app.geo
    assert maps.calls == []
    assert out["buildings"] == []
    assert out["notes"]["buildings"] == plan.NO_KEY_BUILDINGS
    assert out["notes"]["locations"] == plan.NO_KEY_LOCATIONS
    assert {p["source"] for p in out["places"].values()} == {"registry"}
    assert out["missing"] and set(out["missing"]) | set(out["places"]) == _ids(view)


def test_plan_falls_back_to_vworld_and_survives_building_failure(waiting):
    app, view = waiting
    app.geo = _geo(FakeMaps(kakao_fail=True, vworld_fail=True))
    try:
        out = service.complex_plan(app, view["session"]["id"], HANBIT)
    finally:
        del app.geo
    assert "vworld" in {p["source"] for p in out["places"].values()}
    assert out["missing"] == []
    assert out["buildings"] == [] and out["notes"]["buildings"] == plan.FAILED_BUILDINGS


def test_plan_is_for_the_emitter_only(waiting):
    app, view = waiting
    app.geo = plan.Geo(transport=FakeMaps())
    try:
        with pytest.raises(service.ServiceError) as err:
            service.complex_plan(app, view["session"]["id"], SAEGIL)
    finally:
        del app.geo
    assert err.value.code in ("FORBIDDEN", "SESSION_NOT_FOUND")


def test_building_of_uses_containing_then_nearest():
    buildings = [
        {"id": "a", "rings": _square(128.0, 35.0)},
        {"id": "b", "rings": _square(128.001, 35.0)},
    ]
    assert plan.building_of((128.0, 35.0), buildings) == "a"
    assert plan.building_of((128.00125, 35.0), buildings) == "b"  # 건물 밖이지만 약 23m
    assert plan.building_of((128.01, 35.0), buildings) is None


def test_view_box_has_a_minimum_span_and_errors_hide_keys():
    x0, _, x1, _ = plan.view_box([(128.86, 35.09)])
    assert (x1 - x0) * 111_320 * 0.818 >= plan.MIN_SPAN_M - 1
    geo = _geo(lambda url, headers: (500, b"secret"))
    with pytest.raises(plan.PlanError) as err:
        geo.buildings([128.0, 35.0, 128.01, 35.01])
    assert "vworld-key" not in str(err.value) and "kakao-key" not in str(err.value)
