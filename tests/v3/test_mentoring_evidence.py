"""Local regression cases for the three mentoring/evidence requests. All companies are fictitious."""

from dataclasses import replace

import pytest

from app.v3 import service
from app.v3.a2a.guardrails import check_terms, validate_policy
from app.v3.agents import reviewer
from app.v3.agents.contract_terms import INSPECTION_HOLD, inspection_fallback, transport_text
from app.v3.evidence.model import Evidence
from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from app.v3.judge import circular
from app.v3.judge.common import Recorder, transport_carbon, vendor_distance
from app.v3.judge.route import current_cost
from tests.v3.judge_fixtures import EMITTER, TODAY, registry, stream
from tests.v3.runtime_fixtures import SAEGIL, make_app


@pytest.mark.parametrize(
    "floor,ceiling", [(100, 90), (float("nan"), 90), (100, float("inf")), (True, 90)]
)
def test_invalid_policy_does_not_mutate_saved_profile(floor, ceiling):
    app = make_app()
    before = app.store.get_vendor_profile(SAEGIL)
    with pytest.raises(service.ServiceError) as error:
        service.update_vendor(
            app,
            SAEGIL,
            {"guardrails": {"floorPriceKrwPerKg": floor, "ceilingPriceKrwPerKg": ceiling}},
            SAEGIL,
        )
    assert error.value.status == 422
    assert app.store.get_vendor_profile(SAEGIL) == before


@pytest.mark.parametrize(
    "floor,ceiling", [(100, 400), (100, 100), (None, 90), (100, None), (None, None), (-400, -100)]
)
def test_signed_and_optional_bounds(floor, ceiling):
    validate_policy({"floorPriceKrwPerKg": floor, "ceilingPriceKrwPerKg": ceiling})


def test_invalid_stored_policy_cannot_negotiate_or_pass_review():
    guard = {"floorPriceKrwPerKg": 100, "ceilingPriceKrwPerKg": 90}
    assert check_terms(guard, {"unitPriceKrwPerKg": 95})[0].code == "INVALID_POLICY"
    found = reviewer.review(
        rows=[], offers={}, guardrails=guard, regulation=[], approval=None, earlier_deals=0
    )
    assert found["status"] == "blocked"


def test_unknown_fees_block_approval_and_are_not_zero():
    row = {"transportPaidBy": "vendor", "transportPending": True}
    found = reviewer.review(
        rows=[row], offers={}, guardrails={}, regulation=[], approval=None, earlier_deals=0
    )
    assert next(i for i in found["items"] if i["key"] == "transport")["status"] == "block"
    assert "미정" in transport_text(row)
    assert "단가 포함" in transport_text({"transportPaidBy": "vendor"})
    assert "별도" in transport_text({"transportPaidBy": "emitter"})


def test_grade_reference_requires_explicit_existing_matching_criterion():
    criterion = {
        "id": "b",
        "material": "PP",
        "form": "압축",
        "grade": "B",
        "unitPriceKrwPerKg": 250,
        "evidence": ["doc:test"],
    }
    s = {"material": "PP", "form": "압축"}
    assert inspection_fallback({"criteria": [criterion]}, s) is None
    profile = {"criteria": [criterion], "inspectionFallbackCriterionId": "b"}
    assert inspection_fallback(profile, s) == criterion
    assert inspection_fallback(profile, {**s, "form": "가공 전"}) is None
    assert "다음 등급" not in INSPECTION_HOLD


def circular_registry():
    reg = registry()
    load_seed_file(reg, SEED_DIR / "circular.json")
    return reg


def recorder(reg):
    return Recorder("evidence_regression", reg, EMITTER, TODAY, {})


