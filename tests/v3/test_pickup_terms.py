"""방문 수거 상태와 운반비 조건(judge/pickup.py)이 배정·결과 한 장·승인까지 이어지는지. 회사와 수치는 모두 가상이다."""

import copy

import pytest

from app.v3 import service
from app.v3.evidence.model import Evidence
from app.v3.judge import pickup
from tests.v3.judge_fixtures import EMITTER, VENDOR, claim, registry, run, stream, vendor
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    SAEGIL,
    approve_all,
    make_app,
    to_approval,
)

FEE_DOC = f"doc:{VENDOR}:반입조건표:노선밖운반비"
COUNT_DOC = f"doc:{VENDOR}:반입조건표:노선밖횟수"


def defect(**over):
    """불량 사출품 400kg, 현행 혼합 소각 kg당 250원(가상)."""
    base = stream(
        id="st_defect",
        name="불량 사출품",
        monthlyKg=400,
        currentTreatment={"method": "혼합 소각", "unitCostKrwPerKg": 250, "evidence": []},
    )
    base.update(over)
    return base


def buyer(transport, price=220, **over):
    """kg당 price원에 사 가는 업체. transport는 이 공장에 적용되는 운반 조건(도구 입력)이다."""
    return vendor(
        criteria=[
            {
                "id": "pc_pp",
                "material": "PP",
                "unitPriceKrwPerKg": price,
                "minMonthlyKg": 0,
                "evidence": [],
            }
        ],
        transport=transport,
        **over,
    )


def with_quote_docs(reg):
    reg.put(
        Evidence(
            id=FEE_DOC,
            kind="doc",
            value=30000,
            unit="원/회",
            excerpt="노선 밖 방문 수거는 회당 30,000원을 따로 받는다",
            source="가상 반입 조건표",
            as_of="2026-11-10",
            access=(VENDOR, EMITTER),
        )
    )
    reg.put(
        Evidence(
            id=COUNT_DOC,
            kind="doc",
            value=2,
            unit="회/월",
            excerpt="노선 밖 방문 수거는 월 2회",
            source="가상 반입 조건표",
            as_of="2026-11-10",
            access=(VENDOR, EMITTER),
        )
    )
    return reg


def label_of(reg, found):
    """calc 근거의 이름(화면의 근거 패널 제목)."""
    return reg.get(found["evidence"][0], EMITTER).label


STATES = {
    "unknown": {"status": "unknown", "basis": "undecided", "evidence": []},
    "check": {"status": "check", "basis": "undecided", "evidence": []},
    "visit": {
        "status": "visit",
        "basis": "separate",
        "paidBy": "emitter",
        "feePerPickupKrw": 30000,
        "pickupsPerMonth": 2,
        "evidence": [FEE_DOC, COUNT_DOC],
        "feeEvidence": [FEE_DOC],
        "pickupsEvidence": [COUNT_DOC],
    },
    "unavailable": {"status": "unavailable", "basis": "undecided", "evidence": []},
}


