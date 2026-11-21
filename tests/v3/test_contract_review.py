"""계약 검토 담당(세 번째 에이전트)과 수거 가능 지역(2026-11-19 멘토링).

- 업체가 수거 가능 지역과 최대 수거 거리를 밝히고, 결과 한 장이 그것을 공장 지역·거리와 견준다.
- 계약 검토 담당이 합의 조건을 승인 전에 다시 대조하고, 막힘이 있으면 양쪽 모두 승인할 수 없다(안전장치).
- 양쪽 승인 뒤 위탁 계약서 초안을 만들고, 처음 거래하는 두 회사면 시범 기간 조항을 넣는다.
"""

from __future__ import annotations

import pytest

from app.v3 import fleet, service
from app.v3.agents import reviewer
from app.v3.agents.vendor import public_profile
from tests.v3.runtime_fixtures import HANBIT, MISO, SAEGIL, approve_all, make_app, to_approval

REGION = "부산 강서구 녹산산단"


# ── 업체가 밝히는 수거 가능 지역 ─────────────────────────────────────


def test_fleet_takes_service_areas_and_max_distance():
    out = fleet.clean_patch(
        {"serviceAreas": [" 부산 강서구 ", "경남 김해시", "부산 강서구"], "maxPickupKm": 40}
    )
    assert out == {"serviceAreas": ["부산 강서구", "경남 김해시"], "maxPickupKm": 40}
    assert fleet.clean_patch({"serviceAreas": None, "maxPickupKm": None}) == {
        "serviceAreas": None,
        "maxPickupKm": None,
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"serviceAreas": "부산"},
        {"serviceAreas": [""]},
        {"serviceAreas": ["가" * 51]},
        {"serviceAreas": ["부산"] * 31},
        {"maxPickupKm": 0},
        {"maxPickupKm": 1001},
        {"maxPickupKm": "40"},
    ],
)
def test_fleet_rejects_bad_service_areas(patch):
    with pytest.raises(fleet.FleetError):
        fleet.clean_patch(patch)


def test_service_area_is_not_given_to_judge_tools():
    profile = {"fleet": {"capacityKg": 1000, "serviceAreas": ["부산"], "maxPickupKm": 30}}
    assert public_profile(profile)["fleet"] == {"capacityKg": 1000}


@pytest.mark.parametrize(
    ("area", "inside"),
    [
        ("부산", True),
        ("부산광역시", True),
        ("부산 강서구", True),
        ("부산광역시 강서구", True),
        ("강서구", True),
        ("녹산국가산업단지", True),
        ("서울 강서구", False),
        ("부산 사하구", False),
        ("경남 김해시", False),
        ("신호일반산업단지", False),
        ("", False),
    ],
)
def test_in_area(area, inside):
    assert reviewer.in_area(area, REGION) is inside


def test_area_check_statuses():
    km = {
        "id": "st_a_transportKm",
        "kind": "number",
        "value": 20.0,
        "unit": "km",
        "evidence": ["calc:x"],
    }
    assert reviewer.area_check({}, REGION) is None
    assert reviewer.area_check({"serviceAreas": ["부산 강서구"]}, REGION)["status"] == "met"
    outside = reviewer.area_check({"serviceAreas": ["경남 양산시"]}, REGION)
    assert outside["status"] == "unmet" and "경남 양산시" in outside["text"]
    near = reviewer.area_check({"maxPickupKm": 30}, REGION, km)
    assert (
        near["status"] == "met"
        and near["claims"] == [km]
        and "{{st_a_transportKm}}" in near["text"]
    )
    far = reviewer.area_check({"maxPickupKm": 10}, REGION, km)
    assert far["status"] == "unmet"
    unknown = reviewer.area_check({"maxPickupKm": 10}, REGION, None)
    assert unknown["status"] == "conditional"
    # 지역 목록 밖이어도 거리 안이면 수거하러 온다
    either = reviewer.area_check({"serviceAreas": ["경남 양산시"], "maxPickupKm": 30}, REGION, km)
    assert either["status"] == "met"