def test_original_pp_energy_yields_and_foreign_estimate():
    reg = circular_registry()
    effects = circular.stream_effect(
        recorder(reg), stream(origin="PCR", form="압축"), 1000, "incineration", False, "recycle"
    )
    assert effects["virgin"]["value"] == pytest.approx(1000 * 0.79 * 0.85 * 0.9)
    assert effects["energy"]["value"] == pytest.approx((44.5 - 18.79) * 1.05505585262 / 0.90718474)
    assert effects["ghg"]["value"] == pytest.approx(1.796)
    assert "해외" in effects["explain"]["basis"]
    # Changing the JRC yield must not multiply an already net WARM/JRC impact again.
    factor = reg.get("ref:JRC2021:PP-수율-신재대체율@2021")
    reg.put(replace(factor, value=7.65, meta={**factor.meta, "sortingRate": 10}))
    changed = circular.stream_effect(
        recorder(reg), stream(origin="PCR"), 1000, "incineration", False, "recycle"
    )
    assert changed["ghg"]["value"] == effects["ghg"]["value"]
    assert changed["energy"]["value"] == effects["energy"]["value"]
    assert changed["virgin"]["value"] == pytest.approx(1000 * 0.1 * 0.85 * 0.9)


@pytest.mark.parametrize(
    "key,value",
    [
        ("functionalUnit", "kg output pellets"),
        ("systemBoundary", "factory electricity only"),
        ("country", "KR"),
        ("energyType", "electricity"),
    ],
)
def test_incompatible_energy_is_held(key, value):
    reg = circular_registry()
    factor = reg.get("ref:WARM16:PP-recycle-순에너지@2023-12")
    reg.put(replace(factor, meta={**factor.meta, key: value}))
    found = circular.energy_effect(
        recorder(reg), stream(origin="PCR"), 1000, "incineration", "recycle"
    )
    assert found["status"] == "hold" and found["value"] is None


def test_missing_baseline_and_reuse_not_automatically_one_to_one():
    reg = circular_registry()
    assert current_cost(recorder(reg), stream())[0] is None
    found = circular.stream_effect(
        recorder(reg), stream(origin="PCR"), 1000, "incineration", True, "recycle"
    )
    assert all(found[k]["status"] == "hold" for k in circular.METRICS)
    assert (
        circular.virgin_effect(recorder(reg), stream(), 1000, "incineration", "reuse", 900)[
            "status"
        ]
        == "hold"
    )


def test_zero_centroid_and_missing_address_hold_transport():
    rec = recorder(registry())
    vendor = {"fleet": {"depot": [129, 35]}}
    assert not vendor_distance(rec, vendor, None)["known"]
    assert not vendor_distance(rec, vendor, [129, 35])["known"]
    assert not transport_carbon(rec, {"known": True, "km": 0}, 1000)["known"]
    assert not transport_carbon(rec, {"known": True, "km": 10, "assumed": True}, 1000)["known"]


def test_calculation_preserves_coefficient_version_and_acl():
    reg = registry()
    factor = Evidence(
        id="ref:version-test",
        kind="ref",
        value=2,
        unit="kg",
        excerpt="test",
        source="fixture",
        as_of=TODAY,
    )
    reg.put(factor)
    rec = recorder(reg)
    first = rec.number("out", 20, "kg", "10 * coefficient", [factor.id], private=True)
    frozen_id = reg.get(first["evidence"][0]).meta["inputEvidence"][0]
    reg.put(replace(factor, value=3))
    second = recorder(reg).number("out", 30, "kg", "10 * coefficient", [factor.id], private=True)
    assert first["evidence"][0] != second["evidence"][0]
    assert reg.get(frozen_id).value == 2
    assert reg.get(first["evidence"][0]).value == 20
    assert not reg.get(first["evidence"][0]).visible_to("another-company")


def site_reuse(**extra):
    return {
        "virginPriceKrwPerKg": 1000,
        "reusableKgPerMonth": 500,
        "regrindCostKrwPerKg": 100,
        "recoveryYieldPct": 80,
        "virginSubstitutionPct": 50,
        "recoveryApplication": "가상 시험의 PP 부품 원료",
        "recoveryEvidence": ["doc:site-test"],
        "recoveryResidueCostKrwPerKg": 200,
        **extra,
    }


