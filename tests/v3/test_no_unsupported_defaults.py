"""Synthetic unit-test fixtures verify gates, not real measured performance."""

from dataclasses import replace

import pytest

from app.v3.agents.company import SYSTEM_PROMPT as COMPANY
from app.v3.agents.orchestrator import discoverable_profiles
from app.v3.agents.vendor import SYSTEM_PROMPT as VENDOR
from app.v3.evidence.acceptance import approved_policy, usable_basis
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from app.v3.intake.grade_model import grade_stream
from app.v3.judge import basis, defaults, negotiate
from app.v3.judge.common import Recorder, grade_of, vendor_distance
from app.v3.store import InMemoryStore


@pytest.fixture
def registry():
    basis.reset()
    reg = InMemoryRegistry()
    defaults.register_defaults(reg, "2026-11-21")
    load_seed_file(reg, SEED_DIR / "official.json")
    load_seed_file(reg, SEED_DIR / "circular.json")
    load_seed_file(reg, SEED_DIR / "refs.json")
    return reg


@pytest.mark.parametrize(
    "key",
    [
        "negotiation.askPct",
        "contract.pilotMonths",
        "contract.priceBandPct",
        "distance.detourFactor",
        "transport.costPerKm",
        "min.monthlyKg.폐플라스틱",
        "emission.transport.truck",
    ],
)
def test_unapproved_or_unverified_defaults_are_not_used(registry, key):
    assert basis.resolve(registry, "real-org", key) is None


def test_original_checked_literature_remains_available(registry):
    assert basis.resolve(registry, "real-org", "circular.ghg.PP.recycle.incineration") is not None


@pytest.mark.parametrize("key", ["kau.price", "reference.price.PP.압축", "labor.costPerHour"])
def test_official_values_checked_against_the_original_are_used(registry, key):
    found = basis.resolve(registry, "real-org", key)
    assert found is not None and found.kind == "ref"
    assert found.meta["verificationStatus"] == "original_checked" and found.meta["verifiedOn"]


def test_policy_needs_a_real_decision_record_not_just_a_policy_label(registry):
    policy = registry.get(defaults.default_id("negotiation.askPct"))
    assert not approved_policy(policy)
    approved = replace(
        policy,
        meta={
            **policy.meta,
            "policyApproval": {
                "status": "approved",
                "approvedBy": "TEST ONLY operator",
                "reason": "TEST ONLY agreed mandate",
                "source": "TEST ONLY decision record",
                "approvedOn": "2026-11-21",
            },
        },
    )
    assert approved_policy(approved)
    for key in ("approvedBy", "reason", "source", "approvedOn"):
        missing = {**approved.meta["policyApproval"], key: ""}
        assert not approved_policy(
            replace(approved, meta={**approved.meta, "policyApproval": missing})
        )


def test_no_automatic_uplift_without_approved_policy(registry):
    result = negotiate.ask_price(
        registry,
        org_id="real-org",
        today="2026-11-21",
        stream_id="s",
        offer={"value": 300, "unit": "원/kg", "evidence": []},
        vendor_org="real-vendor",
    )
    assert result["output"]["asked"] is None
    assert result["status"] == "needs_information"


def test_zero_approved_policy_does_not_secretly_add_one_krw(registry):
    policy = registry.get(defaults.default_id("negotiation.askPct"))
    registry.put(
        replace(
            policy,
            value=0,
            meta={
                **policy.meta,
                "policyApproval": {
                    "status": "approved",
                    "approvedBy": "TEST operator",
                    "reason": "TEST no adjustment",
                    "source": "TEST decision",
                    "approvedOn": "2026-11-21",
                },
            },
        )
    )
    result = negotiate.ask_price(
        registry,
        org_id="real-org",
        today="2026-11-21",
        stream_id="s",
        offer={"value": 300, "evidence": []},
        vendor_org="real-vendor",
    )
    assert result["output"]["asked"] is None


def test_demo_and_real_suppliers_never_mix():
    store = InMemoryStore()
    for name, demo in (("demo-vendor", True), ("real-vendor", False)):
        store.put_org({"id": name, "role": "vendor", "demo": demo})
        store.put_vendor_profile({"orgId": name, "criteria": [{"id": "c"}]})
    assert set(discoverable_profiles(store, {"demo": False})) == {"real-vendor"}
    assert set(discoverable_profiles(store, {"demo": True})) == {"demo-vendor"}


def test_missing_road_data_does_not_use_detour_default(registry):
    rec = Recorder("test", registry, "real-org", "2026-11-21", {})
    assert not vendor_distance(rec, {"fleet": {"depot": [129, 35]}}, [128, 35])["known"]


def test_team_grade_thresholds_are_not_measured_grades(registry):
    rec = Recorder("test", registry, "real-org", "2026-11-21", {})
    assert grade_of(rec, "PP", 1, 0.3) == (None, [])
    result = grade_stream(
        registry,
        "real-org",
        {"id": "s", "material": "PP", "measure": {"contaminationPct": 1, "moisturePct": 0.3}},
        None,
        "2026-11-21",
    )
    assert result["grade"] is None and result["checks"][0]["key"] == "grade_standard"


def test_synthetic_flags_cannot_become_official_facts():
    evidence = Evidence(
        id="ref:test",
        kind="ref",
        value=1,
        source="test",
        excerpt="Synthetic test fixture",
        as_of="2026-11-21",
        meta={"verificationStatus": "original_checked", "synthetic": True},
    )
    assert not usable_basis(evidence)


def test_agents_receive_explicit_mandate_and_limitation():
    for prompt in (COMPANY, VENDOR):
        assert "2026-11-21-explicit-evidence" in prompt
        assert "승인 기록 없는 인상률" in prompt
        assert "시장 최적" in prompt


def test_a_positive_sale_price_does_not_prove_the_treatment_process():
    from app.v3.outcome_carbon import applied_process

    assert applied_process(None, {}, 300) == {"value": None, "source": "unknown", "assumed": False}
