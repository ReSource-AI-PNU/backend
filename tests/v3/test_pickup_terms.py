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


def originals(reg, found):
    """claim 근거 가운데 업체 문서. 계산 당시 판을 고정한 사본(doc:snapshot)이면 원본 ID로 읽는다."""
    out = []
    for eid in found["evidence"]:
        if eid.startswith("doc:snapshot:"):
            out.append(reg.get(eid, EMITTER).meta["originalId"])
        elif eid.startswith("doc:"):
            out.append(eid)
    return out


def formula_of(reg, found, viewer=EMITTER):
    """calc 근거의 계산식(근거 패널 본문)."""
    found = reg.get(found["evidence"][0], viewer)
    return f"{(found.meta or {}).get('formula') or ''} {found.excerpt or ''}"


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


def test_service_area_and_off_route_stance_decide_together():
    routes = [{"name": "미음", "days": ["화"], "regions": ["부산 강서구 미음"], "complexes": []}]
    region = "부산 강서구 녹산산단"
    included = {"paidBy": "vendor", "evidence": []}
    inside = pickup.effective(included, routes, region, {"serviceAreas": ["부산 강서구"]})
    assert (
        inside["status"] == "visit" and inside["reason"] == "area" and inside["basis"] == "included"
    )
    outside = pickup.effective(included, routes, region, {"serviceAreas": ["경남 양산시"]})
    assert outside["status"] == "unavailable" and outside["reason"] == "area"
    assert outside["areas"] == ["경남 양산시"]
    far = pickup.effective(included, routes, region, {"maxPickupKm": 10}, 25.0)
    assert far["status"] == "unavailable" and far["reason"] == "distance"
    unknown_km = pickup.effective(included, routes, region, {"maxPickupKm": 10}, None)
    assert unknown_km["status"] == "check" and unknown_km["reason"] == "distance"
    # 지역 안이지만 노선 밖 수거 불가를 밝혔으면 어느 쪽으로도 정하지 않는다
    conflict = pickup.effective(
        {**included, "offRoute": {"status": "unavailable"}},
        routes,
        region,
        {"serviceAreas": ["부산 강서구"]},
    )
    assert conflict["status"] == "check" and conflict["reason"] == "conflict"
    assert pickup.monthly(conflict) is None
    # 지역 밖이어도 노선 밖 방문을 밝혔으면 그 견적으로 간다
    visit = pickup.effective(
        {**included, "offRoute": {"status": "visit", "paidBy": "emitter", "feePerMonthKrw": 50000}},
        routes,
        region,
        {"serviceAreas": ["경남 양산시"]},
    )
    assert visit["status"] == "visit" and pickup.monthly(visit) == 50000