def test_result_checks_service_area_and_vendor_screen_saves_it():
    app = make_app()
    saved = service.update_vendor(
        app, SAEGIL, {"fleet": {"serviceAreas": ["부산 강서구"], "maxPickupKm": 35}}, SAEGIL
    )
    assert saved["fleet"]["serviceAreas"] == ["부산 강서구"] and saved["fleet"]["maxPickupKm"] == 35
    # 노선이 없는 미소환경이 수거 가능 지역을 밝히면 노선 밖이어도 "업체 수거 가능 확인"이 된다(수거 상태 한 줄)
    service.update_vendor(app, MISO, {"fleet": {"serviceAreas": ["부산 강서구"]}}, MISO)
    view = to_approval(app)
    rows = [a for a in view["result"]["assignments"] if a["vendorOrgId"] == SAEGIL]
    assert rows
    for row in rows:
        route = [c for c in row["checks"] if c["key"] == "pickup_route"]
        assert route and route[0]["status"] == "met"
        assert not [c for c in row["checks"] if c["key"] == "pickup_area"]  # 한 줄로 합쳤다
    miso = [a for a in view["result"]["assignments"] if a["vendorOrgId"] == MISO]
    for row in miso:
        assert row["pickup"]["status"] == "visit" and row["transportPending"] is False
        route = next(c for c in row["checks"] if c["key"] == "pickup_route")
        assert route["status"] == "met" and "수거 가능 지역(부산 강서구) 안" in route["text"]


# ── 계약 검토 담당 ──────────────────────────────────────────────────


def test_every_agreement_gets_a_review_both_sides_see():
    app = make_app()
    service.update_vendor(app, MISO, {"fleet": {"serviceAreas": ["부산"]}}, MISO)
    view = to_approval(app)
    assert view["approvals"]
    for approval in view["approvals"]:
        review = approval["review"]
        assert review["reviewer"]["label"] == "계약 검토 담당"
        keys = [item["key"] for item in review["items"]]
        # 처음 거래하는 두 회사라 지난 검수 기록(history)이 없어 그 항목은 두지 않는다(test_memory.py)
        assert keys == [k for k in reviewer.TITLES if k != "history"]
        assert review["status"] in ("needs_check", "clear")
        assert not [i for i in review["items"] if i["status"] == "block"]
        for item in review["items"]:
            assert "{{" not in item["text"]
    # 처음 거래하는 두 회사라 시범 기간을 넣는다
    pilot = next(i for i in view["approvals"][0]["review"]["items"] if i["key"] == "pilot")
    assert pilot["pilot"] is True
    # 업체도 같은 검토를 본다
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == MISO)
    vendor_view = service.get_view(app, view["session"]["id"], MISO)
    mine = next(a for a in vendor_view["approvals"] if a["threadId"] == thread["id"])
    theirs = next(a for a in view["approvals"] if a["threadId"] == thread["id"])
    assert mine["review"] == theirs["review"]


def test_review_speaks_in_the_live_feed():
    app = make_app()
    service.update_vendor(app, MISO, {"fleet": {"serviceAreas": ["부산"]}}, MISO)
    view = to_approval(app)
    sid = view["session"]["id"]
    entries = [e for e in app.store.list_activity(sid) if e.get("kind") == "review"]
    assert len(entries) == len(view["approvals"])
    assert all(e["actor"] == reviewer.ACTOR and e["shared"] for e in entries)
    shown = [e["data"] for e in service.stream_events(app, sid, MISO)["events"]]
    reviews = [e for e in shown if e.get("kind") == "review"]
    assert reviews and reviews[0]["author"]["label"] == "계약 검토 담당"
    assert reviews[0]["author"]["side"] == "reviewer"
    assert {i["key"] for i in reviews[0]["review"]["items"]} == set(reviewer.TITLES) - {"history"}
    # 다시 계산해도 같은 검토는 다시 남기지 않는다
    service.get_view(app, sid, HANBIT)
    again = [e for e in app.store.list_activity(sid) if e.get("kind") == "review"]
    assert len(again) == len(entries)


