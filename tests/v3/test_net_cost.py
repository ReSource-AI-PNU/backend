"""순비용 = 처리비 − 매각 수익 + 운반비 + 전처리 인건비 (설계안 v3 8.6). 회사와 수치는 모두 가상이다."""

import pytest

from tests.v3.judge_fixtures import (
    EMITTER,
    claim,
    factor_gate,  # noqa: F401 - 시험 전용 배출계수를 받는 픽스처
    grade_gate,  # noqa: F401 - 팀 등급 임계값을 받는 픽스처(등급 계산 경로만 본다)
    registry,
    run,
    vendor,
    with_test_factors,
)
from tests.v3.judge_fixtures import (
    answered_stream as stream,
)


def paid_by_emitter(fee=30000, **over):
    return vendor(transport={"paidBy": "emitter", "feePerPickupKrw": fee, "evidence": []}, **over)


def test_transport_in_the_price_costs_nothing_extra():
    reg = registry()
    included = vendor(transport={"paidBy": "vendor", "evidence": []})
    cost = run(reg, "cost_carbon", stream=stream(), vendor=included)
    assert claim(cost, "monthlyAfter")["value"] == 320 * 600
    check = next(c for c in cost["checks"] if c["key"] == "transport")
    assert check["status"] == "met" and "수거 포함" in check["text"]
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[included])
    item = done["output"]["assignments"][0]
    assert item["transportPaidBy"] == "vendor"
    assert claim(done, item["transport"])["value"] == 0
    assert claim(done, item["monthlyAfter"])["value"] == claim(done, item["revenue"])["value"]


def test_separate_transport_fee_is_subtracted_once_per_vendor():
    reg = registry()
    hauler = paid_by_emitter(30000)  # 월 2회 수거: 한 달 운반비는 업체당 한 번만 든다
    a = stream()
    b = stream(id="st_runner", name="런너", monthlyKg=200)
    done = run(reg, "optimize_assignment", streams=[a, b], vendors=[hauler])
    items = {i["streamId"]: i for i in done["output"]["assignments"]}
    assert {i["vendorOrgId"] for i in items.values()} == {hauler["orgId"]}
    shares = [claim(done, i["transport"])["value"] for i in items.values()]
    assert sum(shares) == 60000 and shares == [45000, 15000]  # 물량 비율로 나눈다
    after = sum(claim(done, i["monthlyAfter"])["value"] for i in items.values())
    assert after == 320 * 800 - 60000
    assert claim(done, done["output"]["totals"]["monthlyAfter"])["value"] == after
    # 한 흐름만 볼 때(cost_carbon)는 운반비 전부를 그 흐름에 얹어 보여 준다
    alone = run(reg, "cost_carbon", stream=b, vendor=hauler)
    assert claim(alone, "monthlyTransport")["value"] == 60000
    assert claim(alone, "monthlyAfter")["value"] == 320 * 200 - 60000


def test_keeps_the_current_route_when_transport_eats_the_gain():
    reg = registry()
    small = stream(monthlyKg=100, currentTreatment={"method": "혼합 소각", "unitCostKrwPerKg": 250})
    # 100kg을 kg당 100원에 사 가도, 월 운반비가 더 크면 옮기지 않는다
    buyer = paid_by_emitter(
        20000,
        criteria=[
            {
                "id": "pc_small",
                "material": "PP",
                "unitPriceKrwPerKg": 100,
                "minMonthlyKg": 0,
                "evidence": [],
            }
        ],
    )
    done = run(reg, "optimize_assignment", streams=[small], vendors=[buyer])
    assert done["output"]["assignments"][0]["route"] == "keep"
    cheaper = paid_by_emitter(10000, criteria=buyer["criteria"])
    done = run(reg, "optimize_assignment", streams=[small], vendors=[cheaper])
    item = done["output"]["assignments"][0]
    assert item["route"] == "sell"
    assert claim(done, item["difference"])["value"] == (100 * 100 - 20000) - (-250 * 100)