# ── 네 상태를 같은 폐기물로 비교 ─────────────────────────────────────────
def test_four_pickup_states_on_the_same_waste():
    out, reg_of = {}, {}
    for name, transport in STATES.items():
        reg = reg_of[name] = with_quote_docs(registry())
        # 전처리 작업이 없다고 확인한 흐름(작업비 확정): 잠정 여부는 운반비만으로 갈린다
        done = run(
            reg,
            "optimize_assignment",
            streams=[defect(pretreatHoursPerMonth=0)],
            vendors=[buyer(transport)],
        )
        out[name] = (done, done["output"]["assignments"][0])

    for name in ("unknown", "check"):
        done, item = out[name]
        assert item["route"] == "sell" and item["status"] == "conditional"
        assert item["transportPending"] is True and item["transport"] is None
        after = claim(done, item["monthlyAfter"])
        # 이름은 잠정 금액임을, 계산식은 별도 운반비 반영 전임을 밝힌다
        assert after["value"] == 88000 and "미확인 비용 반영 전" in label_of(reg_of[name], after)
        assert "별도 운반비 반영 전" in formula_of(reg_of[name], after)
        assert claim(done, item["difference"])["value"] == 188000
        assert done["output"]["totals"]["pending"] == ["st_defect"]
        total = claim(done, done["output"]["totals"]["difference"])
        assert "미확인 비용 반영 전" in label_of(reg_of[name], total)
        # 운반비가 한 달 이만큼을 넘으면 현행 유지가 낫다
        assert claim(done, item["transportRoom"])["value"] == 188000
        assert item["transportRoomAgainst"] == {"vendorOrgId": None, "route": "keep"}
        assert any(c["key"] == "transport:st_defect" for c in done["checks"])
        assert any(c["key"] == "pickup:st_defect" for c in done["checks"])

    done, item = out["visit"]
    # 수거·운반은 확정이다(배정 상태는 허가증 원문 대조 같은 다른 확인 때문에 조건부일 수 있다)
    assert item["route"] == "sell"
    assert not [c for c in done["checks"] if c["key"].startswith(("transport:", "pickup:"))]
    assert item["pickupStatus"] == "visit" and item["transportPending"] is False
    assert claim(done, item["revenue"])["value"] == 88000
    fee = claim(done, item["transportFee"])
    reg = reg_of["visit"]
    assert fee["value"] == 30000 and fee["unit"] == "원/회" and originals(reg, fee) == [FEE_DOC]
    count = claim(done, item["transportPickups"])
    assert count["value"] == 2 and originals(reg, count) == [COUNT_DOC]
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
    assert difference["value"] == 35000 and "미확인 비용 반영 전" in label_of(reg, difference)
    assert "별도 운반비 반영 전" in formula_of(reg, difference)
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
    app = make_app(miso_visits=False)
    view = to_approval(app)
    row = _defect_row(view)
    assert row["vendorOrgId"] == MISO
    # 미소환경은 노선 수거에는 운반비 포함이지만, 이 공장은 노선 밖이다
    assert row["pickup"]["status"] == "check" and row["pickup"]["basis"] == "undecided"
    assert row["pickup"]["routeBasis"] == "included"
    assert row["transportPending"] is True and row["status"] == "conditional"
    assert (
        "미확인 비용 반영 전" in app.registry.get(row["monthlyAfter"]["evidence"][0], HANBIT).label
    )
    assert "별도 운반비 반영 전" in formula_of(app.registry, row["monthlyAfter"], HANBIT)
    # 필수 운반비가 미정이라 계약 검토 담당이 이 대화의 승인을 막는다(잠정 배정만 보인다)
    approval = next(a for a in view["approvals"] if a["threadId"] == row["threadId"])
    assert approval["review"]["status"] == "blocked"
    assert any(
        i["key"] == "transport" and i["status"] == "block" for i in approval["review"]["items"]
    )
    assert "st_defect" in view["result"]["transportPending"]
    route = next(c for c in row["checks"] if c["key"] == "pickup_route")
    assert route["status"] == "conditional" and "노선에 이 공장 지역이 없습니다" in route["text"]
    # 업체가 조건 답에 수거·운반 한 줄을 붙였고, 그 답의 메시지 번호를 결과가 가리킨다
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == MISO and t["messages"])
    said = [m for m in thread["messages"] if "수거·운반:" in m["text"]]
    assert said and row["pickup"]["seq"] == said[-1]["seq"]
    assert "따로 수거하러 갈 수 있는지는 담당자가 확인" in said[-1]["text"]
    # 비용 비교의 계산 범위를 밝힌다. 운송 배출 계수가 원문 확인 전이라 탄소 운송 범위 줄은 없다
    scope = {item["key"]: item["text"] for item in view["result"]["scope"]}
    assert list(scope) == ["cost"] and "별도 운반비 반영 전" in scope["cost"]
    assert all(a.get("carbonTransport") is None for a in view["result"]["assignments"])
    # 새길자원(노선 포함, 운반비 포함)은 확정 조건이다
    sprue = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_sprue")
    assert sprue["pickup"]["status"] == "route" and sprue["transportPending"] is False


def test_vendor_quote_flows_into_the_result_and_matches_the_api():
    app = make_app(miso_visits=False)
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
    # 금액과 횟수의 근거는 업체 문서(계산 당시 판을 고정한 사본)이고, 배출 기업이 열 수 있다
    frozen = [
        app.registry.get(e, HANBIT)
        for e in row["pickup"]["fee"]["evidence"]
        if e.startswith("doc:snapshot:")
    ]
    assert [f.meta["originalId"] for f in frozen] == ["doc:org_miso:반입조건표:노선밖운반비"]
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
    app = make_app(miso_visits=False)
    _only_miso_for_defect(app)
    _miso_off_route(
        app, {"status": "unavailable", "evidence": ["doc:org_miso:반입조건표:노선밖운반비"]}
    )
    view = to_approval(app)
    row = _defect_row(view)
    assert row["vendorOrgId"] is None and row["route"] == "keep"
    alternative = next(a for a in row["alternatives"] if a["vendorOrgId"] == MISO)
    assert alternative["monthlyAfter"] is None and alternative["note"].startswith("수거 불가")
    # 옮길 곳이 없어진 까닭(수거 불가)을 다른 확인 항목보다 먼저 적는다
    first = row["checks"][0]
    assert first["key"] == "pickup_route" and first["status"] == "unmet"
    assert (
        "(주)미소환경은 이 공장까지 수거하러 올 수 없다고 밝혀 배정에서 뺐습니다" in first["text"]
    )
    totals = view["result"]["totals"]
    assert totals["difference"]["value"] == sum(
        a["difference"]["value"] for a in view["result"]["assignments"] if a["difference"]
    )


