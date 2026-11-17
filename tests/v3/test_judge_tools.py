import pytest

from app.v3.evidence.model import Evidence
from app.v3.judge import basis, defaults
from app.v3.judge.economics import plan_trips
from app.v3.judge.toolbox import TOOLS, call_tool, tool_schemas
from tests.v3.judge_fixtures import (
    EMITTER,
    ORIGIN,
    OTHER,
    TODAY,
    VENDOR,
    VENDOR2,
    assert_grounded,
    claim,
    film_vendor,
    registry,
    run,
    stream,
    vendor,
)


def test_defaults_are_assumptions_and_idempotent():
    reg = registry()
    ids_ = defaults.register_defaults(reg, TODAY)
    assert len(ids_) == len(defaults.load()["items"]) and all(
        i.startswith("assumption:") for i in ids_
    )
    assert all(reg.get(i).version == 1 and "가정값" in reg.get(i).excerpt for i in ids_)
    assert basis.resolve(reg, EMITTER, "kau.price") is None


def test_basis_prefers_ref_when_present():
    reg = registry()
    ref = Evidence(
        id="ref:가상조사:혼합소각-폐플라스틱@2026-08",
        kind="ref",
        value=300,
        unit="원/kg",
        excerpt="가상",
        source="가상",
        as_of="2026-08-31",
    )
    reg.put(ref)
    basis.prefer("current.cost.폐플라스틱", ref.id)
    assert basis.resolve(reg, EMITTER, "current.cost.폐플라스틱").id == ref.id
    basis.reset()
    assert basis.resolve(reg, EMITTER, "current.cost.폐플라스틱").kind == "assumption"


def test_toolbox_has_ten_tools_and_validates_input():
    assert len(TOOLS) == 10 and {t["function"]["name"] for t in tool_schemas()} == set(TOOLS)
    assert sorted({s.stage for s in TOOLS.values()}) == [1, 2, 3, 4, 5, 6]
    reg = registry()
    for bad in ({}, {"stream": "x"}, {"stream": {}, "nope": 1}):
        with pytest.raises(ValueError):
            call_tool("identify_stream", bad, registry=reg, org_id=EMITTER, today=TODAY)
    with pytest.raises(ValueError):
        call_tool("nope", {}, registry=reg, org_id=EMITTER, today=TODAY)


@pytest.mark.parametrize(
    "name,args",
    [
        ("identify_stream", {"stream": stream()}),
        ("judge_route", {"stream": stream(), "vendor": vendor()}),
        ("check_loading", {"streams": [stream()], "vendor": vendor()}),
        ("match_permit", {"stream": stream(), "vendor": vendor()}),
        (
            "find_vendors",
            {"stream": stream(), "vendors": [vendor(), film_vendor()], "origin": ORIGIN},
        ),
        ("cost_carbon", {"stream": stream(), "vendor": vendor(), "origin": ORIGIN}),
        ("optimize_assignment", {"streams": [stream()], "vendors": [vendor()]}),
    ],
)
def test_deterministic_and_grounded(name, args):
    reg = registry()
    first, second = run(reg, name, **args), run(reg, name, **args)
    assert first == second and first["evidence"] == second["evidence"]
    assert_grounded(reg, first)
    other = run(registry(), name, **args)
    assert other == first
    assert all(reg.get(e).version == 1 for e in first["evidence"])


def test_identify_measured_beats_photo_and_flags_disagreement():
    reg = registry()
    r = run(reg, "identify_stream", stream=stream())
    assert (
        r["status"] == "calculated"
        and r["output"]["grade"] == "A"
        and r["output"]["basis"] == "measured"
    )
    r = run(
        reg,
        "identify_stream",
        stream=stream(photo={"contaminationPct": 8.0}, answers={"moisturePct": 0.5}),
    )
    assert (
        r["output"]["grade"] == "A"
        and r["output"]["photoGrade"] == "C"
        and r["status"] == "conditional"
    )
    assert any(c["key"] == "grade_disagreement" and c["ask"] for c in r["checks"])
    r = run(
        reg,
        "identify_stream",
        stream=stream(measure=None, photo={"contaminationPct": 8.0}),
        moisturePct=2.0,
    )
    assert r["output"]["grade"] == "C" and r["output"]["basis"] == "photo_estimate"
    assert r["output"]["gradeEvidence"] == ["assumption:등급기준@v1"]
    assert reg.get("assumption:등급기준@v1").kind == "assumption"
    r = run(reg, "identify_stream", stream=stream(measure=None, photo={"contaminationPct": 8.0}))
    assert r["status"] == "needs_information" and r["output"]["grade"] is None
    assert any(c["key"] == "moisture" and c["ask"] for c in r["checks"])


