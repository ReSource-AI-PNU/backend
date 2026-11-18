"""업체 노선·차량(PUT /vendors/{orgId}의 fleet) 입력 검사. 값은 모두 가상이다."""

import pytest

from app.v3 import fleet, service
from app.v3.agents.vendor import public_profile
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app

ROUTES = [
    {
        "name": "강서 화목 노선",
        "days": ["목", "화", "화"],
        "regions": ["부산 강서구"],
        "complexes": ["녹산국가산업단지", "녹산국가산업단지"],
    },
    {"name": "사하 월 노선", "days": ["월"], "regions": ["부산 사하구"]},
]


def test_routes_and_vehicles_are_kept_and_cleaned():
    app = make_app()
    current = service.get_vendor(app, SAEGIL, SAEGIL)["fleet"]
    # GET으로 받은 값(evidence 포함)을 그대로 돌려보내도 된다
    saved = service.update_vendor(
        app, SAEGIL, {"fleet": {**current, "vehicleCount": 3, "routes": ROUTES}}, SAEGIL
    )["fleet"]
    assert saved["vehicleCount"] == 3 and saved["capacityKg"] == current["capacityKg"]
    assert saved["routes"][0] == {
        "name": "강서 화목 노선",
        "days": ["화", "목"],
        "regions": ["부산 강서구"],
        "complexes": ["녹산국가산업단지"],
    }
    assert saved["routes"][1]["complexes"] == []
    assert saved["evidence"] == current["evidence"]  # 값이 같으면 근거도 그대로다
    # 판단 도구 입력에는 차량 수와 노선이 없다(계산 근거 ID가 바뀌지 않는다)
    profile = app.store.get_vendor_profile(SAEGIL)
    for shown in (public_profile(profile), public_profile(profile, fleet_cost=False)):
        assert "routes" not in shown["fleet"] and "vehicleCount" not in shown["fleet"]
    assert public_profile(profile)["fleet"]["capacityKg"] == current["capacityKg"]
    # null은 그 항목을 지우고, fleet: null은 아무것도 바꾸지 않는다
    cleared = service.update_vendor(app, SAEGIL, {"fleet": {"vehicleCount": None}}, SAEGIL)
    assert "vehicleCount" not in cleared["fleet"] and cleared["fleet"]["routes"]
    same = service.update_vendor(app, SAEGIL, {"fleet": None}, SAEGIL)["fleet"]
    assert same == cleared["fleet"]
    # 업체 자신만 고친다
    with pytest.raises(service.ServiceError):
        service.update_vendor(app, SAEGIL, {"fleet": {"vehicleCount": 2}}, HANBIT)


@pytest.mark.parametrize(
    "bad",
    [
        {"capacityKg": 0},
        {"capacityKg": "3000"},
        {"capacityKg": 100_001},
        {"capacityM3": True},
        {"costPerKm": -1},
        {"depot": [128.9]},
        {"depot": [128.9, 95]},
        {"depot": ["128.9", 35.1]},
        {"vehicleCount": 0},
        {"vehicleCount": 1.5},
        {"capacity_kg": 3000},
        {"routes": [{"name": "노선", "days": ["월"]}]},
        {"routes": [{"name": "노선", "days": [], "regions": ["부산"]}]},
        {"routes": [{"name": "노선", "days": ["월요일"], "regions": ["부산"]}]},
        {"routes": [{"name": " ", "days": ["월"], "regions": ["부산"]}]},
        {"routes": [{"name": "노선", "days": ["월"], "regions": [""]}]},
        {"routes": [{"name": "노선", "days": ["월"], "regions": ["부산"], "stops": []}]},
        {"routes": [{"name": "같음", "days": ["월"], "regions": ["가"]}] * 2},
        {"routes": "강서"},
        "강서",
    ],
)
def test_wrong_fleet_values_are_refused_without_changing_the_profile(bad):
    app = make_app()
    before = app.store.get_vendor_profile(SAEGIL)
    with pytest.raises(service.ServiceError) as caught:
        service.update_vendor(app, SAEGIL, {"fleet": bad}, SAEGIL)
    assert (caught.value.code, caught.value.status) == ("INVALID_VALUE", 422)
    assert caught.value.message and not caught.value.message.startswith("'")
    assert app.store.get_vendor_profile(SAEGIL) == before


def test_clean_patch_keeps_numbers_as_given():
    cleaned = fleet.clean_patch({"capacityKg": 3000, "costPerKm": 0, "depot": [129, 35]})
    assert cleaned == {"capacityKg": 3000, "costPerKm": 0, "depot": [129.0, 35.0]}
    assert isinstance(cleaned["capacityKg"], int)
    assert fleet.clean_patch({"evidence": ["doc:x"]}) == {}