def test_changed_pickup_terms_clear_approvals_and_rerun_refreshes_the_result():
    app = make_app(miso_visits=False)
    _only_miso_for_defect(app)
    view = to_approval(app)
    sid = view["session"]["id"]
    assert _defect_row(view)["pickup"]["status"] == "check"
    thread = next(
        t for t in view["threads"] if t["vendorOrgId"] == MISO and t["status"] == "agreed"
    )
    # 운반비가 미정인 동안에는 계약 검토 담당이 막아 승인할 수 없다
    with pytest.raises(service.ServiceError) as refused:
        service.approve(app, sid, thread["id"], HANBIT)
    assert refused.value.code == "REVIEW_BLOCKED"
    # 합의한 뒤에 미소환경이 노선 밖 방문 수거와 운반비를 밝혔다
    _miso_off_route(app, VISIT)
    service._recheck(app, MISO)
    blocked = service.get_view(app, sid, HANBIT)
    approval = next(a for a in blocked["approvals"] if a["threadId"] == thread["id"])
    assert approval["blocked"] and "PICKUP_TERMS_CHANGED" in approval["blockedCodes"]
    assert approval["emitter"] is False
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
    app = make_app(miso_visits=False)
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


def test_off_route_quote_without_a_count_stays_pending_in_the_conversation():
    """노선 밖 방문 견적에 월 횟수가 없으면 업체 측 답이 정기 수거 횟수를 끌어다 운반비를 확정하지 않는다."""
    app = make_app(miso_visits=False)
    _only_miso_for_defect(app)
    no_count = {
        **VISIT,
        "evidence": ["doc:org_miso:반입조건표:노선밖운반비"],
        "pickupsEvidence": [],
    }
    no_count.pop("pickupsPerMonth")
    _miso_off_route(app, no_count)
    view = to_approval(app)
    row = _defect_row(view)
    assert row["vendorOrgId"] == MISO and row["pickup"]["status"] == "visit"
    assert row["pickup"]["fee"]["value"] == 30000 and row["pickup"]["pickups"] is None
    assert row["pickup"]["monthly"] is None and row["transportPending"] is True
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == MISO and t["messages"])
    said = next(m for m in reversed(thread["messages"]) if "수거·운반:" in m["text"])
    assert "월 수거 횟수는 담당자가 확인한 뒤 알려 드립니다" in said["text"]


def test_reopened_thread_renegotiates_so_price_and_money_agree():
    """수거·운반 조건만 바뀌어 대화를 다시 열어도 표시 단가와 계산 단가가 같은 협상 결과를 쓴다(협상 켬)."""
    app = make_app(negotiate=True, miso_visits=False)
    saegil = copy.deepcopy(app.store.get_vendor_profile(SAEGIL))
    saegil["guardrails"]["autoDecline"] = [*saegil["guardrails"]["autoDecline"], "금속 인서트"]
    app.store.put_vendor_profile(saegil)
    view = to_approval(app)
    sid = view["session"]["id"]
    before = _defect_row(view)
    assert before["vendorOrgId"] == MISO
    price = before["unitPrice"]["value"]
    assert before["revenue"]["value"] == price * 400
    _miso_off_route(app, VISIT)
    service._recheck(app, MISO)
    fresh = service.run_session(app, sid, HANBIT)
    row = _defect_row(fresh)
    assert row["vendorOrgId"] == MISO and row["pickup"]["status"] == "visit"
    # 다시 협상한 단가가 표시와 계산에 함께 쓰인다(처음 협상과 같은 범위라 같은 단가다)
    assert row["unitPrice"]["value"] == price
    assert row["revenue"]["value"] == row["unitPrice"]["value"] * 400
    assert row["transport"]["value"] == 60000
    assert row["monthlyAfter"]["value"] == row["revenue"]["value"] - 60000
    alternatives = [a for a in row["alternatives"] if a["monthlyAfter"] and a["unitPrice"]]
    for item in alternatives:
        assert item["monthlyAfter"]["value"] <= item["unitPrice"]["value"] * 400