def test_judge_route_three_states_and_sensitivity():
    reg = registry()
    r = run(reg, "judge_route", stream=stream(), vendor=vendor())
    assert (
        r["status"] == "calculated"
        and r["output"]["route"] == "sell"
        and r["output"]["current"]["assumed"]
    )
    assert (
        claim(r, "monthlyAfter")["value"] == 192000 and claim(r, "monthlyNow")["value"] == -150000
    )
    assert claim(r, "difference")["value"] == 342000
    assert claim(r, "sensitivityA")["value"] == 192000 and r["output"]["current"]["sensitivity"]
    assert f"doc:{VENDOR}:매입기준표:PP-A" in claim(r, "unitPrice")["evidence"]
    given = stream(
        currentTreatment={"method": "위탁 소각", "unitCostKrwPerKg": 300, "evidence": []}
    )
    r = run(reg, "judge_route", stream=given, vendor=vendor())
    assert not r["output"]["current"]["assumed"] and claim(r, "monthlyNow")["value"] == -180000
    assert not any(c["id"].startswith("sensitivity") for c in r["claims"])
    r = run(reg, "judge_route", stream=stream(form=None), vendor=vendor())
    assert r["status"] == "conditional" and any(
        c["key"] == "form" and c["ask"] for c in r["checks"]
    )
    r = run(reg, "judge_route", stream=stream(monthlyKg=None), vendor=vendor())
    assert r["status"] == "needs_information" and r["claims"] == []
    dirty = stream(measure={"contaminationPct": 9.0, "moisturePct": 0.3})
    r = run(reg, "judge_route", stream=dirty, vendor=vendor())
    assert r["output"]["acceptable"] is False and r["output"]["route"] == "keep"
    assert not any(c["id"] == "monthlyAfter" for c in r["claims"])
    r = run(reg, "judge_route", stream=stream(), vendor=film_vendor())
    assert r["output"]["acceptable"] is False and any(
        c["key"] == "permit" and c["status"] == "unmet" for c in r["checks"]
    )


def test_loading_is_origin_agnostic_and_combines_streams():
    reg = registry()
    small = [stream(id="st_a", monthlyKg=300), stream(id="st_b", monthlyKg=300, origin="PCR")]
    r = run(reg, "check_loading", streams=small, vendor=vendor())
    assert r["status"] == "calculated" and claim(r, "totalKg")["value"] == 600
    assert claim(r, "totalM3")["value"] == 2.4 and claim(r, "perPickupKg")["value"] == 300
    for origin in ("PIR", "PCR", None):
        one = run(
            reg, "check_loading", streams=[stream(monthlyKg=300, origin=origin)], vendor=vendor()
        )
        status = [(c["key"], c["status"]) for c in one["checks"]]
        assert ("min_volume", "conditional") in status and one["status"] == "conditional"
    light = run(
        reg,
        "check_loading",
        streams=[stream(monthlyKg=3000, bulkDensityKgPerM3=30)],
        vendor=vendor(),
    )
    assert any(c["key"] == "loading" and c["status"] == "conditional" for c in light["checks"])
    unknown = run(reg, "check_loading", streams=[stream(bulkDensityKgPerM3=None)], vendor=vendor())
    assert any(c["key"] == "loading_volume" for c in unknown["checks"])
    with pytest.raises(ValueError):
        run(reg, "check_loading", streams=[stream(), stream(id="x", orgId=OTHER)], vendor=vendor())


