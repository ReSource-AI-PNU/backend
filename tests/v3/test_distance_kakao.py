"""카카오모빌리티 길찾기 도로 거리 제공자: 요청 모양, 응답 해석, 실패했을 때의 동작(가짜 transport).

응답 모양은 2026-11-18의 실제 호출 한 번(시연 좌표 두 지점)에서 본 것과 같게 적었다. 키는 지어낸 글이다.
"""

import json
import logging
from urllib.parse import parse_qs, urlsplit

import pytest

from app.v3 import devserver, vendor_devserver
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.judge import distance
from app.v3.judge.defaults import register_defaults
from app.v3.judge.toolbox import call_tool
from tests.v3.judge_fixtures import TODAY, VENDOR, vendor

DEPOT, PLANT, YARD = [128.90, 35.10], [128.85, 35.09], [128.86, 35.10]
KEY = "kakao-test"


def route(meters, code=0):
    """실제 응답에서 본 모양: routes[0]에 result_code, result_msg, summary(distance, duration…)."""
    body = {"result_code": code, "result_msg": "길찾기 성공" if code == 0 else "길찾기 실패"}
    if code == 0:
        body["summary"] = {
            "origin": {},
            "destination": {},
            "waypoints": [],
            "priority": "RECOMMEND",
            "bound": {},
            "fare": {"taxi": 0, "toll": 0},
            "distance": meters,
            "duration": 600,
        }
        body["sections"] = []
    return {"trans_id": "t", "routes": [body]}


def fake(meters=lambda a, b: 1000, status=200, raw=None):
    """(transport, 보낸 요청 목록). meters(출발, 도착) -> m. 좌표는 (경도, 위도) 글이다."""
    sent = []

    def transport(method, url, headers, body):
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        sent.append({"method": method, "url": url, "headers": headers, "body": body, **query})
        if raw is not None:
            return status, raw
        payload = route(meters(query["origin"], query["destination"]))
        return status, json.dumps(payload).encode("utf-8")

    return transport, sent


def test_request_follows_the_documented_shape():
    transport, sent = fake(lambda a, b: 10793)
    provider = distance.KakaoDistance(KEY, transport=transport)
    assert provider([PLANT, DEPOT]) == [[0.0, 10.793], [10.793, 0.0]]
    first = sent[0]
    assert first["method"] == "GET" and first["body"] is None
    assert first["url"].startswith("https://apis-navi.kakaomobility.com/v1/directions?")
    assert first["headers"]["Authorization"] == f"KakaoAK {KEY}"
    assert (first["origin"], first["destination"]) == ("128.85,35.09", "128.9,35.1")  # 경도,위도
    assert first["priority"] == "RECOMMEND" and first["summary"] == "true"
    assert KEY not in first["url"] and KEY not in repr(provider) and provider.configured
    with pytest.raises(ValueError, match="priority"):
        distance.KakaoDistance(KEY, priority="FASTEST")


def test_each_pair_is_asked_once_in_both_directions():
    transport, sent = fake(lambda a, b: 2000 if a < b else 3000)
    provider = distance.KakaoDistance(KEY, transport=transport)
    first = provider([DEPOT, PLANT, YARD, PLANT])  # 같은 지점이 두 번 와도 한 번만 묻는다
    assert provider.calls == 6 and len(sent) == 6  # 고유 지점 3개 → 3 × 2쌍
    assert first[1][3] == 0.0 and first[1] == first[3]
    assert first[1][0] != first[0][1]  # 가는 길과 오는 길을 따로 묻는다
    assert provider([PLANT, DEPOT]) == [[0.0, first[1][0]], [first[0][1], 0.0]]
    assert provider.calls == 6  # 이미 받은 쌍은 다시 묻지 않는다
    assert provider([DEPOT]) == [[0.0]] and provider([]) == [] and provider.calls == 6


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"routes": []},
        {"routes": ["x"]},
        route(0, code=104),  # 길찾기 실패 코드(출발지와 도착지가 너무 가깝다 등)
        {"routes": [{"result_code": 0}]},
        {"routes": [{"result_code": 0, "summary": {"distance": "10km"}}]},
        {"routes": [{"result_code": 0, "summary": {"distance": -1}}]},
        {"routes": [{"result_code": 0, "summary": {"distance": True}}]},
    ],
)
def test_bad_responses_give_no_matrix(payload):
    transport, _ = fake(raw=json.dumps(payload).encode("utf-8"))
    provider = distance.KakaoDistance(KEY, transport=transport)
    assert provider([DEPOT, PLANT]) is None and provider.last_error
    with pytest.raises(distance.DistanceError):
        distance.parse_kakao_route(payload)