def test_site_recovery_uses_mass_balance_and_prices_losses():
    from app.v3.judge.route import reuse_numbers

    reuse = site_reuse()
    found = circular.virgin_effect(
        recorder(circular_registry()), stream(reuse=reuse), 1000, "incineration", "reuse", 500
    )
    assert found["value"] == 200 and found["method"] == "site"
    assert reuse_numbers(stream(reuse=reuse), 1000, 250) == pytest.approx(
        (500, 150000, 145000, 5000)
    )
    assert (
        reuse_numbers(stream(reuse=site_reuse(recoveryResidueCostKrwPerKg=None)), 1000, 250) is None
    )
    assert reuse_numbers(stream(reuse=site_reuse(regrindCostKrwPerKg=None)), 1000, 250) is None


@pytest.mark.parametrize(
    "field",
    ["recoveryYieldPct", "virginSubstitutionPct", "recoveryApplication", "recoveryEvidence"],
)
def test_missing_site_input_holds(field):
    found = circular.virgin_effect(
        recorder(circular_registry()),
        stream(reuse=site_reuse(**{field: None})),
        1000,
        "incineration",
        "reuse",
        500,
    )
    assert found["status"] == "hold"


def test_recovery_evidence_access_and_range_are_enforced_by_service():
    from tests.v3.runtime_fixtures import HANBIT

    app = make_app()
    proof = Evidence(
        id="doc:site-test",
        kind="doc",
        source="가상 계량 시험",
        excerpt="test",
        as_of=TODAY,
        access=(HANBIT,),
        meta={"streamId": "st_sprue"},
    )
    app.registry.put(proof)
    for wrong in (
        site_reuse(recoveryYieldPct=101),
        site_reuse(virginSubstitutionPct=float("nan")),
        site_reuse(recoveryEvidence=["doc:missing"]),
    ):
        with pytest.raises(service.ServiceError):
            service.update_stream(app, HANBIT, "st_sprue", {"reuse": wrong}, HANBIT)
    saved = service.update_stream(app, HANBIT, "st_sprue", {"reuse": site_reuse()}, HANBIT)
    assert saved["reuse"]["recoveryYieldPct"] == 80
    app.registry.put(replace(proof, access=("other",)))
    with pytest.raises(service.ServiceError) as caught:
        service.update_stream(app, HANBIT, "st_sprue", {"reuse": site_reuse()}, HANBIT)
    assert caught.value.status == 404


def test_unverified_domestic_and_worldsteel_factors_are_not_used():
    reg = circular_registry()
    rec = recorder(reg)
    assert (
        circular.baseline_emission(
            rec, [{"stream": stream(), "baseline": "incineration", "kg": 1000}]
        )["claim"]
        is None
    )
    assert circular.resource_refs(rec, stream(material="철"), 1000, "recycle") == []


def test_source_units_convert_without_combining_energy_types():
    from app.v3.evidence.units import convert

    assert convert(1000, "kg", "t") == 1
    assert convert(1, "MMBtu/short ton", "GJ/t") == pytest.approx(1.05505585262 / 0.90718474)


def test_cost_pending_is_visible_and_no_transport_estimate_wins():
    from tests.v3.judge_fixtures import run, vendor

    row = stream(currentTreatment={"method": "소각", "unitCostKrwPerKg": 250})
    reg = registry()
    missing = run(reg, "cost_carbon", stream=row, vendor=vendor())
    assert missing["output"]["transportPending"] and missing["output"]["costPending"]
    selected = run(reg, "optimize_assignment", streams=[row], vendors=[vendor()])
    assert selected["output"]["assignments"][0]["route"] == "keep"


def test_contract_documents_use_same_included_terms_and_preserve_sources():
    from tests.v3.runtime_fixtures import HANBIT, approve_all, to_approval

    app = make_app()
    confirmed = approve_all(app, to_approval(app))
    docs = confirmed["documents"]
    assert {d["kind"] for d in docs} >= {"request", "plan", "contract"}
    for doc in docs:
        assert doc["validation"]["ok"]
        assert "운반비 별도" not in doc["rendered"]
        assert "운반비 단가 포함" in doc["rendered"]
        assert "다음 등급" not in doc["rendered"]
        assert "현행 처리 단가가 없어" not in doc["rendered"]
        for claim in doc["claims"]:
            assert all(app.registry.get(e, HANBIT).meta.get("frozen") for e in claim["evidence"])