# ── 이 공장에 적용되는 조건 ─────────────────────────────────────────────
def test_effective_terms_split_pickup_status_from_fee():
    routes = [{"name": "사상", "days": ["월", "수"], "regions": ["부산 사상구"], "complexes": []}]
    included = {"paidBy": "vendor", "evidence": ["doc:x"]}
    on_route = pickup.effective(included, routes, "부산 사상구 학장동")
    assert on_route["status"] == "route" and on_route["basis"] == "included"
    assert on_route["days"] == ["월", "수"]
    assert pickup.monthly(on_route) == 0
    # 노선 밖인데 노선 밖 조건이 없으면 노선 수거의 "단가에 포함"을 그대로 쓰지 않는다
    outside = pickup.effective(included, routes, "부산 강서구 녹산산단")
    assert outside["status"] == "check" and outside["basis"] == "undecided"
    assert outside["routeBasis"] == "included" and pickup.monthly(outside) is None
    visit = pickup.effective(
        {
            **included,
            "offRoute": {
                "status": "visit",
                "paidBy": "emitter",
                "feePerPickupKrw": 30000,
                "pickupsPerMonth": 2,
            },
        },
        routes,
        "부산 강서구 녹산산단",
    )
    assert visit["status"] == "visit" and visit["basis"] == "separate"
    assert pickup.monthly(visit) == 60000
    no_count = pickup.effective(
        {
            **included,
            "offRoute": {"status": "visit", "basis": "separate", "feePerPickupKrw": 30000},
        },
        routes,
        "부산 강서구",
    )
    assert pickup.monthly(no_count) is None  # 횟수가 없으면 0원으로 정하지 않는다
    assert pickup.monthly(no_count, pickups=2) == 60000
    monthly = pickup.effective(
        {**included, "offRoute": {"status": "visit", "basis": "separate", "feePerMonthKrw": 70000}},
        routes,
        "부산 강서구",
    )
    assert pickup.monthly(monthly) == 70000  # 월 금액은 횟수를 곱하지 않는다
    gone = pickup.effective(
        {**included, "offRoute": {"status": "unavailable"}}, routes, "부산 강서구"
    )
    assert gone["status"] == "unavailable" and pickup.monthly(gone) is None
    unknown = pickup.effective(None, None, "부산 강서구")
    assert unknown["status"] == "unknown" and unknown["basis"] == "undecided"


# ── 네 상태를 같은 폐기물로 비교 ─────────────────────────────────────────
def test_four_pickup_states_on_the_same_waste():
    out, reg_of = {}, {}
    for name, transport in STATES.items():
        reg = reg_of[name] = with_quote_docs(registry())
        done = run(reg, "optimize_assignment", streams=[defect()], vendors=[buyer(transport)])
        out[name] = (done, done["output"]["assignments"][0])

    for name in ("unknown", "check"):
        done, item = out[name]
        assert item["route"] == "sell" and item["status"] == "conditional"
        assert item["transportPending"] is True and item["transport"] is None
        after = claim(done, item["monthlyAfter"])
        assert after["value"] == 88000 and "별도 운반비 반영 전" in label_of(reg_of[name], after)
        assert claim(done, item["difference"])["value"] == 188000
        assert done["output"]["totals"]["pending"] == ["st_defect"]
        total = claim(done, done["output"]["totals"]["difference"])
        assert "별도 운반비 반영 전" in label_of(reg_of[name], total)
        # 운반비가 한 달 이만큼을 넘으면 현행 유지가 낫다
        assert claim(done, item["transportRoom"])["value"] == 188000
        assert item["transportRoomAgainst"] == {"vendorOrgId": None, "route": "keep"}
        assert any(c["key"] == "transport:st_defect" for c in done["checks"])
        assert any(c["key"] == "pickup:st_defect" for c in done["checks"])

    done, item = out["visit"]
    assert item["route"] == "sell" and item["status"] == "calculated"
    assert item["pickupStatus"] == "visit" and item["transportPending"] is False
    assert claim(done, item["revenue"])["value"] == 88000
    fee = claim(done, item["transportFee"])
    assert fee["value"] == 30000 and fee["unit"] == "원/회" and FEE_DOC in fee["evidence"]
    count = claim(done, item["transportPickups"])
    assert count["value"] == 2 and COUNT_DOC in count["evidence"]
    assert claim(done, item["transportMonthly"])["value"] == 60000
    assert claim(done, item["transport"])["value"] == 60000
    assert claim(done, item["monthlyAfter"])["value"] == 28000
    assert claim(done, item["difference"])["value"] == 128000
    assert claim(done, done["output"]["totals"]["difference"])["value"] == 128000
    assert done["output"]["totals"]["pending"] == []

    done, item = out["unavailable"]
    assert item["route"] == "keep" and item["vendorOrgId"] is None
    assert claim(done, item["monthlyAfter"])["value"] == -100000
    assert claim(done, done["output"]["totals"]["difference"])["value"] == 0
    assert done["output"]["pickupExcluded"][0]["vendorOrgId"] == VENDOR


