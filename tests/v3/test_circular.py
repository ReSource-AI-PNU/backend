"""자원순환 효과(judge/circular.py): 세 지표, 적용 조건, 산정 보류, 기준선, 시나리오 부분합, 묶음 없는 배정.

계수는 원문 표에서 옮긴 값이다(근거 시드 circular.json).
- EU JRC 2021 Annex 5 (B) 소각 대비·(C) 매립 대비 순효과, Table 2 수율·대체율
- US EPA WARM v16 Exhibit 7-12 순에너지(million Btu/short ton)
"""

import pytest

from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from app.v3.evidence.units import convert
from app.v3.judge import circular
from app.v3.judge.common import Recorder
from tests.v3.judge_fixtures import (
    EMITTER,
    TODAY,
    VENDOR,
    claim,
    doc,
    registry,
    run,
    stream,
    vendor,
)


@pytest.fixture
def reg():
    found = registry()
    load_seed_file(found, SEED_DIR / "circular.json")
    return found


def rec(reg):
    return Recorder("optimize_assignment", reg, EMITTER, TODAY, {"case": "circular"})


def film(**over):
    return stream(
        id="st_film",
        name="포장 필름",
        wasteItem="포장재",
        material="LDPE",
        origin="PCR",
        form="필름",
        monthlyKg=300,
        **over,
    )


def test_unit_conversion_matches_the_warm_table():
    # (소각 −18.79) − (재활용 −44.50) = 25.71 million Btu/short ton → 29.90 GJ/t
    assert convert(-18.79 - -44.50, "MMBtu/short ton", "GJ/t") == pytest.approx(29.90, abs=0.005)
    assert convert(-18.83 - -44.78, "MMBtu/short ton", "GJ/t") == pytest.approx(30.18, abs=0.005)
    assert convert(-9.99 - -28.59, "MMBtu/short ton", "GJ/t") == pytest.approx(21.63, abs=0.005)
    assert convert(-6.63 - -15.10, "MMBtu/short ton", "GJ/t") == pytest.approx(9.85, abs=0.005)
    assert convert(-1796, "kgCO2e/t", "tCO2e/t") == pytest.approx(-1.796)


def test_pcr_film_uses_literature_without_double_counting(reg):
    found = circular.stream_effect(rec(reg), film(), 300, "incineration", False, "recycle")
    # 감축량 = 0.3t × 1.381(JRC LDPE 소각 대비). 수율·대체율을 다시 곱하지 않는다
    assert found["ghg"]["status"] == "estimated" and found["ghg"]["method"] == "literature"
    assert found["ghg"]["value"] == pytest.approx(0.3 * 1.381)
    assert found["ghg"]["basis"] == ["ref:JRC2021:LDPE-재활용-소각대비@2021"]
    # 신재 대체량 = 투입량 × 선별 수율 × 재활용 수율 × 신재 대체율(같은 연구 Table 2)
    assert found["virgin"]["value"] == pytest.approx(300 * 0.73 * 0.84 * 0.75)
    assert found["virginMaterial"] == "LDPE"
    # LDPE 재활용 에너지는 원문에 없다(NA). PP 값으로 채우지 않는다
    assert found["energy"]["status"] == "hold" and "NA" in found["energy"]["reason"]
    assert any("소각장이 전기·열을 회수" in line for line in found["unconfirmed"])


def test_pp_post_consumer_energy_and_landfill_baseline(reg):
    pp = stream(origin="PCR", form="압축")
    found = circular.stream_effect(rec(reg), pp, 1000, "incineration", False, "recycle")
    assert found["energy"]["value"] == pytest.approx(29.90, abs=0.005)
    assert found["ghg"]["value"] == pytest.approx(1.796)
    # 지금 매립하면 (C) 매립 대비 순효과를 쓴다
    landfilled = circular.stream_effect(rec(reg), pp, 1000, "landfill", False, "recycle")
    assert landfilled["ghg"]["value"] == pytest.approx(0.620)
    assert landfilled["ghg"]["basis"] == ["ref:JRC2021:PP-재활용-매립대비@2021"]
    assert landfilled["energy"]["value"] == pytest.approx(
        convert(0.27 - -44.50, "MMBtu/short ton", "GJ/t")
    )