def test_vendor_outside_its_service_area_cannot_be_approved():
    # 시드 그대로: 미소환경은 노선 밖 방문 수거 답이 없어 수거 가능 지역만으로 수거 여부가 정해진다
    app = make_app(miso_visits=False)
    service.update_vendor(app, MISO, {"fleet": {"serviceAreas": ["부산 강서구"]}}, MISO)
    view = to_approval(app)
    sid = view["session"]["id"]
    thread = next(
        t for t in view["threads"] if t["vendorOrgId"] == MISO and t["status"] == "agreed"
    )
    # 합의한 뒤에 업체가 "양산까지만 수거한다"고 밝혔다
    service.update_vendor(app, MISO, {"fleet": {"serviceAreas": ["경남 양산시"]}}, MISO)
    fresh = service.get_view(app, sid, HANBIT)
    review = next(a for a in fresh["approvals"] if a["threadId"] == thread["id"])["review"]
    assert review["status"] == "blocked"
    pickup = next(i for i in review["items"] if i["key"] == "pickup")
    assert pickup["status"] == "block" and "경남 양산시" in pickup["text"]
    # 수거 가능 지역이 바뀐 것은 합의 뒤 수거·운반 조건 변경이라 승인이 먼저 막힌다(PICKUP_TERMS_CHANGED).
    # 계약 검토도 같은 판단으로 막는다(위 review)
    for org in (HANBIT, MISO):
        with pytest.raises(service.ServiceError) as caught:
            service.approve(app, sid, thread["id"], org)
        assert caught.value.code == "APPROVAL_BLOCKED"
        assert "수거 가능 지역" in caught.value.message
    # 이 사업장에 적용되는 기존 수거·운반 조건이 복원되면 검토도 다시 통과한다.
    # 지역명 문자열 자체가 아닌 이 거래의 실효 조건을 비교한다.
    service.update_vendor(app, MISO, {"fleet": {"serviceAreas": ["부산"]}}, MISO)
    fresh = service.get_view(app, sid, HANBIT)
    again = next(a for a in fresh["approvals"] if a["threadId"] == thread["id"])
    assert not again["blocked"] and again["review"]["status"] != "blocked"
    service.approve(app, sid, thread["id"], HANBIT)


def test_price_outside_vendor_range_blocks():
    offers = {"st_a": {"unitPriceKrwPerKg": 300.0, "days": ["토"]}}
    rows = [{"streamId": "st_a", "checks": [], "transportPaidBy": "vendor"}]
    found = reviewer.review(
        rows=rows,
        offers=offers,
        guardrails={"floorPriceKrwPerKg": 100, "ceilingPriceKrwPerKg": 250, "days": ["화"]},
        regulation=[],
        approval=None,
        earlier_deals=1,
        stream_names={"st_a": "PP 러너"},
    )
    item = next(i for i in found["items"] if i["key"] == "terms_range")
    assert item["status"] == "block"
    assert "PP 러너의 합의 단가" in item["text"] and "PP 러너의 수거 요일" in item["text"]
    # 업체가 감춘 하한·상한 값은 문장에 없다
    assert "250" not in item["text"] and "100" not in item["text"]
    assert found["status"] == "blocked"
    pilot = next(i for i in found["items"] if i["key"] == "pilot")
    assert pilot["pilot"] is False


def test_contract_draft_after_both_approvals():
    app = make_app()
    view = approve_all(app, to_approval(app))
    drafts = [d for d in view["documents"] if d["kind"] == "contract"]
    assert len(drafts) == len(view["approvals"])
    for draft in drafts:
        assert draft["validation"]["ok"]
        text = draft["rendered"]
        assert draft["title"].startswith("위탁 계약서 초안")
        for head in ("1. 대상 폐기물과 물량", "2. 단가와 정산", "3. 수거와 운반", "시범 기간"):
            assert head in text
        assert "계약 검토 담당의 검토" in text
        assert "법무 검토" in text and "관청 제출 서류가 아닙니다" in text