def test_cost_carbon_reports_the_same_states():
    reg = with_quote_docs(registry())
    visit = run(reg, "cost_carbon", stream=defect(), vendor=buyer(STATES["visit"]))
    assert claim(visit, "monthlyTransport")["value"] == 60000
    assert claim(visit, "monthlyAfter")["value"] == 28000
    assert visit["output"]["transportPending"] is False
    check = run(reg, "cost_carbon", stream=defect(), vendor=buyer(STATES["check"]))
    assert check["output"]["transportPending"] is True
    assert claim(check, "monthlyAfter")["value"] == 88000  # 어림값을 빼지 않는다
    assert not any(c["id"] == "monthlyTransport" for c in check["claims"])
    assert {c["key"] for c in check["checks"]} >= {"pickup", "transport"}
    gone = run(reg, "cost_carbon", stream=defect(), vendor=buyer(STATES["unavailable"]))
    assert next(c for c in gone["checks"] if c["key"] == "pickup")["status"] == "unmet"


# ── 100kg 반례 ─────────────────────────────────────────────────────────
def test_100kg_counterexample_is_provisional_until_the_quote_arrives():
    small = defect(monthlyKg=100)
    reg = registry()
    empty = run(reg, "optimize_assignment", streams=[small], vendors=[buyer(None, price=100)])
    item = empty["output"]["assignments"][0]
    # 운반 조건이 비어 있으면 0원으로 확정하지 않는다
    assert item["route"] == "sell" and item["status"] == "conditional"
    assert item["transportPending"] is True
    difference = claim(empty, item["difference"])
    assert difference["value"] == 35000 and "별도 운반비 반영 전" in label_of(reg, difference)
    assert claim(empty, item["transportRoom"])["value"] == 35000
    # 월 70,000원 견적(월 금액)을 넣으면 현행 유지가 낫다
    quoted = {"status": "visit", "basis": "separate", "feePerMonthKrw": 70000, "evidence": []}
    done = run(reg, "optimize_assignment", streams=[small], vendors=[buyer(quoted, price=100)])
    assert done["output"]["assignments"][0]["route"] == "keep"
    # 회당 35,000원 × 월 2회도 같다
    per_pickup = {
        "status": "visit",
        "basis": "separate",
        "feePerPickupKrw": 35000,
        "pickupsPerMonth": 2,
        "evidence": [],
    }
    done = run(reg, "optimize_assignment", streams=[small], vendors=[buyer(per_pickup, price=100)])
    assert done["output"]["assignments"][0]["route"] == "keep"


# ── 같은 수거 건에 운반비를 두 번 매기지 않는다 ─────────────────────────
@pytest.mark.parametrize(
    "transport",
    [
        {"status": "route", "basis": "separate", "feePerMonthKrw": 60000, "evidence": []},
        {
            "status": "route",
            "basis": "separate",
            "feePerPickupKrw": 30000,
            "pickupsPerMonth": 2,
            "evidence": [],
        },
    ],
)
def test_monthly_transport_is_charged_once_per_pickup_run(transport):
    reg = registry()
    a = defect()
    b = defect(id="st_runner", name="런너", monthlyKg=200)
    done = run(reg, "optimize_assignment", streams=[a, b], vendors=[buyer(transport)])
    items = done["output"]["assignments"]
    assert {i["vendorOrgId"] for i in items} == {VENDOR}
    assert sum(claim(done, i["transport"])["value"] for i in items) == 60000
    assert {claim(done, i["transportMonthly"])["value"] for i in items} == {60000}
    assert claim(done, done["output"]["totals"]["monthlyAfter"])["value"] == 220 * 600 - 60000