def test_regulation_never_invents_clauses():
    reg = registry()
    r = run(reg, "search_clauses", query="위탁 재활용")
    assert r["status"] == "needs_information" and r["claims"] == []
    r = run(reg, "check_requirements", stream=stream(), route="sell")
    assert r["status"] == "needs_information" and r["claims"] == []
    text = "가상 조항이다. 폐플라스틱을 위탁하려는 자는 수탁자의 허가 여부를 확인하여야 한다. 그 밖의 사항은 따로 정한다."
    reg.put(
        Evidence(
            id="law:가상법:제1조@2026-01-01",
            kind="law",
            excerpt=text,
            source="테스트용 가상 법령",
            as_of="2026-01-01",
            label="가상법 제1조",
        )
    )
    r = run(reg, "search_clauses", query="위탁 허가")
    assert (
        r["status"] == "calculated"
        and r["claims"][0]["value"] in text
        and "위탁" in r["claims"][0]["value"]
    )
    assert_grounded(reg, r)
    r = run(reg, "check_requirements", stream=stream(), route="sell")
    assert r["status"] == "conditional" and all(c["status"] == "conditional" for c in r["checks"])
    assert all(c["value"] in text for c in r["claims"])
    assert any(i["claim"] is None for i in r["output"]["items"])  # 못 찾은 주제는 비워 둔다


def test_permit_mismatch_blocks_thread_with_evidence():
    reg = registry()
    r = run(reg, "match_permit", stream=stream(), vendor=film_vendor())
    assert r["output"]["matched"] is False and r["output"]["openThread"] is False
    assert r["output"]["permitEvidence"] == [f"doc:{VENDOR2}:허가증:품목"]
    r = run(reg, "match_permit", stream=stream(), vendor=vendor(permits=[]))
    assert r["status"] == "needs_information" and r["output"]["openThread"] is False
    r = run(reg, "find_vendors", stream=stream(), vendors=[film_vendor(), vendor()], origin=ORIGIN)
    assert [c["vendorOrgId"] for c in r["output"]["candidates"]] == [VENDOR]
    assert r["output"]["candidates"][0]["distanceAssumed"] is True
    ex = r["output"]["excluded"][0]
    assert ex["vendorOrgId"] == VENDOR2 and ex["evidence"] == [f"doc:{VENDOR2}:허가증:품목"]
    road = call_tool(
        "find_vendors",
        {"stream": stream(), "vendors": [vendor()], "origin": ORIGIN},
        registry=reg,
        org_id=EMITTER,
        today=TODAY,
        distance_provider=lambda pts: [[0, 7.5], [7.5, 0]],
    )
    assert (
        road["output"]["candidates"][0]["distanceAssumed"] is False
        and road["claims"][0]["value"] == 7.5
    )
    far = run(reg, "find_vendors", stream=stream(), vendors=[vendor()], origin=ORIGIN, maxKm=0.1)
    assert (
        far["output"]["candidates"] == []
        and far["output"]["excluded"][0]["reason"] == "탐색 거리 밖입니다."
    )


def test_cost_carbon_marks_missing_basis_as_needs_check():
    reg = registry()
    r = run(reg, "cost_carbon", stream=stream(), vendor=vendor(), origin=ORIGIN)
    assert r["status"] == "conditional" and r["output"]["transportAssumed"] is True
    assert claim(r, "monthlyAfter")["value"] == 192000  # 운송비는 기본으로 업체 부담
    # 환경성적표지 평가계수: (재활용 공정 − 혼합 소각) × 월 발생량. 신재 대체 효과는 넣지 않는다
    assert claim(r, "carbonDifference")["value"] == pytest.approx(
        (0.0186 - 3.412554039) * 600 / 1000, abs=1e-6
    )
    assert all(
        e.startswith(("calc:", "ref:환경성적표지:"))
        for e in claim(r, "carbonDifference")["evidence"]
    )
    assert r["output"]["kau"] is None and any(c["key"] == "kau" for c in r["checks"])
    paid = run(
        reg,
        "cost_carbon",
        stream=stream(),
        vendor=vendor(),
        origin=ORIGIN,
        transportPaidBy="emitter",
    )
    assert claim(paid, "monthlyAfter")["value"] == pytest.approx(
        192000 - claim(paid, "monthlyTransport")["value"]
    )
    wood = run(
        reg,
        "cost_carbon",
        stream=stream(wasteItem="폐목재"),
        vendor=vendor(permits=[{"wasteItem": "폐목재", "materials": [], "evidence": []}]),
        origin=ORIGIN,
    )
    assert wood["output"]["carbon"] is None and any(c["key"] == "carbon" for c in wood["checks"])