def test_unknown_transport_fee_is_left_out_and_flagged():
    reg = registry()
    silent = vendor(transport={"paidBy": "emitter", "evidence": []})
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[silent])
    item = done["output"]["assignments"][0]
    assert item["status"] == "conditional" and item["transport"] is None
    assert any(c["key"].startswith("transport:") for c in done["checks"])
    cost = run(reg, "cost_carbon", stream=stream(), vendor=silent)
    check = next(c for c in cost["checks"] if c["key"] == "transport")
    assert check["status"] == "conditional" and "확인 필요" in check["text"]


def test_pretreatment_labor_needs_the_companys_own_rate_and_never_uses_the_minimum_wage():
    reg = registry()
    # 작업 시간만 있고 시간당 인건비가 없으면 최저임금을 실제 작업비로 대입하지 않는다(잠정 금액)
    sorted_by_hand = stream(pretreatHoursPerMonth=6)
    done = run(reg, "optimize_assignment", streams=[sorted_by_hand], vendors=[vendor()])
    item = done["output"]["assignments"][0]
    assert item["labor"] is None and item["costPending"] is True
    assert claim(done, item["monthlyAfter"])["value"] == 320 * 600
    after = claim(done, item["monthlyAfter"])
    assert "잠정" in reg.get(after["evidence"][0], EMITTER).label
    assert all("ref:최저임금" not in str(c["evidence"]) for c in done["claims"])
    cost = run(reg, "cost_carbon", stream=sorted_by_hand, vendor=vendor())
    assert all(c["id"] != "monthlyLabor" for c in cost["claims"])
    check = next(c for c in cost["checks"] if c["key"] == "labor_unknown")
    assert check["status"] == "conditional" and "최저임금" in check["text"]
    assert cost["output"]["costPending"] is True
    # 회사가 시간당 인건비를 넣으면 그 값으로 뺀다
    own = stream(pretreatHoursPerMonth=6, laborCostKrwPerHour=15000)
    done = run(reg, "optimize_assignment", streams=[own], vendors=[vendor()])
    item = done["output"]["assignments"][0]
    assert claim(done, item["labor"])["value"] == 90000 and item["costPending"] is False
    assert claim(done, item["monthlyAfter"])["value"] == 320 * 600 - 90000
    cost = run(reg, "cost_carbon", stream=own, vendor=vendor())
    assert claim(cost, "monthlyLabor")["value"] == 90000
    # 작업 시간이 없으면 인건비를 지어내지 않고 미확인으로 둔다. 작업이 없다고 밝히면(0) 확정이다
    plain = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()])
    assert plain["output"]["assignments"][0]["labor"] is None
    assert plain["output"]["assignments"][0]["costPending"] is True
    none = run(
        reg, "optimize_assignment", streams=[stream(pretreatHoursPerMonth=0)], vendors=[vendor()]
    )
    assert none["output"]["assignments"][0]["costPending"] is False


def test_labor_can_make_selling_worse_than_keeping():
    reg = registry()
    # 회사가 밝힌 작업 시간·시간당 인건비: 인건비 103,200원
    heavy = stream(monthlyKg=100, pretreatHoursPerMonth=10, laborCostKrwPerHour=10320)
    done = run(
        reg,
        "optimize_assignment",
        streams=[heavy],
        vendors=[
            vendor(
                criteria=[
                    {
                        "id": "pc",
                        "material": "PP",
                        "unitPriceKrwPerKg": 320,
                        "minMonthlyKg": 0,
                        "evidence": [],
                    }
                ]
            )
        ],
    )
    # 팔면 +32,000 − 103,200 = −71,200, 지금은 −25,000(담당자가 답한 혼합 소각 처리비 kg당 250원)
    assert done["output"]["assignments"][0]["route"] == "keep"


def test_vendor_monthly_capacity_is_a_constraint():
    reg = registry()
    full = vendor(monthlyCapacityKg=500)
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[full])
    assert done["output"]["assignments"][0]["route"] == "keep"
    roomy = vendor(monthlyCapacityKg=600)
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[roomy])
    assert done["output"]["assignments"][0]["route"] == "sell"