def test_http_errors_missing_key_and_too_many_points(caplog):
    for status, raw in ((401, b"{}"), (429, b"{}"), (200, b"not json")):
        transport, _ = fake(status=status, raw=raw)
        provider = distance.KakaoDistance(KEY, transport=transport)
        with caplog.at_level(logging.WARNING):
            assert provider([DEPOT, PLANT]) is None
        assert provider.last_error and KEY not in provider.last_error and KEY not in caplog.text
    assert "거리를 보류" in caplog.text

    def down(method, url, headers, body):
        raise distance.DistanceError(f"연결 실패 {headers}")  # 키가 든 문구를 내도 옮기지 않는다

    offline = distance.KakaoDistance(KEY, transport=down)
    assert offline([DEPOT, PLANT]) is None and KEY not in offline.last_error
    strict = distance.KakaoDistance(KEY, transport=down, strict=True)
    with pytest.raises(distance.DistanceError):
        strict([DEPOT, PLANT])
    transport, sent = fake()
    unset = distance.KakaoDistance("", transport=transport)
    assert unset([DEPOT, PLANT]) is None and sent == [] and not unset.configured
    many = [[128.0 + i / 100, 35.0] for i in range(distance.KAKAO_MAX_POINTS + 1)]
    crowded = distance.KakaoDistance(KEY, transport=transport)
    assert crowded(many) is None and sent == []  # 너무 많으면 묻지 않는다
    assert crowded([[200.0, 35.0], PLANT]) is None and sent == []  # 좌표 범위 밖


def test_partial_failure_never_mixes_road_and_straight_line():
    def transport(method, url, headers, body):
        query = parse_qs(urlsplit(url).query)
        blocked = query["destination"][0].startswith("128.86")
        return 200, json.dumps(route(1000, code=1 if blocked else 0)).encode("utf-8")

    provider = distance.KakaoDistance(KEY, transport=transport)
    assert provider([DEPOT, PLANT, YARD]) is None
    assert provider([DEPOT, PLANT]) is not None  # 닿는 쌍만 물으면 받는다


def test_evidence_is_a_looked_up_value_with_the_query_date():
    transport, _ = fake(lambda a, b: 10793)
    provider = distance.KakaoDistance(KEY, transport=transport)
    assert provider.evidence([PLANT, DEPOT], TODAY) == []  # 묻기 전에는 근거가 없다
    provider([PLANT, DEPOT])
    records = provider.evidence([PLANT, DEPOT, PLANT], TODAY, access=("org_hanbit", VENDOR))
    assert len(records) == 2  # 가는 길, 오는 길
    go = records[0]
    assert go.kind == "ref" and go.id.startswith("ref:도로거리:카카오길찾기-")
    assert go.id.endswith(f"@{TODAY}") and go.as_of == TODAY  # 기준일은 조회한 날
    assert (go.value, go.unit) == (10.793, "km") and "카카오모빌리티" in go.source
    assert go.meta["origin"] == PLANT and go.meta["destination"] == DEPOT
    assert go.meta["provider"] == "kakao" and go.access == ("org_hanbit", VENDOR)
    assert "10793 m" in go.excerpt and KEY not in json.dumps(go.to_dict(), ensure_ascii=False)
    assert records[1].id != go.id and records[1].meta["origin"] == DEPOT
    # 같은 쌍과 같은 날이면 같은 ID다
    assert [r.id for r in provider.evidence([PLANT, DEPOT], TODAY)] == [r.id for r in records]
    registry = InMemoryRegistry()
    assert registry.put(go).id == go.id and registry.get(go.id, VENDOR).value == 10.793