# ── 시연 시나리오: 업체 조건 답 → 결과 한 장 → 승인 ─────────────────────
def _miso_off_route(app, offroute):
    """미소환경이 노선 밖 공장에 대한 조건(offRoute)을 밝힌다. 금액·횟수 근거는 업체 문서다."""
    for eid, value, unit in (
        ("doc:org_miso:반입조건표:노선밖운반비", 30000, "원/회"),
        ("doc:org_miso:반입조건표:노선밖횟수", 2, "회/월"),
    ):
        app.registry.put(
            Evidence(
                id=eid,
                kind="doc",
                value=value,
                unit=unit,
                excerpt="노선 밖 방문 수거 조건(가상)",
                source="반입 조건표",
                as_of="2026-11-10",
                access=(MISO,),
            )
        )
    profile = copy.deepcopy(app.store.get_vendor_profile(MISO))
    profile["transport"] = {**profile["transport"], "offRoute": offroute}
    app.store.put_vendor_profile(profile)


VISIT = {
    "status": "visit",
    "basis": "separate",
    "paidBy": "emitter",
    "feePerPickupKrw": 30000,
    "pickupsPerMonth": 2,
    "evidence": ["doc:org_miso:반입조건표:노선밖운반비", "doc:org_miso:반입조건표:노선밖횟수"],
    "feeEvidence": ["doc:org_miso:반입조건표:노선밖운반비"],
    "pickupsEvidence": ["doc:org_miso:반입조건표:노선밖횟수"],
}


def _only_miso_for_defect(app, price=220):
    """새길자원의 B등급 기준을 빼고 미소환경 단가를 price로 둔다(불량 사출품을 미소환경만 받는다)."""
    saegil = copy.deepcopy(app.store.get_vendor_profile(SAEGIL))
    saegil["criteria"] = [c for c in saegil["criteria"] if c["id"] != "pc_saegil_pp_b"]
    app.store.put_vendor_profile(saegil)
    miso = copy.deepcopy(app.store.get_vendor_profile(MISO))
    for criterion in miso["criteria"]:
        criterion["unitPriceKrwPerKg"] = price
        app.registry.put(
            Evidence(
                id="doc:org_miso:매입기준표:220",
                kind="doc",
                value=price,
                unit="원/kg",
                excerpt="PP B등급 매입 단가(가상)",
                source="매입 기준표",
                as_of="2026-11-10",
                access=(MISO,),
            )
        )
        criterion["evidence"] = ["doc:org_miso:매입기준표:220", *criterion["evidence"][1:]]
    app.store.put_vendor_profile(miso)


def _defect_row(view):
    return next(a for a in view["result"]["assignments"] if a["streamId"] == "st_defect")


def test_demo_off_route_vendor_is_not_shown_as_transport_included():
    app = make_app()
    view = to_approval(app)
    row = _defect_row(view)
    assert row["vendorOrgId"] == MISO
    # 미소환경은 노선 수거에는 운반비 포함이지만, 이 공장은 노선 밖이다
    assert row["pickup"]["status"] == "check" and row["pickup"]["basis"] == "undecided"
    assert row["pickup"]["routeBasis"] == "included"
    assert row["transportPending"] is True and row["status"] == "conditional"
    assert (
        "별도 운반비 반영 전" in app.registry.get(row["monthlyAfter"]["evidence"][0], HANBIT).label
    )
    assert "st_defect" in view["result"]["transportPending"]
    route = next(c for c in row["checks"] if c["key"] == "pickup_route")
    assert route["status"] == "conditional" and "노선에 이 공장 지역이 없습니다" in route["text"]
    # 업체가 조건 답에 수거·운반 한 줄을 붙였고, 그 답의 메시지 번호를 결과가 가리킨다
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == MISO and t["messages"])
    said = [m for m in thread["messages"] if "수거·운반:" in m["text"]]
    assert said and row["pickup"]["seq"] == said[-1]["seq"]
    assert "따로 수거하러 갈 수 있는지는 담당자가 확인" in said[-1]["text"]
    # 비용·탄소 비교의 계산 범위를 밝힌다
    keys = [item["key"] for item in view["result"]["scope"]]
    assert keys == ["cost", "carbon"]
    # 새길자원(노선 포함, 운반비 포함)은 확정 조건이다
    sprue = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_sprue")
    assert sprue["pickup"]["status"] == "route" and sprue["transportPending"] is False