def test_carbon_is_held_without_original_checked_factors_or_an_explicit_process():
    """시드 배출계수(환경성적표지)는 원문 경계를 확인하지 못해(metadata_only) 탄소를 보류한다."""
    reg = registry()
    held = run(reg, "cost_carbon", stream=stream(), vendor=vendor(process="재활용"))
    assert held["output"]["carbon"] is None
    assert any(c["key"] == "carbon" and "확인 필요" in c["text"] for c in held["checks"])
    # 업체가 처리 방식을 밝히지 않으면 매입 단가만 보고 재활용으로 추정하지 않는다
    unknown = run(reg, "cost_carbon", stream=stream(), vendor=vendor())
    assert unknown["output"]["carbon"] is None
    assert any("처리 방식을 몰라" in c["text"] for c in unknown["checks"])


@pytest.mark.usefixtures("factor_gate")
def test_carbon_needs_a_factor_for_both_sides():
    """계산식만 본다: 시험 전용 원문 확인 모의 계수(값은 시드와 같다)를 쓴다."""
    reg = with_test_factors(registry())
    recycler = vendor(process="재활용")
    base = run(reg, "cost_carbon", stream=stream(), vendor=recycler)
    assert claim(base, "carbonDifference")["value"] == pytest.approx(
        (0.0186 - 3.412554039) * 0.6, abs=1e-6
    )
    # 열분해는 공식 표에 계수가 없다
    pyro = run(reg, "cost_carbon", stream=stream(), vendor=vendor(process="열분해"))
    assert pyro["output"]["carbon"] is None
    assert any(c["key"] == "carbon" and "확인 필요" in c["text"] for c in pyro["checks"])
    # 현행이 소각도 매립도 재활용도 아니면 현행 쪽 계수가 없다
    reused = stream(currentTreatment={"method": "위탁 보관", "unitCostKrwPerKg": 0})
    other = run(reg, "cost_carbon", stream=reused, vendor=recycler)
    assert other["output"]["carbon"] is None
    landfill = stream(currentTreatment={"method": "매립", "unitCostKrwPerKg": 100})
    buried = run(reg, "cost_carbon", stream=landfill, vendor=recycler)
    assert claim(buried, "carbonDifference")["value"] == pytest.approx(
        (0.0186 - 0.013203539) * 0.6, abs=1e-6
    )


# ── 현행 처리비 질문 ─────────────────────────────────────────────────────
def test_current_cost_answer_replaces_the_assumption_and_stays_with_the_emitter():
    import json

    from app.v3 import service
    from app.v3.agents import questions
    from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, make_app, start

    app = make_app()
    view = answer_all(app, start(app))  # 첫 묶음(수분)에 답하면 현행 처리비를 묻는다
    sid = view["session"]["id"]
    asked = [q for q in view["questions"] if not q["answered"]]
    assert {(q["streamId"], q["field"]) for q in asked} == {
        ("st_film", "currentCost"),
        ("st_purge", "currentCost"),
    }
    assert all(q["unit"] == "원/kg" and q["batch"] == 2 for q in asked)
    for question in asked:
        value = 180 if question["streamId"] == "st_film" else "unknown"
        service.answer_question(app, sid, question["id"], value, HANBIT)
    view = service.run_session(app, sid, HANBIT)
    assert view["session"]["status"] == "awaiting_approval"
    by_stream = {a["streamId"]: a for a in view["result"]["assignments"]}
    film, purge = by_stream["st_film"], by_stream["st_purge"]
    assert film["currentAssumed"] is False and film["monthlyNow"]["value"] == -180 * 300
    # 계산 근거는 답의 고정본(…:snapshot:…)을 잇고, 고정본은 원래 답 근거를 가리킨다
    answered = {
        app.registry.get(e, HANBIT).meta.get("originalId") or e
        for e in film["monthlyNow"]["evidence"]
    }
    assert "human:org_hanbit:st_film-현행처리비" in answered
    # 모름이면 혼합 소각 가정값으로 채우지 않고 현행 처리 단가 확인 필요로 배정을 보류한다
    assert purge["currentAssumed"] is False and purge["vendorOrgId"] is None
    assert purge["status"] == "needs_information" and purge["difference"] is None
    assert [(c["key"], c["text"]) for c in purge["checks"]] == [
        ("skipped", "현행 처리 단가 확인 필요")
    ]
    assert any(c["key"] == "current_method" for c in film["checks"])
    # 업체에게는 현행 처리비도, 그 답의 근거도 보이지 않는다
    seen = json.dumps(service.get_view(app, sid, SAEGIL), ensure_ascii=False)
    assert "현행처리비" not in seen and "현행 처리비" not in seen
    # 업체는 현행 처리비를 물을 수 없다
    assert "currentCost" not in questions.ASK_FIELDS
    assert questions.clean_asks([{"streamId": "st_film", "field": "currentCost"}]) == []