def test_industrial_scrap_mixture_and_pyrolysis_are_held_not_zero(reg):
    r = rec(reg)
    scrap = circular.stream_effect(r, stream(), 600, "incineration", False, "recycle")
    mixed = circular.stream_effect(
        r, stream(material="PP+ABS", origin="PCR"), 250, "incineration", False, "recycle"
    )
    pyro = circular.stream_effect(
        r, stream(material="PP+ABS"), 250, "incineration", False, "pyrolysis"
    )
    for found, word in ((scrap, "PIR"), (mixed, "섞인"), (pyro, "열분해")):
        for metric in circular.METRICS:
            assert found[metric]["status"] == "hold", (word, metric)
            assert found[metric]["value"] is None
        assert word in " ".join(found[m]["reason"] for m in circular.METRICS)
    assert "열분해유의 용도(화학 원료인지 연료인지)." in pyro["unconfirmed"]


def test_already_recycled_only_changes_the_buyer(reg):
    found = circular.stream_effect(rec(reg), film(), 300, "recycle", False, "recycle")
    for metric in circular.METRICS:
        assert found[metric]["status"] == "zero" and found[metric]["value"] == 0
    assert "거래처만 바뀝니다" in found["ghg"]["reason"]


def test_landfill_comparison_has_no_recovery(reg):
    r = rec(reg)
    found = circular.stream_effect(r, film(), 300, "incineration", False, "landfill")
    # 소각 → 매립 = (C) 순효과 − (B) 순효과 = −215 − (−1381) kg/t
    assert found["ghg"]["value"] == pytest.approx(0.3 * 1.166)
    assert found["energy"]["value"] < 0  # 소각으로 얻던 전기를 잃는다
    assert found["virgin"]["status"] == "zero"
    # 지금 재활용하던 물량을 매립하면 배출이 늘고 지금의 대체를 잃는다
    lost = circular.stream_effect(r, film(), 300, "recycle", False, "landfill")
    assert lost["ghg"]["value"] == pytest.approx(-0.3 * 0.215)
    assert lost["virgin"]["value"] == pytest.approx(-300 * 0.73 * 0.84 * 0.75)


def test_pet_and_pe_need_the_form_or_grade(reg):
    r = rec(reg)
    assert circular.material_key(stream(material="PET"))[0] is None
    assert circular.material_key(stream(material="PE"))[0] is None
    bottle = circular.stream_effect(
        r, stream(material="PET", form="병", origin="PCR"), 1000, "incineration", False, "recycle"
    )
    assert bottle["ghg"]["value"] == pytest.approx(3.147)
    assert bottle["energy"]["value"] == pytest.approx(
        21.63, abs=0.005
    )  # WARM은 PET를 나누지 않는다


def test_reuse_counts_site_input_only(reg):
    found = circular.stream_effect(
        rec(reg),
        stream(
            reuse={
                "recoveryYieldPct": 80,
                "virginSubstitutionPct": 90,
                "recoveryApplication": "가상 동일 제품 재투입",
                "recoveryEvidence": ["human:x"],
            }
        ),
        600,
        "incineration",
        False,
        "reuse",
        400,
        ["human:x"],
    )
    assert found["virgin"]["method"] == "site" and found["virgin"]["value"] == pytest.approx(
        400 * 0.8 * 0.9
    )
    assert found["ghg"]["status"] == "hold" and "분쇄 전력" in found["ghg"]["needs"]


def test_scenario_partial_sum_and_held_mass(reg):
    r = rec(reg)
    rows = [
        {
            "stream": film(),
            "kg": 300,
            "baseline": "incineration",
            "baselineAssumed": False,
            "dest": "recycle",
        },
        {
            "stream": stream(),
            "kg": 600,
            "baseline": "incineration",
            "baselineAssumed": False,
            "dest": "recycle",
        },
    ]
    scene = circular.scenario(r, "A", rows)
    ghg = scene["totals"]["ghg"]
    assert ghg["status"] == "partial" and ghg["heldStreams"] == ["st_sprue"]
    total = next(c for c in r.claims if c["id"] == ghg["claim"])
    assert total["value"] == pytest.approx(0.3 * 1.381)
    held = next(c for c in r.claims if c["id"] == ghg["heldKg"])
    assert held["value"] == 600
    # 세 지표의 단위가 서로 다르다(합치지 않는다)
    units = {
        c["unit"]
        for c in r.claims
        if c["id"].endswith(("GhgReduction", "EnergySaving", "VirginDisplaced"))
    }
    assert units <= {"tCO2e/월", "GJ/월", "kg/월"}