def test_vendor_quote_flows_into_the_result_and_matches_the_api():
    app = make_app()
    _only_miso_for_defect(app)
    _miso_off_route(app, VISIT)
    view = to_approval(app)
    row = _defect_row(view)
    assert row["vendorOrgId"] == MISO and row["route"] == "sell"
    assert row["pickup"]["status"] == "visit" and row["pickup"]["basis"] == "separate"
    assert row["pickup"]["paidBy"] == "emitter" and row["pickup"]["unit"] == "per_pickup"
    assert row["pickup"]["fee"]["value"] == 30000 and row["pickup"]["fee"]["unit"] == "원/회"
    assert row["pickup"]["pickups"]["value"] == 2
    assert row["pickup"]["monthly"]["value"] == 60000
    assert row["revenue"]["value"] == 88000
    assert row["transport"]["value"] == 60000
    assert row["monthlyAfter"]["value"] == 28000
    assert row["difference"]["value"] == 128000
    assert row["transportPending"] is False
    # 금액과 횟수의 근거는 업체 문서이고, 배출 기업이 열 수 있다
    assert "doc:org_miso:반입조건표:노선밖운반비" in row["pickup"]["fee"]["evidence"]
    for evidence_id in row["pickup"]["monthly"]["evidence"]:
        app.registry.get(evidence_id, HANBIT)
    # 업체 조건 답의 본문과 claim에 같은 견적이 있다
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == MISO and t["messages"])
    said = next(m for m in reversed(thread["messages"]) if "수거·운반:" in m["text"])
    assert row["pickup"]["seq"] == said["seq"]
    values = {(c["value"], c["unit"]) for c in said["claims"]}
    assert (30000, "원/회") in values and (2, "회/월") in values
    # 업체에게는 손익분기(배출 기업의 판단)를 보이지 않는다
    seen = service.get_view(app, view["session"]["id"], MISO)
    mine = _defect_row(seen)
    assert mine["pickup"]["room"] is None and mine["pickup"]["monthly"] is None
    assert mine["monthlyAfter"] is None and mine["pickup"]["status"] == "visit"
    assert any(c["key"] == "pickup_route" for c in mine["checks"])


def test_unavailable_vendor_is_left_out_of_assignment_and_totals():
    app = make_app()
    _only_miso_for_defect(app)
    _miso_off_route(
        app, {"status": "unavailable", "evidence": ["doc:org_miso:반입조건표:노선밖운반비"]}
    )
    view = to_approval(app)
    row = _defect_row(view)
    assert row["vendorOrgId"] is None and row["route"] == "keep"
    alternative = next(a for a in row["alternatives"] if a["vendorOrgId"] == MISO)
    assert alternative["monthlyAfter"] is None and alternative["note"].startswith("수거 불가")
    totals = view["result"]["totals"]
    assert totals["difference"]["value"] == sum(
        a["difference"]["value"] for a in view["result"]["assignments"] if a["difference"]
    )