# ── 공정 재투입 (설계안 v3 8.2의 경로 후보) ──────────────────────────────
# 재투입 금액은 신재 단가·재투입량·분쇄비에 더해 회수 수율·신재 대체율·실제 용도·계량/시험 증빙과 수율 손실(잔재)의
# 처리비가 모두 있어야 낸다(미정 비용을 무료로 두지 않는다)
RECOVERY_EVIDENCE = f"measure:{EMITTER}:st_sprue:오염도@2026-11-10"
REUSE = {
    "virginPriceKrwPerKg": 1500,
    "reusableKgPerMonth": 400,
    "regrindCostKrwPerKg": 120,
    "recoveryYieldPct": 90,
    "virginSubstitutionPct": 80,
    "recoveryApplication": "같은 사출 공정의 신재 혼합 투입(가상)",
    "recoveryEvidence": [RECOVERY_EVIDENCE],
    "recoveryResidueCostKrwPerKg": 250,
    "evidence": [],
}
# 400kg을 다시 넣으면 400 × 90% × 80% = 288kg의 신재를 대체한다. 수율 손실 40kg은 kg당 250원에 처리한다
SAVING = 400 * 0.9 * 0.8 * 1500 - 400 * 120
REST = 200 * 250 + 400 * 0.1 * 250


@pytest.mark.usefixtures("grade_gate")
def test_reuse_is_a_candidate_only_for_clean_measured_process_scrap():
    """후보 조건(출처·재질·측정 등급)을 본다. 등급은 grade_gate로 팀 임계값을 받아들인 경로다."""
    reg = registry()
    clean = run(reg, "judge_reuse", stream=stream())
    assert clean["output"]["eligible"] is True and clean["status"] == "needs_information"
    assert any(c["key"] == "reuse_inputs" for c in clean["checks"])  # 금액은 입력이 있어야 낸다
    assert all(c["id"] != "monthlyAfter" for c in clean["claims"])
    for bad, key in (
        (stream(origin="PCR"), "reuse_origin"),
        (stream(material="PP+ABS"), "reuse_material"),
        (stream(contaminants=[{"name": "종이 라벨", "ratio": 0.04}]), "reuse_material"),
        (
            stream(measure={"contaminationPct": 6.0, "moisturePct": 0.3, "evidence": []}),
            "reuse_quality",
        ),
    ):
        result = run(reg, "judge_reuse", stream=bad)
        assert result["output"]["eligible"] is False and result["status"] == "calculated"
        assert next(c for c in result["checks"] if c["key"] == key)["status"] == "unmet"
    # 측정값이 없으면 후보이되 조건부다(사진 추정만으로는 넣어도 된다고 하지 않는다)
    photo = stream(measure=None, photo={"contaminationPct": 1.0, "evidence": []})
    assert run(reg, "judge_reuse", stream=photo)["status"] == "needs_information"