def test_route_extra_cost_respects_weight_and_volume():
    reg = registry()
    v = vendor()
    stops = [{"id": "a", "location": [128.92, 35.12], "kg": 1000, "m3": 4}]
    new = {"id": "n", "location": ORIGIN, "kg": 600, "m3": 2.4}
    r = run(reg, "route_extra_cost", org=VENDOR, vendor=v, stops=stops, newStop=new)
    assert_grounded(reg, r, VENDOR)
    assert claim(r, "extraKm")["value"] > 0 and claim(r, "trips")["value"] == 1
    assert claim(r, "extraCost")["value"] == pytest.approx(
        claim(r, "extraKm")["value"] * 1200, rel=1e-6
    )
    bulky = run(
        reg, "route_extra_cost", org=VENDOR, vendor=v, stops=stops, newStop={**new, "m3": 18}
    )
    assert claim(bulky, "trips")["value"] == 2 and bulky["output"]["extraTrips"] == 1
    too_big = run(
        reg, "route_extra_cost", org=VENDOR, vendor=v, stops=stops, newStop={**new, "kg": 9000}
    )
    assert too_big["output"]["feasible"] is False
    m = [[0, 1, 1, 5], [1, 0, 1, 5], [1, 1, 0, 5], [5, 5, 5, 0]]
    assert plan_trips(m, [(1, 0), (1, 0), (1, 0)], 2, None) == (13.0, 2)
    assert plan_trips(m, [(1, 0), (1, 0), (1, 0)], 3, None) == (12.0, 1)
    assert plan_trips(m, [(1, 2), (1, 2), (1, 2)], 3, 2) == (14.0, 3)


def test_optimize_assignment_constraints_and_tiebreak():
    reg = registry()
    a, b = stream(id="st_a", monthlyKg=300), stream(id="st_b", monthlyKg=300)
    r = run(reg, "optimize_assignment", streams=[a, b], vendors=[vendor()])
    assert [x["vendorOrgId"] for x in r["output"]["assignments"]] == [
        VENDOR,
        VENDOR,
    ]  # 합쳐야 최소 물량을 넘는다
    assert claim(r, "totalAfter")["value"] == 192000 and claim(r, "totalNow")["value"] == -150000
    alone = run(reg, "optimize_assignment", streams=[a], vendors=[vendor()])
    assert (
        alone["output"]["assignments"][0]["route"] == "keep"
        and claim(alone, "totalDifference")["value"] == 0
    )
    twin = vendor(orgId="org_aaa")
    tie = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor(), twin])
    assert tie["output"]["assignments"][0]["vendorOrgId"] == "org_aaa"
    assert tie == run(reg, "optimize_assignment", streams=[stream()], vendors=[twin, vendor()])
    none = run(reg, "optimize_assignment", streams=[stream()], vendors=[film_vendor()])
    assert none["output"]["assignments"][0]["vendorOrgId"] is None
    skipped = run(
        reg,
        "optimize_assignment",
        streams=[stream(), stream(id="st_x", monthlyKg=None)],
        vendors=[vendor()],
    )
    assert (
        skipped["status"] == "conditional" and skipped["output"]["skipped"][0]["streamId"] == "st_x"
    )