def test_missing_baseline_is_held_in_view_and_unknown_carbon_is_not_best():
    from tests.v3.runtime_fixtures import to_approval

    view = to_approval(make_app())
    for row in view["streams"]:
        if row["id"] in ("st_film", "st_purge"):
            assert row["currentTreatment"]["method"] == "미확인"
            assert row["currentTreatment"]["unitCost"] is None
    assert view["result"]["greenest"] is None


@pytest.mark.parametrize("change", [{"unit": "원/kg"}, {"meta_material": "PET"}])
def test_bad_units_and_wrong_material_cannot_enter_energy(change):
    reg = circular_registry()
    factor = reg.get("ref:WARM16:PP-recycle-순에너지@2023-12")
    if "unit" in change:
        reg.put(replace(factor, unit=change["unit"]))
    else:
        reg.put(replace(factor, meta={**factor.meta, "material": change["meta_material"]}))
    assert (
        circular.energy_effect(
            recorder(reg), stream(origin="PCR"), 1000, "incineration", "recycle"
        )["status"]
        == "hold"
    )


def test_saved_calculation_inputs_do_not_follow_mutation():
    reg = registry()
    args = {"stream": {"id": "mutable", "monthlyKg": 10}}
    rec = Recorder("input_freeze", reg, EMITTER, TODAY, args)
    claim = rec.number("mass", 10, "kg", "input mass", private=True)
    args["stream"]["monthlyKg"] = 999
    assert reg.get(claim["evidence"][0]).meta["inputs"]["args"]["stream"]["monthlyKg"] == 10


def test_process_declaration_is_recorded_and_change_invalidates_approval():
    from tests.v3.runtime_fixtures import HANBIT, thread_of, to_approval

    app = make_app()
    saved = service.update_vendor(app, SAEGIL, {"process": "재활용"}, SAEGIL)
    proof = app.registry.get(saved["processEvidence"][0])
    assert proof.meta["verificationStatus"] == "user_reported"
    with pytest.raises(service.ServiceError):
        service.update_vendor(
            app,
            SAEGIL,
            {
                "process": "열분해",
                "guardrails": {"floorPriceKrwPerKg": 100, "ceilingPriceKrwPerKg": 90},
            },
            SAEGIL,
        )
    assert app.registry.get(proof.id) == proof
    assert app.store.get_vendor_profile(SAEGIL)["process"] == "재활용"
    view = to_approval(app)
    thread = thread_of(view, SAEGIL)
    service.approve(app, view["session"]["id"], thread["id"], HANBIT)
    service.update_vendor(app, SAEGIL, {"process": "열분해"}, SAEGIL)
    changed = service.get_view(app, view["session"]["id"], HANBIT)
    assert not next(a for a in changed["approvals"] if a["threadId"] == thread["id"])["emitter"]
    with pytest.raises(service.ServiceError):
        service.update_vendor(app, SAEGIL, {"process": "알 수 없는 기술"}, SAEGIL)


def test_generic_recycling_does_not_establish_mechanical_technology():
    from app.v3.judge.common import after_process

    assert circular.destination({"vendor": {"process": "재활용"}}, after_process) == "unknown"
    assert circular.destination({"vendor": {"process": "물질 재활용"}}, after_process) == "recycle"


def test_same_wrong_material_in_both_factors_is_not_applicable_to_stream():
    reg = circular_registry()
    for route in ("recycle", "combustion"):
        factor = reg.get(f"ref:WARM16:PP-{route}-순에너지@2023-12")
        reg.put(replace(factor, meta={**factor.meta, "material": "PET"}))
    found = circular.energy_effect(
        recorder(reg), stream(origin="PCR"), 1000, "incineration", "recycle"
    )
    assert found["status"] == "hold"


@pytest.mark.parametrize("km,kg", [(-1, 1000), (float("nan"), 1000), (10, -1), (10, float("inf"))])
def test_invalid_actual_transport_inputs_are_held(km, kg):
    found = transport_carbon(
        recorder(circular_registry()),
        {"known": True, "km": km, "actualSiteAddressesConfirmed": True},
        kg,
    )
    assert not found["known"] and found["tco2e"] is None
