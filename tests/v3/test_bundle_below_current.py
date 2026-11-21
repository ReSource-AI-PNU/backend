"""묶음 때문에 현행보다 불리한 배정(2026-11-22).

같은 업체의 최소 물량을 채우려고, 혼자 보면 지금보다 손해인 폐기물을 묶음에 넣는 배정은 운반비·작업비가 모두
확인되고 묶음 전체가 현행보다 나을 때만 허용하고, 그 폐기물에 손해와 묶음 전체 차이를 밝혀 조건부로 둔다.
회사와 수치는 모두 가상이다.
"""

import pytest

from app.v3 import service
from tests.v3.judge_fixtures import assert_grounded, claim, registry, run, stream, vendor
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app, to_approval

INCLUDED = {"paidBy": "vendor", "evidence": []}  # 운반비는 단가에 포함(확인된 비용)


def pair(sale, hours=0):
    """사출 스크랩(지금 kg당 200원을 내고 버림)과 런너(지금 kg당 sale원에 팖). 둘 다 300kg, 업체 최소 500kg."""
    extra = {} if hours is None else {"pretreatHoursPerMonth": hours}
    scrap = stream(
        id="st_sprue",
        monthlyKg=300,
        currentTreatment={"method": "혼합 소각", "unitCostKrwPerKg": 200, "evidence": []},
        **extra,
    )
    runner = stream(
        id="st_runner",
        name="런너",
        monthlyKg=300,
        currentTreatment={"sale": True, "salePriceKrwPerKg": sale, "evidence": []},
        **extra,
    )
    return [scrap, runner]


def assign(streams):
    reg = registry()
    done = run(reg, "optimize_assignment", streams=streams, vendors=[vendor(transport=INCLUDED)])
    return reg, done, {a["streamId"]: a for a in done["output"]["assignments"]}


def test_a_loss_inside_a_paying_bundle_is_allowed_and_disclosed():
    reg, done, items = assign(pair(350))
    assert {s: a["vendorOrgId"] for s, a in items.items()} == {
        "st_sprue": SAEGIL,
        "st_runner": SAEGIL,
    }
    runner = items["st_runner"]
    loss = claim(done, runner["difference"])["value"]
    assert loss == pytest.approx(320 * 300 - 350 * 300)  # 혼자 보면 한 달 9,000원 손해
    below = runner["bundleBelowCurrent"]
    assert below["streamIds"] == ["st_sprue"] and below["loss"] == runner["difference"]
    whole = claim(done, below["bundle"])
    gain = claim(done, items["st_sprue"]["difference"])["value"]
    assert whole["value"] == pytest.approx(loss + gain) and whole["value"] > 0
    assert runner["status"] == "conditional"
    assert items["st_sprue"]["bundleBelowCurrent"] is None  # 이득인 폐기물에는 붙이지 않는다
    check = next(c for c in done["checks"] if c["key"] == "bundle_below_current:st_runner")
    assert check["status"] == "conditional"
    assert len(check["evidence"]) == 2  # 이 폐기물의 차이와 묶음 전체 차이의 계산 근거
    assert_grounded(reg, done)


def test_unconfirmed_labor_never_pays_for_another_streams_loss():
    _, _, items = assign(pair(350, hours=None))  # 전처리 작업비 미확인: 잠정 금액
    assert all(a["vendorOrgId"] is None for a in items.values())
    assert all(a["bundleBelowCurrent"] is None for a in items.values())


def test_a_bundle_that_loses_overall_keeps_the_current_route():
    _, done, items = assign(pair(900))  # 런너를 지금 kg당 900원에 판다: 묶음 전체가 손해
    assert all(a["vendorOrgId"] is None for a in items.values())
    assert not any(c["key"].startswith("bundle_below_current") for c in done["checks"])


def test_demo_buyer_paying_less_than_today_stays_out_while_costs_are_unconfirmed():
    """시연: 스프루를 지금 kg당 400원에 파는데 새길자원은 320원. 작업비가 미확인이라 묶음 손해를 허용하지 않는다."""
    app = make_app()
    service.update_stream(
        app,
        HANBIT,
        "st_sprue",
        {"currentTreatment": {"sale": True, "salePriceKrwPerKg": 400}},
        HANBIT,
    )
    view = to_approval(app)
    sprue = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_sprue")
    assert sprue["vendorOrgId"] is None and sprue["bundleBelowCurrent"] is None
    assert all(
        (a["difference"] or {}).get("value", 0) >= 0 or a["bundleBelowCurrent"]
        for a in view["result"]["assignments"]
    )


def test_the_result_and_approval_show_why_a_losing_stream_is_in_the_bundle():
    """시연: 미소환경이 수거 불가, 필름은 지금 비싸게 팔고 있어 새길자원 최소 물량을 사출 불량품만으로 못 채운다.
    작업비를 모두 확인하면(작업 없음) 스프루를 kg당 400원 판매에서 320원으로 옮기는 묶음이 허용되고, 그 까닭이 보인다.
    """
    app = make_app()
    for sid in ("st_sprue", "st_defect", "st_film", "st_purge"):
        service.update_stream(app, HANBIT, sid, {"pretreatHoursPerMonth": 0}, HANBIT)
    sale = {"sale": True, "salePriceKrwPerKg": 400}
    service.update_stream(app, HANBIT, "st_sprue", {"currentTreatment": sale}, HANBIT)
    expensive = {"sale": True, "salePriceKrwPerKg": 1000}
    service.update_stream(app, HANBIT, "st_film", {"currentTreatment": expensive}, HANBIT)
    unavailable = {"offRoute": {"status": "unavailable"}}
    service.update_vendor(app, "org_miso", {"transport": unavailable}, "org_miso")
    view = to_approval(app)
    items = {a["streamId"]: a for a in view["result"]["assignments"]}
    sprue = items["st_sprue"]
    assert sprue["vendorOrgId"] == SAEGIL and items["st_defect"]["vendorOrgId"] == SAEGIL
    below = sprue["bundleBelowCurrent"]
    assert below["loss"]["value"] == pytest.approx(320 * 600 - 400 * 600)
    assert below["bundle"]["value"] == pytest.approx(
        below["loss"]["value"] + items["st_defect"]["difference"]["value"]
    )
    assert below["bundle"]["value"] > 0 and below["streamIds"] == ["st_defect"]
    assert sprue["status"] == "conditional"
    check = next(c for c in sprue["checks"] if c["key"] == "bundle_below_current")
    assert [c["id"] for c in check["claims"]] == [
        "st_sprue_bundleLoss",
        "st_sprue_bundleDifference",
    ]
    assert "{{st_sprue_bundleLoss}}" in check["text"] and "불량 사출품" in check["text"]
    # 업체는 배출 기업의 현행 손익 비교를 보지 못한다
    theirs = service.get_view(app, view["session"]["id"], SAEGIL)["result"]
    mine = next(a for a in theirs["assignments"] if a["streamId"] == "st_sprue")
    assert mine["bundleBelowCurrent"] is None
    assert all(c["key"] != "bundle_below_current" for c in mine["checks"])
