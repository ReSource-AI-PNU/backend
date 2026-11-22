"""같은 단지 평면도(complex_partners/plan.py, GET /sessions/{id}/complex-plan). 지도 서비스는 가짜 전송으로 대신한다."""

import json
import urllib.parse

import pytest

from app.v3 import service
from app.v3.complex_partners import plan
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, make_app, start


class FakeMaps:
    """카카오와 VWorld 지오코더는 주소마다 정해진 좌표를 돌려준다(건물 도형은 브라우저가 받으므로 부르지 않는다)."""

    def __init__(self, *, kakao_fail=False):
        self.calls = []
        self.kakao_fail = kakao_fail
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
        raise AssertionError(url)


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


def test_plan_places_every_partner_and_hands_buildings_to_the_browser(waiting):
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
    # 건물은 서버가 받지 않는다(VWorld가 해외 IP를 끊는다). 브라우저가 부를 값만 준다
    assert out["buildings"] == [] and not any(u.startswith(plan.VWORLD_DATA) for u in maps.calls)
    layer = out["buildingLayer"]
    assert layer["url"] == plan.VWORLD_DATA and layer["layer"] == "LT_C_SPBD"
    assert layer["key"] == "vworld-key" and layer["domain"] == "https://example.test"
    assert all(p["buildingId"] is None for p in out["places"].values())
    assert plan.BROWSER_BUILDINGS in out["sources"]
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
    assert out["buildings"] == [] and out["buildingLayer"] is None
    assert out["notes"]["buildings"] == plan.NO_KEY_BUILDINGS
    assert out["notes"]["locations"] == plan.NO_KEY_LOCATIONS
    assert {p["source"] for p in out["places"].values()} == {"registry"}
    assert out["missing"] and set(out["missing"]) | set(out["places"]) == _ids(view)


def test_plan_falls_back_to_vworld_geocoder(waiting):
    app, view = waiting
    app.geo = _geo(FakeMaps(kakao_fail=True))
    try:
        out = service.complex_plan(app, view["session"]["id"], HANBIT)
    finally:
        del app.geo
    assert "vworld" in {p["source"] for p in out["places"].values()}
    assert out["missing"] == []


def test_plan_is_for_the_emitter_only(waiting):
    app, view = waiting
    app.geo = plan.Geo(transport=FakeMaps())
    try:
        with pytest.raises(service.ServiceError) as err:
            service.complex_plan(app, view["session"]["id"], SAEGIL)
    finally:
        del app.geo
    assert err.value.code in ("FORBIDDEN", "SESSION_NOT_FOUND")


def test_view_box_has_a_minimum_span_and_errors_hide_keys():
    x0, _, x1, _ = plan.view_box([(128.86, 35.09)])
    assert (x1 - x0) * 111_320 * 0.818 >= plan.MIN_SPAN_M - 1
    geo = _geo(lambda url, headers: (500, b"secret"))
    with pytest.raises(plan.PlanError) as err:
        geo._json("https://dapi.kakao.com/x", {"Authorization": "KakaoAK kakao-key"})
    assert "vworld-key" not in str(err.value) and "kakao-key" not in str(err.value)
    assert geo.locate("부산광역시 강서구 녹산산업중로 1") is None  # 둘 다 실패하면 위치 없음