def test_demo_view_never_measures_pickup_distance_or_writes():
    """조회(진단 뷰, 계약 검토, 승인 재확인)는 길찾기를 부르거나 근거를 쓰지 않는다(체험하기는 쓰기를 거절한다).

    최대 수거 거리를 밝힌 업체(새길자원·남해열분해 시드)는 배정을 돌릴 때 잰 거리를 진단에 적어 두고, 조회는 그 값을
    읽는다. 적어 둔 값이 없는 예전 진단은 직선 거리 × 우회 계수로 어림한다.
    """
    from app.v3.demo.scope import demo_app
    from app.v3.judge.common import haversine_km

    def road(points):  # 가상 도로 거리 제공자. 우회 계수 가정값은 쓰지 않는다
        return [[haversine_km(a, b) * 1.3 for b in points] for a in points]

    app = make_app(miso_visits=False, distance_provider=road)
    view = to_approval(app)
    sid = view["session"]["id"]
    session = app.store.get_session(sid)
    assert session["work"].get("pickupKm")  # 배정 때 잰 거리

    def boom(points):
        raise AssertionError("조회에서 길찾기를 부르면 안 된다")

    app.distance_provider = boom
    for viewer in (HANBIT, MISO, SAEGIL):
        shown = service.get_view(demo_app(app), sid, viewer)
        assert shown["approvals"] and all(a.get("review") for a in shown["approvals"])
    session["work"].pop("pickupKm")
    app.store.put_session(session)
    shown = service.get_view(demo_app(app), sid, HANBIT)
    assert all(a.get("review") for a in shown["approvals"])


def test_matching_route_without_comparable_range_keeps_existing_terms():
    routes = [{"regions": ["부산 강서구"], "days": ["화", "목"]}]
    transport = {"paidBy": "vendor"}
    region = "부산 강서구 녹산산단"
    for fleet, km in (({}, 404), ({"maxPickupKm": 40}, None)):
        terms = pickup.effective(transport, routes, region, fleet, km)
        assert terms["status"] == "route"
        assert terms["days"] == ["화", "목"]
        assert pickup.monthly(terms) == 0
    area_conflict = pickup.effective(transport, routes, region, {"serviceAreas": ["경남 양산시"]})
    assert area_conflict["status"] == "check"
    assert "가능 지역 밖" in pickup.route_conflict_detail(area_conflict)
    assert pickup.monthly(area_conflict) is None


@pytest.mark.parametrize(
    ("fee", "a_profit", "selected"),
    [(None, 320000, "org_a"), (69000, 251000, "org_a"), (70000, 250000, "org_a"), (71000, 249000, "org_b")],
)
def test_transport_threshold_between_two_buyers(fee, a_profit, selected):
    runner = defect(monthlyKg=1000, pretreatHoursPerMonth=0)
    quote = {"status": "visit", "basis": "separate"}
    if fee is not None:
        quote["feePerMonthKrw"] = fee
    a = buyer(quote, price=320, orgId="org_a")
    b = buyer({"status": "visit", "basis": "included"}, price=250, orgId="org_b")
    reg = registry()
    # 두 업체의 처리 방식과 거리는 같다. 비용만 달리하면 70,000원에서 돈 기준으로 동률이다.
    for profile, expected in ((a, a_profit), (b, 250000)):
        cost = run(reg, "cost_carbon", stream=runner, vendor=profile)
        assert claim(cost, "monthlyAfter")["value"] == expected
    result = run(reg, "optimize_assignment", streams=[runner], vendors=[a, b])
    [item] = result["output"]["assignments"]
    assert item["vendorOrgId"] == selected
    assert item["transportPending"] is (fee is None)
    if fee is None:
        assert item["transportMonthly"] is None
        assert claim(result, item["transportRoom"])["value"] == 70000
        assert item["transportRoomAgainst"] == {"vendorOrgId": "org_b", "route": "sell"}
    else:
        assert item["transportRoom"] is None


@pytest.mark.parametrize(("other_basis", "hours"), [("separate", 0), ("included", None)])
def test_no_vendor_threshold_until_comparable_costs_are_known(other_basis, hours):
    runner = defect(monthlyKg=1000, pretreatHoursPerMonth=hours)
    a = buyer({"status": "visit", "basis": "separate"}, price=320, orgId="org_a")
    b = buyer({"status": "visit", "basis": other_basis}, price=250, orgId="org_b")
    result = run(registry(), "optimize_assignment", streams=[runner], vendors=[a, b])
    [item] = result["output"]["assignments"]
    assert item["vendorOrgId"] == "org_a" and item["transportPending"]
    assert item["transportRoom"] is None and item["transportRoomAgainst"] is None


def test_transport_quote_per_visit_is_multiplied_before_comparing_monthly_profit():
    runner = defect(monthlyKg=1000, pretreatHoursPerMonth=0)
    a = buyer(
        {"status": "visit", "basis": "separate", "feePerPickupKrw": 50000, "pickupsPerMonth": 2},
        price=320,
        orgId="org_a",
    )
    result = run(registry(), "optimize_assignment", streams=[runner], vendors=[a])
    [item] = result["output"]["assignments"]
    assert claim(result, item["transportMonthly"])["value"] == 100000
    assert claim(result, item["monthlyAfter"])["value"] == 220000