def test_judge_tool_uses_kakao_distance_and_holds_without_it():
    registry = InMemoryRegistry()
    register_defaults(registry, TODAY)
    profile = vendor()
    profile["fleet"] = {**profile["fleet"], "depot": DEPOT}
    arguments = {
        "vendor": profile,
        "stops": [{"id": "a", "location": YARD, "kg": 300, "m3": 1}],
        "newStop": {"id": "b", "location": PLANT, "kg": 300, "m3": 1},
    }

    def run(provider):
        return call_tool(
            "route_extra_cost",
            arguments,
            registry=registry,
            org_id=VENDOR,
            today=TODAY,
            distance_provider=provider,
        )

    transport, _ = fake(lambda a, b: 4000)
    road = run(distance.KakaoDistance(KEY, transport=transport))
    assert road["output"]["distanceAssumed"] is False
    broken, _ = fake(status=500, raw=b"{}")
    straight = run(distance.KakaoDistance(KEY, transport=broken))
    # 실패하면 직선 × 우회 계수(가정값)로 채우지 않고 거리를 보류한다
    assert straight["status"] == "needs_information" and straight["claims"] == []
    assert straight["output"]["distanceAssumed"] is None
    assert any(c["key"] == "distance" for c in straight["checks"])


def test_provider_is_chosen_by_env():
    choose = distance.distance_provider_from_env
    assert choose({}) is None and choose({"V3_DISTANCE": "none", "AZURE_MAPS_KEY": "k"}) is None
    assert isinstance(choose({"AZURE_MAPS_KEY": "k"}), distance.AzureMapsDistance)
    both = {"AZURE_MAPS_KEY": "k", "KAKAO_REST_API_KEY": KEY}
    assert isinstance(choose(both), distance.AzureMapsDistance)  # 비어 있으면 이전과 같다
    assert isinstance(choose({**both, "V3_DISTANCE": " Kakao "}), distance.KakaoDistance)
    assert isinstance(choose({**both, "V3_DISTANCE": "azure"}), distance.AzureMapsDistance)
    assert choose({"V3_DISTANCE": "kakao"}) is None  # 키가 없으면 가정값 경로(경고)
    with pytest.raises(ValueError, match="KAKAO_REST_API_KEY"):
        choose({"V3_DISTANCE": "kakao"}, strict=True)
    with pytest.raises(ValueError, match="AZURE_MAPS_KEY"):
        choose({"V3_DISTANCE": "azure"}, strict=True)
    with pytest.raises(ValueError, match="V3_DISTANCE"):
        choose({"V3_DISTANCE": "tmap"})


def test_both_local_servers_read_the_same_setting(tmp_path):
    env = {"V3_DISTANCE": "kakao", "KAKAO_REST_API_KEY": KEY}
    app = devserver.build_app(today=TODAY, env=env)
    assert isinstance(app.distance_provider, distance.KakaoDistance)
    assert devserver.build_app(today=TODAY, env={}).distance_provider is None
    agent_app = vendor_devserver.build_vendor_app(str(tmp_path / "s.json"), today=TODAY, env=env)
    assert isinstance(agent_app.distance_provider, distance.KakaoDistance)
    for build in (
        lambda: devserver.build_app(today=TODAY, env={"V3_DISTANCE": "kakao"}),
        lambda: vendor_devserver.build_vendor_app(
            str(tmp_path / "s.json"), today=TODAY, env={"V3_DISTANCE": "kakao"}
        ),
    ):
        with pytest.raises(SystemExit, match="KAKAO_REST_API_KEY"):
            build()