def test_changed_pickup_terms_clear_approvals_and_rerun_refreshes_the_result():
    app = make_app()
    _only_miso_for_defect(app)
    view = to_approval(app)
    sid = view["session"]["id"]
    assert _defect_row(view)["pickup"]["status"] == "check"
    thread = next(
        t for t in view["threads"] if t["vendorOrgId"] == MISO and t["status"] == "agreed"
    )
    service.approve(app, sid, thread["id"], HANBIT)
    # 합의한 뒤에 미소환경이 노선 밖 방문 수거와 운반비를 밝혔다
    _miso_off_route(app, VISIT)
    service._recheck(app, MISO)
    blocked = service.get_view(app, sid, HANBIT)
    approval = next(a for a in blocked["approvals"] if a["threadId"] == thread["id"])
    assert approval["blocked"] and "PICKUP_TERMS_CHANGED" in approval["blockedCodes"]
    assert approval["emitter"] is False  # 앞선 승인은 지웠다
    assert (
        "별도 수거 확인 필요 · 운반비 미정 → 지금: 업체 수거 가능 확인" in approval["blockedReason"]
    )
    with pytest.raises(service.ServiceError):
        service.approve(app, sid, thread["id"], HANBIT)
    # 다시 진행하면 대화를 다시 열고 새 견적으로 결과와 합계를 다시 계산한다
    fresh = service.run_session(app, sid, HANBIT)
    row = _defect_row(fresh)
    assert row["pickup"]["status"] == "visit" and row["pickup"]["monthly"]["value"] == 60000
    assert row["monthlyAfter"]["value"] == 28000 and row["difference"]["value"] == 128000
    again = next(a for a in fresh["approvals"] if a["threadId"] == thread["id"])
    assert not again["blocked"] and again["emitter"] is False
    done = approve_all(app, fresh)
    assert done["session"]["status"] == "confirmed"


def test_changing_route_transport_after_agreement_blocks_approval():
    """노선 수거 조건(운반비 포함 → 회당 금액 별도)을 합의 뒤에 바꿔도 막힌다."""
    app = make_app()
    view = to_approval(app)
    sid = view["session"]["id"]
    thread = next(
        t for t in view["threads"] if t["vendorOrgId"] == SAEGIL and t["status"] == "agreed"
    )
    eid = "doc:org_saegil:반입조건표:운반비"
    app.registry.put(
        Evidence(
            id=eid,
            kind="doc",
            value=50000,
            unit="원/회",
            excerpt="운반비는 단가와 따로 회당 50,000원(가상)",
            source="반입 조건표",
            as_of="2026-11-10",
            access=(SAEGIL,),
        )
    )
    profile = copy.deepcopy(app.store.get_vendor_profile(SAEGIL))
    profile["transport"] = {
        "paidBy": "emitter",
        "feePerPickupKrw": 50000,
        "evidence": [eid],
        "feeEvidence": [eid],
    }
    app.store.put_vendor_profile(profile)
    service._recheck(app, SAEGIL)
    blocked = service.get_view(app, sid, HANBIT)
    approval = next(a for a in blocked["approvals"] if a["threadId"] == thread["id"])
    assert approval["blocked"] and "PICKUP_TERMS_CHANGED" in approval["blockedCodes"]
    fresh = service.run_session(app, sid, HANBIT)
    sprue = next(a for a in fresh["result"]["assignments"] if a["streamId"] == "st_sprue")
    if sprue["vendorOrgId"] == SAEGIL:
        assert sprue["pickup"]["basis"] == "separate" and sprue["pickup"]["monthly"]["value"] > 0
        assert sprue["transport"]["value"] > 0


def test_per_pickup_quote_without_a_count_is_not_filled_from_the_minimum():
    """회당 견적만 있고 월 횟수가 없으면 업체가 정한 월 최소 횟수로 채우지 않는다(0원도 아니다)."""
    reg = with_quote_docs(registry())
    quote = {
        "status": "visit",
        "basis": "separate",
        "feePerPickupKrw": 30000,
        "evidence": [FEE_DOC],
        "feeEvidence": [FEE_DOC],
    }
    done = run(reg, "optimize_assignment", streams=[defect()], vendors=[buyer(quote)])
    item = done["output"]["assignments"][0]
    assert item["transportPending"] is True and item["transport"] is None
    check = next(c for c in done["checks"] if c["key"] == "transport:st_defect")
    assert "월 수거 횟수 확인 필요" in check["text"]
    cost = run(reg, "cost_carbon", stream=defect(), vendor=buyer(quote))
    assert cost["output"]["transportPending"] is True
    assert (
        "월 수거 횟수 확인 필요"
        in next(c for c in cost["checks"] if c["key"] == "transport")["text"]
    )
