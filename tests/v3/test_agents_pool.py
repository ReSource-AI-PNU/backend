import pytest

from app.v3.a2a.message import Claim
from app.v3.agents.pool import ClaimPool, UnknownClaimRef, is_private, safe_id
from app.v3.demo.seed import share_stream, share_vendor_terms
from app.v3.evidence.validator import validate_text
from app.v3.judge.toolbox import call_tool
from tests.v3.runtime_fixtures import DAON, HANBIT, SAEGIL, TODAY, make_app


@pytest.fixture
def app():
    return make_app()


def test_safe_id_makes_placeholder_names():
    assert safe_id("st_sprue", "monthlyKg") == "st_sprue_monthlyKg"
    assert safe_id("매입", "PP-A") == "PP_A"
    assert safe_id("9", "x") == "c_9_x"
    assert is_private("monthlyNow") and is_private("sensitivityA") and not is_private("unitPrice")


def test_stream_facts_carry_matching_evidence_first(app):
    pool = ClaimPool(app.registry, HANBIT)
    facts = pool.add_stream(app.store.get_stream("st_sprue"))
    assert set(facts) == {"monthlyKg", "contaminationPct", "moisturePct"}
    moisture = pool.get(facts["moisturePct"]).claim
    assert moisture.value == 0.3 and moisture.evidence[0].endswith("수분@2026-11-10")
    text = " ".join("{{" + cid + "}}" for cid in facts.values())
    assert validate_text(
        text, pool.resolve(facts.values()), app.registry, org_id=HANBIT, today=TODAY
    ).ok
    # 현행 처리 단가는 달라고 할 때만 들어간다
    private = ClaimPool(app.registry, HANBIT).add_stream(
        app.store.get_stream("st_sprue"), private=True
    )
    assert "currentUnitCost" in private and "currentUnitCost" not in facts


def test_pool_only_holds_what_the_org_can_see(app):
    stream, profile = app.store.get_stream("st_sprue"), app.store.get_vendor_profile(SAEGIL)
    vendor_pool = ClaimPool(app.registry, SAEGIL)
    assert vendor_pool.add_stream(stream) == {}  # 대화가 열리기 전에는 업체가 볼 수 없다
    share_stream(app.registry, stream, SAEGIL)
    assert set(vendor_pool.add_stream(stream)) == {"monthlyKg", "contaminationPct", "moisturePct"}
    emitter_pool = ClaimPool(app.registry, HANBIT)
    assert emitter_pool.add_vendor_profile(profile)["pc_saegil_pp_a"] == {}
    share_vendor_terms(app.registry, profile, HANBIT)
    shared = emitter_pool.add_vendor_profile(profile)
    assert shared["pc_saegil_pp_a"]["unitPrice"] == "pc_saegil_pp_a_unitPrice"
    assert shared["fleet"] == {}  # 차량 근거는 열지 않는다
    assert set(vendor_pool.add_vendor_profile(profile)["fleet"]) == {
        "capacityKg",
        "capacityM3",
        "costPerKm",
    }
    # 다른 배출 기업의 흐름은 어떤 풀에도 들어가지 않는다
    assert ClaimPool(app.registry, HANBIT).add_stream(app.store.get_stream("st_daon_pp")) == {}
    assert ClaimPool(app.registry, DAON).add_stream(stream) == {}


def test_tool_results_are_namespaced_and_private_keys_skipped(app):
    stream, profile = app.store.get_stream("st_sprue"), app.store.get_vendor_profile(SAEGIL)
    share_vendor_terms(app.registry, profile, HANBIT)
    result = call_tool(
        "judge_route",
        {"stream": stream, "vendor": profile},
        registry=app.registry,
        org_id=HANBIT,
        today=TODAY,
    )
    pool = ClaimPool(app.registry, HANBIT)
    mapping = pool.add_tool_result(result, "route_sprue", skip_private=True, streamId="st_sprue")
    assert mapping["unitPrice"] == "route_sprue_unitPrice" and "monthlyNow" not in mapping
    assert pool.find(tool="judge_route", key="unitPrice", streamId="st_sprue") == [
        "route_sprue_unitPrice"
    ]
    entry = pool.describe()[0]
    assert {"id", "kind", "value", "unit", "evidence", "label", "source"} <= set(entry)
    full = ClaimPool(app.registry, HANBIT).add_tool_result(result, "route_sprue")
    assert "monthlyNow" in full


def test_claim_refs_must_exist_in_pool(app):
    pool = ClaimPool(app.registry, HANBIT)
    facts = pool.add_stream(app.store.get_stream("st_sprue"))
    output = {"claimRefs": [facts["monthlyKg"], facts["monthlyKg"]]}
    claims = pool.claims_from_output(output)
    assert [c.id for c in claims] == ["st_sprue_monthlyKg"] and isinstance(claims[0], Claim)
    with pytest.raises(UnknownClaimRef) as error:
        pool.claims_from_output({"claimRefs": [facts["monthlyKg"], "made_up", "another"]})
    assert error.value.missing == ["made_up", "another"]
    assert error.value.rejection()["code"] == "UNKNOWN_CLAIM_REF"
    assert pool.claims_from_output({}) == ()
    # 근거가 없는 값은 풀에 들어가지 못한다
    assert pool.add("free", value=123, unit="kg", evidence=[]) is None
    assert pool.add("ghost", value=1, unit="kg", evidence=["doc:none:x:y"]) is None


def test_message_claims_enter_pool_with_sequence_prefix(app):
    stream = app.store.get_stream("st_sprue")
    pool = ClaimPool(app.registry, HANBIT)
    facts = pool.add_stream(stream)
    claim = pool.get(facts["monthlyKg"]).claim.to_dict()
    message = {"seq": 3, "from": HANBIT, "claims": [claim], "meta": {}}
    assert pool.add_message(message) == {"st_sprue_monthlyKg": "m3_st_sprue_monthlyKg"}
    assert pool.get("m3_st_sprue_monthlyKg").tags["seq"] == 3