def test_carbon_weight_only_changes_objective_when_positive():
    reg = registry()
    payer = vendor(
        process="재활용",  # 처리비를 받는 업체는 처리 방식을 밝혀야 탄소를 계산한다
        criteria=[
            {
                "id": "pc_fee",
                "material": "PP",
                "unitPriceKrwPerKg": -260,
                "minMonthlyKg": 0,
                "evidence": [],
            }
        ],
    )
    off = run(reg, "optimize_assignment", streams=[stream()], vendors=[payer])
    assert (
        off["output"]["carbonInObjective"] is False
        and off["output"]["assignments"][0]["route"] == "keep"
    )
    on = run(reg, "optimize_assignment", streams=[stream()], vendors=[payer], carbonWeight=100000)
    assert (
        on["output"]["carbonInObjective"] is True
        and on["output"]["assignments"][0]["route"] == "treat"
    )


def test_org_boundary():
    reg = registry()
    leak = stream(
        currentTreatment={
            "method": "위탁",
            "unitCostKrwPerKg": 400,
            "evidence": [f"doc:{OTHER}:위탁계약서:단가"],
        }
    )
    r = run(reg, "judge_route", stream=leak, vendor=vendor())
    assert all(OTHER not in e for c in r["claims"] for e in c["evidence"])
    assert all(
        OTHER not in e for e in reg.get(claim(r, "monthlyNow")["evidence"][0]).meta["inputEvidence"]
    )
    calc = reg.get(r["evidence"][0])
    assert set(calc.access) == {EMITTER, VENDOR} and not calc.visible_to(OTHER)
    with pytest.raises(ValueError):
        run(reg, "optimize_assignment", streams=[stream(orgId=OTHER)], vendors=[vendor()])
    with pytest.raises(ValueError):
        run(reg, "check_loading", streams=[stream(orgId=OTHER)], vendor=vendor())
    hidden = run(reg, "cost_carbon", stream=stream(), vendor=vendor(), origin=ORIGIN)
    assert f"doc:{VENDOR}:노선:차량" not in {
        e for c in hidden["claims"] for e in c["evidence"]
    }  # 업체 내부 문서


def test_kau_reference_feeds_cost_carbon_when_seeded():
    from app.v3.evidence.seeds import load_seed_dir

    reg = registry()
    assert basis.resolve(reg, EMITTER, "kau.price") is None
    load_seed_dir(reg)
    kau = basis.resolve(reg, EMITTER, "kau.price")
    assert kau.kind == "ref" and kau.unit == "원/tCO2e" and kau.meta["per"] == "tCO2e"
    r = run(reg, "cost_carbon", stream=stream(), vendor=vendor(), origin=ORIGIN)
    value = claim(r, "kauValue")
    assert r["output"]["kau"] == "kauValue" and value["unit"] == "원/월"
    assert value["value"] == pytest.approx(-claim(r, "carbonDifference")["value"] * kau.value)
    assert kau.id in value["evidence"] and not any(c["key"] == "kau" for c in r["checks"])


def test_grade_thresholds_have_one_source():
    from app.v3.intake.grade_model import grade_by_thresholds

    assert not [i for i in defaults.load()["items"] if i["key"].startswith("grade.")]
    assert basis.resolve(registry(), EMITTER, "grade.A.maxContaminationPct") is None
    reg = registry()
    for material, cont, moist in (("PP", 2.5, 0.8), ("LDPE", 3.0, 2.0), ("PP+ABS", 40.0, 1.0)):
        r = run(
            reg,
            "identify_stream",
            stream=stream(material=material, measure=None, photo={"contaminationPct": cont}),
            moisturePct=moist,
        )
        assert r["output"]["grade"] == grade_by_thresholds(material, cont, moist)["grade"]