def _pooled_world(reg):
    """필름은 혼자서는 업체 최소 물량(500kg)을 채우지 못하고, 스프루와 합치면 채운다."""
    reg.put(doc(f"doc:{VENDOR}:매입기준표:필름", [VENDOR, EMITTER], 150, "원/kg"))
    reg.put(doc(f"doc:{EMITTER}:위탁계약서:단가", [EMITTER], 250, "원/kg"))
    seller = vendor(
        permits=[
            {
                "wasteItem": "폐플라스틱",
                "materials": ["PP"],
                "evidence": [f"doc:{VENDOR}:허가증:품목"],
            },
            {
                "wasteItem": "포장재",
                "materials": ["LDPE"],
                "evidence": [f"doc:{VENDOR}:허가증:품목"],
            },
        ],
        criteria=[
            *vendor()["criteria"],
            {
                "id": "pc_film",
                "material": "LDPE",
                "grade": "B",
                "form": "필름",
                "maxContaminationPct": 5,
                "maxMoisturePct": 3,
                "unitPriceKrwPerKg": 150,
                "minMonthlyKg": 500,
                "evidence": [f"doc:{VENDOR}:매입기준표:필름"],
            },
        ],
        process="물질 재활용",
        transport={"status": "visit", "basis": "included"},
    )
    current = {
        "method": "혼합 소각",
        "unitCostKrwPerKg": 250,
        "evidence": [f"doc:{EMITTER}:위탁계약서:단가"],
    }
    measure = {
        "contaminationPct": 1.0,
        "moisturePct": 0.3,
        "measuredOn": "2026-11-10",
        "evidence": [f"measure:{EMITTER}:st_sprue:오염도@2026-11-10"],
    }
    streams = [
        stream(currentTreatment=current, pretreatHoursPerMonth=0),
        film(currentTreatment=current, measure=measure, pretreatHoursPerMonth=0),
    ]
    return streams, [seller]


def test_bundle_scenario_shows_what_pooling_adds(reg):
    streams, vendors = _pooled_world(reg)
    result = run(reg, "optimize_assignment", streams=streams, vendors=vendors, carbonWeight=0)
    out = result["output"]["circular"]
    a, b, c, d = out["scenarios"]
    assert [s["id"] for s in out["scenarios"]] == ["A", "B", "C", "D"]
    routes_a = {row["streamId"]: row["route"] for row in a["rows"]}
    routes_b = {row["streamId"]: row["route"] for row in b["rows"]}
    assert routes_a == {"st_sprue": "recycle", "st_film": "recycle"}
    # 묶음이 없으면 필름은 업체 최소 물량을 혼자 채우지 못해 지금처럼 처리한다
    assert routes_b == {"st_sprue": "recycle", "st_film": "keep"}
    assert out["bundle"]["streams"] == ["st_film"]
    assert claim(result, out["bundle"]["kg"])["value"] == 300
    # A는 필름의 감축량을 문헌으로 내고, B는 필름이 그대로라 감축이 없다(스프루는 공정 스크랩이라 보류)
    assert a["totals"]["ghg"]["status"] == "partial"
    assert b["totals"]["ghg"]["status"] == "hold"
    assert claim(result, a["totals"]["ghg"]["claim"])["value"] == pytest.approx(0.3 * 1.381)
    # 월 손익: B의 차이는 따로 계산하고, C는 기준선, D는 매립 처리비가 없어 비운다
    assert claim(result, b["money"])["value"] < claim(result, a["money"])["value"]
    assert c["money"] is None and d["money"] is None and d["moneyReason"]
    assert all(t["status"] == "zero" for t in c["totals"].values())
    assert c["baselineEmission"]["claim"] is None


def test_pyrolysis_is_not_added_to_process_carbon(reg):
    # 열분해 처리 단계 계수(문헌값)는 산출물 용도 근거가 없어 연결하지 않는다
    from app.v3.judge import basis

    assert basis.resolve(reg, EMITTER, "emission.pyrolysis.폐플라스틱") is None


