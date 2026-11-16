import pytest

from app.v3.demo.seed import (
    seed_demo,
    share_evidence,
    share_stream,
    share_vendor_terms,
    stream_evidence_ids,
    vendor_evidence_ids,
)
from app.v3.evidence.registry import EvidenceForbidden, InMemoryRegistry
from app.v3.store import InMemoryStore

TODAY = "2026-11-17"


@pytest.fixture
def demo():
    store, registry = InMemoryStore(lambda: "2026-11-17T00:00:00+00:00"), InMemoryRegistry()
    return store, registry, seed_demo(store, registry, TODAY)


def test_orgs_and_streams(demo):
    store, _, summary = demo
    assert summary["synthetic"] is True and summary["emitterOrgId"] == "org_hanbit"
    names = {o["id"]: o["name"] for o in store.list_orgs()}
    assert names == {
        "org_hanbit": "(주)한빛정밀",
        "org_daon": "(주)다온산업",
        "org_saegil": "(주)새길자원",
        "org_namhae": "(주)남해열분해",
        "org_miso": "(주)미소환경",
    }
    assert all(o["synthetic"] for o in store.list_orgs())
    streams = {s["id"]: s for s in store.list_streams("org_hanbit")}
    assert {k: (v["material"], v["origin"], v["monthlyKg"]) for k, v in streams.items()} == {
        "st_sprue": ("PP", "PIR", 600),
        "st_defect": ("PP", "PIR", 400),
        "st_purge": ("PP+ABS", "PIR", 250),
        "st_film": ("LDPE", "PCR", 300),
    }
    assert streams["st_sprue"]["measure"]["contaminationPct"] == 1.0
    assert streams["st_defect"]["measure"] is None and streams["st_defect"]["photo"]
    assert streams["st_film"]["measure"] is None  # 수분 미확인
    assert streams["st_sprue"]["currentTreatment"]["unitCostKrwPerKg"] == 250
    assert streams["st_purge"]["currentTreatment"] is None  # 혼합 소각 가정이 쓰인다
    assert [s["id"] for s in store.list_streams("org_daon")] == ["st_daon_pp"]


def test_vendor_profiles(demo):
    store, _, _ = demo
    vendors = {p["orgId"]: p for p in store.list_vendor_profiles()}
    assert vendors["org_namhae"]["model"]["provider"] == "ollama"
    assert vendors["org_saegil"]["model"] == {"provider": "foundry", "name": "gpt-5-mini"}

    def permitted(org):
        return {(p["wasteItem"], m) for p in vendors[org]["permits"] for m in p["materials"]}

    assert ("포장재", "LDPE") in permitted("org_saegil")
    assert not any(m in ("LDPE", "LLDPE") or item == "포장재" for item, m in permitted("org_miso"))
    assert ("폐플라스틱", "PP+ABS") in permitted("org_namhae")
    for profile in vendors.values():
        assert set(profile["guardrails"]) >= {
            "floorPriceKrwPerKg",
            "days",
            "minPickupsPerMonth",
            "autoDecline",
        }
        assert set(profile["fleet"]) >= {"depot", "capacityKg", "capacityM3", "costPerKm"}
        for criterion in profile["criteria"]:
            floor = profile["guardrails"]["floorPriceKrwPerKg"]
            ceiling = profile["guardrails"]["ceilingPriceKrwPerKg"]
            assert floor <= criterion["unitPriceKrwPerKg"] <= ceiling


def test_every_reference_is_registered_with_matching_value(demo):
    store, registry, summary = demo
    referenced = []
    for org in ("org_hanbit", "org_daon"):
        for stream in store.list_streams(org):
            referenced += stream_evidence_ids(stream)
            cost = stream["currentTreatment"]
            if cost:
                assert registry.get(cost["evidence"][0]).value == cost["unitCostKrwPerKg"]
            kg = registry.get(stream["monthlyKgEvidence"][0])
            assert (kg.value, kg.unit) == (stream["monthlyKg"], "kg/월")
    for profile in store.list_vendor_profiles():
        referenced += vendor_evidence_ids(profile)
        for criterion in profile["criteria"]:
            price = registry.get(criterion["evidence"][0])
            assert (price.value, price.unit) == (criterion["unitPriceKrwPerKg"], "원/kg")
    assert set(referenced) == set(summary["evidenceIds"])
    for evidence_id in summary["evidenceIds"]:
        evidence = registry.get(evidence_id)
        assert evidence.kind in ("doc", "measure", "model", "human")
        assert evidence.meta["synthetic"] is True and "가상" in evidence.source


def test_evidence_is_private_until_shared(demo):
    store, registry, _ = demo
    price = "doc:org_saegil:매입기준표:pc_saegil_pp_a-단가"
    own = "measure:org_hanbit:st_sprue:오염도@2026-11-10"
    assert registry.get(price, "org_saegil").value == 320
    for outsider, evidence_id in (("org_hanbit", price), ("org_saegil", own), ("org_daon", own)):
        with pytest.raises(EvidenceForbidden):
            registry.get(evidence_id, outsider)
    assert registry.list(org_id="org_daon") == registry.list(
        org_id="org_daon", kinds=["doc", "measure"]
    )
    assert all(e.meta["ownerOrgId"] == "org_daon" for e in registry.list(org_id="org_daon"))

    saegil = store.get_vendor_profile("org_saegil")
    shared = share_vendor_terms(registry, saegil, "org_hanbit")
    assert price in shared and registry.get(price, "org_hanbit").value == 320
    fleet = saegil["fleet"]["evidence"][0]
    assert fleet not in shared
    with pytest.raises(EvidenceForbidden):
        registry.get(fleet, "org_hanbit")
    with pytest.raises(EvidenceForbidden):
        registry.get(price, "org_daon")

    sprue = store.get_stream("st_sprue")
    assert own in share_stream(registry, sprue, "org_saegil")
    assert registry.get(own, "org_saegil").value == 1.0
    contract = sprue["currentTreatment"]["evidence"][0]
    with pytest.raises(EvidenceForbidden):
        registry.get(contract, "org_saegil")  # 현행 계약 단가는 업체에 열지 않는다
    version = registry.get(price).version
    assert share_evidence(registry, price, "org_hanbit").version == version  # 다시 열어도 그대로


def test_reseed_gives_same_state(demo):
    store, registry, summary = demo
    before = store.snapshot()
    evidence = [e.to_dict() for e in registry.list()]
    store.put_session({"id": "ses_1", "emitterOrgId": "org_hanbit"})
    store.put_stream({"id": "st_extra", "orgId": "org_hanbit"})
    assert seed_demo(store, registry, TODAY) == summary
    assert store.snapshot() == before
    assert [e.to_dict() for e in registry.list()] == evidence
    fresh_store, fresh_registry = (
        InMemoryStore(lambda: "2026-11-17T00:00:00+00:00"),
        InMemoryRegistry(),
    )
    seed_demo(fresh_store, fresh_registry, TODAY)
    assert fresh_store.snapshot() == before