def test_every_tool_output_passes_the_validator():
    """도구 10개의 claims와 ask 문장이 근거 검증기를 그대로 통과한다."""
    from app.v3.evidence.seeds import load_seed_dir

    reg = registry()
    load_seed_dir(reg)
    mixed = stream(
        id="st_defect",
        measure=None,
        photo={"contaminationPct": 6.0},
        monthlyKgBasis="estimated",
        contaminants=[{"name": "종이 라벨", "ratio": 0.04}],
    )
    stops = [{"id": "a", "location": [128.92, 35.12], "kg": 1000, "m3": 4}]
    new = {"id": "n", "location": ORIGIN, "kg": 600, "m3": 2.4}
    calls = [
        (EMITTER, "identify_stream", {"stream": mixed}),
        (EMITTER, "identify_stream", {"stream": mixed, "moisturePct": 1.5}),
        (EMITTER, "judge_route", {"stream": stream()}),
        (EMITTER, "judge_route", {"stream": mixed, "vendor": vendor()}),
        (EMITTER, "judge_route", {"stream": stream(), "vendor": film_vendor()}),
        (EMITTER, "check_loading", {"streams": [stream(), mixed], "vendor": vendor()}),
        (EMITTER, "search_clauses", {"query": "폐기물 위탁 재활용"}),
        (EMITTER, "check_requirements", {"stream": stream(), "route": "sell"}),
        (EMITTER, "match_permit", {"stream": stream(), "vendor": film_vendor()}),
        (EMITTER, "find_vendors", {"stream": stream(), "vendors": [vendor()], "origin": ORIGIN}),
        (EMITTER, "cost_carbon", {"stream": mixed, "vendor": vendor(), "origin": ORIGIN}),
        (VENDOR, "route_extra_cost", {"vendor": vendor(), "stops": stops, "newStop": new}),
        (EMITTER, "optimize_assignment", {"streams": [stream(), mixed], "vendors": [vendor()]}),
    ]
    assert {name for _, name, _ in calls} == set(TOOLS)
    for org, name, args in calls:
        assert_grounded(reg, run(reg, name, org=org, **args), org)


def test_reference_prices_are_shown_but_never_used_in_profit():
    from app.v3.evidence.seeds import load_seed_dir

    reg = registry()
    assert basis.reference_prices(reg, EMITTER) == []  # 근거 시드가 없으면 비어 있다
    load_seed_dir(reg)
    prices = basis.reference_prices(reg, EMITTER, ["PP"])
    assert [p["form"] for p in prices] == ["압축", "펠렛", "플레이크"]
    assert all(p["evidence"].kind == "ref" and p["evidence"].unit == "원/kg" for p in prices)
    assert basis.resolve(reg, EMITTER, "reference.price.PP.펠렛").id.startswith(
        "ref:가격조사:PP-펠렛"
    )
    assert len(basis.reference_prices(reg, EMITTER)) == 6
    # 손익을 내는 도구의 계산 사슬에 참고 시세가 들어가지 않는다
    for name, args in (
        ("judge_route", {"stream": stream(), "vendor": vendor()}),
        ("cost_carbon", {"stream": stream(), "vendor": vendor(), "origin": ORIGIN}),
        ("optimize_assignment", {"streams": [stream()], "vendors": [vendor()]}),
    ):
        r = run(reg, name, **args)
        used = {e for c in r["claims"] for e in c["evidence"]}
        used |= {i for e in r["evidence"] for i in reg.get(e).meta["inputEvidence"]}
        assert not [e for e in used if e.startswith("ref:가격조사")]


def test_current_cost_stays_private_and_exclusions_apply():
    reg = registry()
    mine = stream(
        currentTreatment={
            "method": "혼합 소각",
            "unitCostKrwPerKg": 250,
            "evidence": [],
        },
        contaminants=[{"name": "종이 라벨", "ratio": 0.04}],
    )
    r = run(reg, "judge_route", stream=mine, vendor=vendor())
    for key in ("currentUnitCost", "monthlyNow", "difference"):
        assert reg.get(claim(r, key)["evidence"][0]).access == (EMITTER,)
    assert reg.get(claim(r, "unitPrice")["evidence"][0]).access == (EMITTER, VENDOR)
    assert any(c["key"] == "mixture" and "종이 라벨" in c["text"] for c in r["checks"])

    base = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()])
    assert base["output"]["assignments"][0]["vendorOrgId"] == VENDOR
    cut = run(
        reg,
        "optimize_assignment",
        streams=[stream()],
        vendors=[vendor()],
        exclude=[{"streamId": "st_sprue", "vendorOrgId": VENDOR}],
    )
    assert cut["output"]["assignments"][0]["route"] == "keep"
    assert cut["evidence"] != base["evidence"]