def test_greenest_never_trades_an_unknown_process_carbon():
    """열분해처럼 처리 방식 배출계수를 모르는 배정을 소각으로 되돌리면 탄소가 준다고 말하지 않는다."""
    from tests.v3.evidence_fixtures import negotiation_app as make_app
    from tests.v3.runtime_fixtures import to_approval

    result = to_approval(make_app())["result"]
    purge = next(a for a in result["assignments"] if a["streamId"] == "st_purge")
    assert purge["route"] == "treat" and purge["carbonProcess"] is None
    greenest = result["greenest"]
    assert greenest is None


@pytest.mark.parametrize("process", [None, "재활용", "물질 재활용", "열분해"])
def test_ldpe_reference_uses_matched_mass_without_vendor_process(reg, process):
    ldpe = {**film(), "origin": "PIR", "currentTreatment": {"methodAssumed": True}}
    rows = [
        {"stream": ldpe, "kg": 250, "baseline": None, "baselineAssumed": True, "dest": "unknown"}
    ]
    r = rec(reg)
    found = circular.reference_estimates(r, rows, [{"vendor": {"process": process}}])
    values = {c["id"]: c["value"] for c in r.claims}
    assert values[found["monthlyKg"]] == 250
    totals = found["totals"]
    assert values[totals["ghg"]["claim"]] == pytest.approx(250 / 1000 * 1.381)
    expected_virgin = 250 * 0.73 * 0.84 * 0.75
    assert values[totals["virgin"]["claim"]] == pytest.approx(expected_virgin)
    assert values[totals["energy"]["claim"]] == pytest.approx(
        expected_virgin / 1000 * (82.0 - 7.41)
    )
    assert all(x["status"] == "estimated" for x in totals.values())
    assert all(x["assumptions"] for x in found["rows"])
    assert ldpe["origin"] == "PIR"
    strict = circular.stream_effect(rec(reg), ldpe, 250, None, True, "unknown")
    assert all(strict[m]["status"] == "hold" for m in circular.METRICS)


@pytest.mark.parametrize("material", ["PP+ABS", "PE"])
def test_reference_estimate_does_not_invent_material_factors(reg, material):
    rows = [{"stream": stream(material=material), "kg": 300}]
    found = circular.reference_estimates(rec(reg), rows, [{"vendor": {"id": "vendor"}}])
    assert all(x["status"] == "hold" and x["claim"] is None for x in found["totals"].values())


def test_reference_excludes_unassigned_mass_and_keeps_partial_totals(reg):
    r = rec(reg)
    rows = [
        {"stream": stream(id="pp"), "kg": 300},
        {"stream": stream(id="ps", material="PS"), "kg": 200},
        {"stream": film(), "kg": 450},
    ]
    choices = [{"vendor": {"id": "vendor"}}, {"vendor": {"id": "vendor"}}, {}]
    assert circular.reference_estimates(r, rows, [{}] * 3) is None
    found = circular.reference_estimates(r, rows, choices)
    values = {c["id"]: c["value"] for c in r.claims}
    assert values[found["monthlyKg"]] == 500
    energy = found["totals"]["energy"]
    assert energy["status"] == "partial"
    assert energy["heldStreams"] == ["ps"]
    assert values[energy["heldKg"]] == 200
    assert values[energy["claim"]] == pytest.approx(181.305 / 1000 * (96.0 - 4.87))
    assert values[found["totals"]["ghg"]["claim"]] == pytest.approx(0.5388 + 0.344)


def test_reference_uses_a_fixed_comparison_even_if_existing_route_is_recycling(reg):
    rows = [
        {
            "stream": stream(),
            "kg": 300,
            "baseline": "recycle",
            "baselineAssumed": False,
            "dest": "recycle",
        }
    ]
    r = rec(reg)
    found = circular.reference_estimates(r, rows, [{"vendor": {"process": "재활용"}}])
    ghg = found["totals"]["ghg"]
    assert ghg["status"] == "estimated"
    assert next(c["value"] for c in r.claims if c["id"] == ghg["claim"]) == pytest.approx(0.5388)
