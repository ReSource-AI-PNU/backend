"""Azure Maps 도로 거리 제공자: 요청 본문, 응답 해석, 실패했을 때의 동작(가짜 transport)."""

import json

import pytest

from app.v3.evidence.registry import InMemoryRegistry
from app.v3.judge import distance
from app.v3.judge.defaults import register_defaults
from app.v3.judge.toolbox import call_tool
from tests.v3.judge_fixtures import TODAY, VENDOR, vendor

DEPOT, PLANT, YARD = [128.90, 35.10], [128.85, 35.09], [128.86, 35.10]


def cell(i, j, meters, status=200):
    """공식 문서의 응답 예시와 같은 칸."""
    return {
        "originIndex": i,
        "destinationIndex": j,
        "distanceInMeters": meters,
        "durationInSeconds": 600,
        "durationTrafficInSeconds": 600,
        "statusCode": status,
    }


def response(cells):
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "matrix": cells,
            "summary": {"successfulCount": len(cells), "totalCount": len(cells)},
        },
    }


def full(count, meters=lambda i, j: 1000 * (abs(i - j) + (i > j))):
    return response([cell(i, j, meters(i, j)) for i in range(count) for j in range(count)])


class FakeMaps:
    def __init__(self, payload=None, status=200):
        self.payload, self.status, self.requests = payload, status, []

    def __call__(self, method, url, headers, body):
        request = json.loads(body.decode("utf-8"))
        self.requests.append((method, url, headers, request))
        payload = self.payload
        if payload is None:
            payload = full(len(request["features"][0]["geometry"]["coordinates"]))
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return self.status, raw


def test_request_follows_the_documented_shape():
    fake = FakeMaps()
    provider = distance.AzureMapsDistance("test-key", transport=fake)
    km = provider([DEPOT, PLANT, YARD])
    method, url, headers, body = fake.requests[0]
    assert method == "POST"
    assert url == "https://atlas.microsoft.com/route/matrix?api-version=2025-01-01"
    assert headers == {"Content-Type": "application/geo+json", "subscription-key": "test-key"}
    assert body["type"] == "FeatureCollection" and body["travelMode"] == "truck"
    kinds = [f["properties"]["pointType"] for f in body["features"]]
    assert kinds == ["origins", "destinations"]
    for feature in body["features"]:
        assert feature["type"] == "Feature"
        assert feature["geometry"] == {"type": "MultiPoint", "coordinates": [DEPOT, PLANT, YARD]}
    assert km == [[0.0, 1.0, 2.0], [2.0, 0.0, 1.0], [3.0, 2.0, 0.0]]  # 미터를 km로
    assert "test-key" not in repr(provider) and provider.configured


def test_duplicate_points_are_asked_once_and_results_are_cached():
    fake = FakeMaps()
    provider = distance.AzureMapsDistance("test-key", transport=fake)
    km = provider([DEPOT, PLANT, DEPOT])
    assert len(fake.requests[0][3]["features"][0]["geometry"]["coordinates"]) == 2
    assert km == [[0.0, 1.0, 0.0], [2.0, 0.0, 2.0], [0.0, 1.0, 0.0]]
    assert provider([DEPOT, PLANT, DEPOT]) == km and provider.calls == 1
    assert provider([DEPOT]) == [[0.0]] and provider([]) == [] and provider.calls == 1


def test_documented_sample_response_is_parsed():
    """공식 문서의 예시 응답(출발 2 × 도착 2)."""
    sample = response(
        [cell(0, 0, 492466), cell(0, 1, 877028), cell(1, 0, 427769), cell(1, 1, 836080)]
    )
    km = distance.parse_matrix(sample, 2)
    assert km == [[0.0, 877.028], [427.769, 0.0]]  # 같은 지점끼리는 0으로 둔다


@pytest.mark.parametrize(
    "payload",
    [
        response([cell(0, 1, 1000)]),  # 빠진 칸
        response([cell(0, 1, 1000), cell(1, 0, 1000, status=400)]),  # 닿지 못함
        response([cell(0, 1, 1000), cell(0, 1, 1000), cell(1, 0, 1000)]),  # 같은 칸 두 번
        response([cell(0, 5, 1000), cell(1, 0, 1000)]),  # 번호가 범위 밖
        response([cell(0, 1, -5), cell(1, 0, 1000)]),
        response([cell(0, 1, "1000"), cell(1, 0, 1000)]),
        {"properties": {}},
        {"error": {"code": "BadRequest"}},
        [],
    ],
)
def test_bad_responses_give_no_matrix(payload):
    with pytest.raises(distance.DistanceError):
        distance.parse_matrix(payload, 2)
    provider = distance.AzureMapsDistance("test-key", transport=FakeMaps(payload))
    assert provider([DEPOT, PLANT]) is None and provider.last_error
    strict = distance.AzureMapsDistance("test-key", transport=FakeMaps(payload), strict=True)
    with pytest.raises(distance.DistanceError):
        strict([DEPOT, PLANT])


def test_http_errors_and_bad_input():
    denied = distance.AzureMapsDistance("test-key", transport=FakeMaps({"error": {}}, status=401))
    assert denied([DEPOT, PLANT]) is None and "401" in denied.last_error
    assert "test-key" not in denied.last_error
    not_json = distance.AzureMapsDistance("test-key", transport=FakeMaps(b"<html>"))
    assert not_json([DEPOT, PLANT]) is None
    no_key = distance.AzureMapsDistance("", transport=FakeMaps())
    assert no_key([DEPOT, PLANT]) is None and not no_key.configured
    provider = distance.AzureMapsDistance("test-key", transport=FakeMaps())
    for bad in ([[128.9]], [["a", "b"]], [[True, 1]], [[200, 35]], [[128.9, 95]]):
        assert provider(bad) is None
    assert provider.calls == 0
    many = [[128 + i / 1000, 35.0] for i in range(distance.MAX_POINTS + 1)]
    assert provider(many) is None and provider.calls == 0
    with pytest.raises(ValueError, match="travel_mode"):
        distance.AzureMapsDistance("test-key", travel_mode="walking")
    with pytest.raises(distance.DistanceError, match="https"):
        distance.urllib_transport("POST", "http://atlas.microsoft.com/route/matrix", {}, b"{}")


def test_judge_tool_uses_road_distance_and_falls_back_to_assumption():
    registry = InMemoryRegistry()
    register_defaults(registry, TODAY)
    v = vendor()
    stops = [{"location": YARD, "kg": 1000, "m3": 4}]
    new = {"location": PLANT, "kg": 600, "m3": 3}
    args = {"vendor": v, "stops": stops, "newStop": new}

    def run(provider):
        return call_tool(
            "route_extra_cost",
            args,
            registry=registry,
            org_id=VENDOR,
            today=TODAY,
            distance_provider=provider,
        )

    road = run(distance.AzureMapsDistance("test-key", transport=FakeMaps()))
    assert road["output"]["distanceAssumed"] is False
    assert not [c for c in road["checks"] if c["key"] == "distance"]
    failed = run(distance.AzureMapsDistance("test-key", transport=FakeMaps(status=500)))
    assert failed["output"]["distanceAssumed"] is True  # 실패하면 가정값이라고 밝힌다
    assert [c for c in failed["checks"] if c["key"] == "distance"]
    assert failed["output"] == run(None)["output"]