@pytest.mark.usefixtures("grade_gate")
def test_reuse_saving_counts_only_what_can_go_back_in():
    reg = registry()
    result = run(reg, "judge_reuse", stream=stream(reuse=REUSE))
    assert result["status"] == "calculated"
    # 600kg 가운데 400kg만 다시 넣는다. 남는 200kg은 담당자가 답한 현행 처리비(kg당 250원)대로 처리한다
    assert claim(result, "reuseKg")["value"] == 400
    assert claim(result, "monthlyRevenue")["value"] == pytest.approx(SAVING)
    assert claim(result, "monthlyAfter")["value"] == pytest.approx(SAVING - REST)
    assert claim(result, "difference")["value"] == pytest.approx(SAVING - REST + 600 * 250)
    # 수율·대체율·증빙이나 잔재 처리비가 없으면 금액을 내지 않는다(미정 비용을 무료로 두지 않는다)
    for missing in (
        "recoveryYieldPct",
        "virginSubstitutionPct",
        "recoveryEvidence",
        "recoveryResidueCostKrwPerKg",
    ):
        partial = {k: v for k, v in REUSE.items() if k != missing}
        held = run(reg, "judge_reuse", stream=stream(reuse=partial))
        assert held["status"] == "needs_information", missing
        assert all(c["id"] != "monthlyAfter" for c in held["claims"])
        assert any(c["key"] == "reuse_inputs" for c in held["checks"])


@pytest.mark.usefixtures("grade_gate")
def test_optimizer_picks_reuse_when_it_beats_selling_and_only_when_eligible():
    reg = registry()
    done = run(reg, "optimize_assignment", streams=[stream(reuse=REUSE)], vendors=[vendor()])
    item = done["output"]["assignments"][0]
    assert item["route"] == "reuse" and item["vendorOrgId"] is None
    assert claim(done, item["monthlyAfter"])["value"] == pytest.approx(SAVING - REST)
    assert claim(done, item["revenue"])["value"] == pytest.approx(SAVING)
    # 재투입 절감이 작으면 파는 쪽을 고른다
    weak = dict(REUSE, virginPriceKrwPerKg=300, reusableKgPerMonth=100)
    done = run(reg, "optimize_assignment", streams=[stream(reuse=weak)], vendors=[vendor()])
    assert done["output"]["assignments"][0]["route"] == "sell"
    # 혼입물이 있으면 값이 있어도 재투입을 고르지 않는다
    dirty = stream(reuse=REUSE, contaminants=[{"name": "금속 인서트", "ratio": 0.02}])
    done = run(reg, "optimize_assignment", streams=[dirty], vendors=[])
    assert done["output"]["assignments"][0]["route"] == "keep"


@pytest.mark.usefixtures("grade_gate")
def test_demo_result_lists_reuse_candidates_and_hides_them_from_vendors():
    from app.v3 import service
    from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app, to_approval

    app = make_app()
    view = to_approval(app)
    by_stream = {a["streamId"]: a for a in view["result"]["assignments"]}
    sprue = by_stream["st_sprue"]["reuse"]
    assert sprue["eligible"] is True and sprue["status"] == "needs_information"
    assert sprue["monthlyAfter"] is None and "신재 단가" in sprue["reason"]
    assert by_stream["st_purge"]["reuse"]["eligible"] is False  # 재질이 섞였다
    assert by_stream["st_film"]["reuse"]["eligible"] is False  # 쓰고 난 뒤 나온 것
    vendor_view = service.get_view(app, view["session"]["id"], SAEGIL)
    assert all(a["reuse"] is None for a in vendor_view["result"]["assignments"])
    # 값을 넣으면 재투입이 계산되고, 다시 찾으면 스프루는 재투입으로 간다
    saved = service.update_stream(app, HANBIT, "st_sprue", {"reuse": REUSE}, HANBIT)
    assert saved["reuse"]["virginPriceKrwPerKg"] == 1500
    # 회사가 넣은 세 값의 근거와 고른 계량·시험 증빙
    assert len(saved["reuse"]["evidence"]) == 4 and RECOVERY_EVIDENCE in saved["reuse"]["evidence"]
    fresh = to_approval(app)
    again = {a["streamId"]: a for a in fresh["result"]["assignments"]}
    assert again["st_sprue"]["route"] == "reuse" and again["st_sprue"]["vendorOrgId"] is None
    assert again["st_sprue"]["reuse"]["status"] == "calculated"
    assert again["st_film"]["vendorOrgId"] == SAEGIL  # 필름은 그대로 새길자원으로 간다


def test_cost_inputs_are_validated_and_never_reach_a_vendor():
    import json

    from app.v3 import service
    from tests.v3.runtime_fixtures import HANBIT, MISO, make_app, to_approval

    app = make_app()
    with pytest.raises(service.ServiceError):
        service.update_stream(app, HANBIT, "st_defect", {"pretreatHoursPerMonth": -1}, HANBIT)
    with pytest.raises(service.ServiceError):
        service.update_stream(
            app, HANBIT, "st_sprue", {"reuse": {"virginPriceKrwPerKg": 0}}, HANBIT
        )
    service.update_stream(
        app, HANBIT, "st_defect", {"pretreatHoursPerMonth": 4, "laborCostKrwPerHour": 12000}, HANBIT
    )
    view = to_approval(app)
    defect = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_defect")
    assert defect["vendorOrgId"] == MISO and defect["labor"]["value"] == 48000
    assert defect["monthlyAfter"]["value"] == 200 * 400 - 48000
    mine = next(s for s in view["streams"] if s["id"] == "st_defect")
    assert mine["costInputs"]["pretreatHoursPerMonth"] == 4
    seen = json.dumps(service.get_view(app, view["session"]["id"], MISO), ensure_ascii=False)
    # 문의 글의 "전처리 조건" 같은 낱말은 있어도 되지만, 작업 시간과 인건비는 없어야 한다
    assert "costInputs" not in seen and "인건비" not in seen and "전처리 작업" not in seen
    assert "48000" not in seen and "12000" not in seen
    assert all("pretreatHoursPerMonth" not in s for s in service.list_streams(app, HANBIT, MISO))


# ── 도로 거리 제공자의 값은 가정값이 아니라 근거가 된다 ──────────────────
def test_road_distance_from_a_provider_is_registered_as_evidence():
    from app.v3.judge.distance import KakaoDistance
    from app.v3.judge.toolbox import call_tool
    from tests.v3.judge_fixtures import EMITTER, ORIGIN, TODAY, VENDOR, origin_of

    def transport(method, url, headers, body):
        import json

        body = {"routes": [{"result_code": 0, "summary": {"distance": 12345}}]}
        return 200, json.dumps(body).encode("utf-8")

    provider = KakaoDistance("test-key", transport=transport)
    reg = registry()
    # 업체가 운반 조건을 밝히지 않으면 운반비를 거리로 어림하지 않는다(cost_carbon에는 거리 근거가 없다).
    # 배출 기업 쪽 거리(업체 찾기)는 도로 거리 제공자의 값을 근거로 남긴다
    found_vendors = call_tool(
        "find_vendors",
        {"stream": stream(), "vendors": [vendor()], "origin": ORIGIN},
        registry=reg,
        org_id=EMITTER,
        today=TODAY,
        distance_provider=provider,
    )
    km = claim(found_vendors, "distanceKm0")
    assert km["value"] == pytest.approx(12.345)
    road = [o for o in (origin_of(reg, e) for e in km["evidence"]) if o.startswith("ref:도로거리:")]
    assert len(road) == 2  # 가는 길과 오는 길
    found = reg.get(road[0], EMITTER)
    assert found.kind == "ref" and found.unit == "km" and found.value == pytest.approx(12.345)
    assert "카카오" in found.label and not any(
        origin_of(reg, e).startswith("assumption:distance") for e in km["evidence"]
    )
    # 같은 구간을 업체 쪽 도구가 다시 물어도(왕복 24.69km) 배출 기업은 계속 볼 수 있다
    stop = {"id": "n", "location": ORIGIN, "kg": 600, "m3": 2.4}
    extra = call_tool(
        "route_extra_cost",
        {"vendor": vendor(), "stops": [], "newStop": stop},
        registry=reg,
        org_id=VENDOR,
        today=TODAY,
        distance_provider=provider,
    )
    trip = claim(extra, "newKm")
    assert trip["value"] == pytest.approx(24.69)
    assert set(road) <= {origin_of(reg, e, VENDOR) for e in trip["evidence"]}
    assert reg.get(road[0], EMITTER).value == pytest.approx(12.345)
    assert reg.get(road[0], VENDOR).value == pytest.approx(12.345)
