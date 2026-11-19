"""허가 근거 수준과 수거 운행 배출이 최종 판단(배정, 법령 요건)에 들어가는지.

2026-11-19 평가 의견: "허가 근거의 수준과 수거 거리의 환경 영향이 최종 판단에 충분히 반영되지 않는다".
"""

import pytest

from app.v3.evidence.model import Evidence
from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from app.v3.judge.permits import permit_level, registry_records
from tests.v3.judge_fixtures import (
    EMITTER,
    ORIGIN,
    VENDOR,
    VENDOR2,
    assert_grounded,
    claim,
    registry,
    run,
    stream,
    vendor,
)

# 공공 허가 명부(registry.v1.json)에 있는 실제 업체 두 곳. 상호와 좌표만 빌려 대조를 시험한다
PLASTIC_NAME, PLASTIC_AT = "㈜우리환경산업", [129.12468, 35.229955]  # 51-03-01 폐합성수지류
TIRE_NAME, TIRE_AT = "감만타이어", [129.090554, 35.11976]  # 51-15-02 폐타이어만


def _human_permit_vendor(reg, **over):
    """업체가 화면에서 입력만 한 허가 품목(human 근거, 허가증 원본 확인 전)."""
    eid = f"human:{VENDOR}:허가품목-폐플라스틱"
    reg.put(
        Evidence(
            id=eid,
            kind="human",
            excerpt="영업대상 폐기물: 폐플라스틱 (PP, PE)",
            source="업체 담당자 입력(허가증 원본 확인 전)",
            as_of="2026-11-10",
            access=(VENDOR, EMITTER),
        )
    )
    return vendor(
        permits=[{"wasteItem": "폐플라스틱", "materials": ["PP", "PE"], "evidence": [eid]}],
        **over,
    )


def _seeded():
    reg = registry()
    load_seed_file(reg, SEED_DIR / "refs.json")  # 배출권 시세(KAU)
    return reg


# ── 허가 근거 수준 ─────────────────────────────────────────────────────


def test_document_permit_is_met_but_says_registry_was_not_checked():
    reg = registry()
    level = permit_level(reg, stream(), vendor(name="(주)새길자원"))
    assert level["level"] == "document" and level["status"] == "met"
    assert "공공 허가 명부와는 대조하지 못했으니" in level["text"]


def test_vendor_typed_permit_is_conditional():
    reg = registry()
    level = permit_level(reg, stream(), _human_permit_vendor(reg))
    assert level["level"] == "self" and level["status"] == "conditional"


def test_registry_match_needs_same_name_nearby_and_matching_code():
    reg = registry()
    near = vendor(name=PLASTIC_NAME, fleet={**vendor()["fleet"], "depot": PLASTIC_AT})
    level = permit_level(reg, stream(), near)
    assert level["level"] == "registry" and level["status"] == "met"
    ref = next(e for e in level["evidence"] if e.startswith("ref:허가명부:"))
    found = reg.get(ref, EMITTER)
    assert found.kind == "ref" and "51-03-01" in found.excerpt and "*" in found.access
    # 같은 상호라도 차고지에서 먼 사업장은 다른 곳으로 본다(허가증 문서 수준에 머문다)
    far = vendor(name=PLASTIC_NAME, fleet={**vendor()["fleet"], "depot": [126.97, 37.56]})
    assert registry_records(PLASTIC_NAME, [126.97, 37.56]) == []
    assert permit_level(reg, stream(), far)["level"] == "document"


def test_registry_without_this_waste_code_is_mismatch():
    reg = registry()
    tire = vendor(name=TIRE_NAME, fleet={**vendor()["fleet"], "depot": TIRE_AT})
    level = permit_level(reg, stream(), tire)
    assert level["level"] == "mismatch" and level["status"] == "conditional"


def test_requirement_permit_holder_follows_level():
    reg = registry()
    load_seed_file(reg, SEED_DIR / "laws.json")
    doc_vendor = {**vendor(), "name": "(주)새길자원"}
    r = run(reg, "check_requirements", stream=stream(), route="sell", vendors=[doc_vendor])
    item = next(i for i in r["output"]["items"] if i["decidedBy"] == "permit_match")
    assert item["status"] == "met" and "허가 근거: (주)새길자원: 허가증 문서" in item["note"]
    typed = {**_human_permit_vendor(reg), "name": "(주)새길자원"}
    r = run(reg, "check_requirements", stream=stream(), route="sell", vendors=[typed])
    item = next(i for i in r["output"]["items"] if i["decidedBy"] == "permit_match")
    assert item["status"] == "conditional" and "업체 입력뿐" in item["note"]
    assert r["status"] == "conditional"
    assert_grounded(reg, r)


def test_assignment_with_vendor_typed_permit_is_conditional():
    reg = _seeded()
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[_human_permit_vendor(reg)])
    entry = r["output"]["assignments"][0]
    assert entry["vendorOrgId"] == VENDOR
    assert entry["permit"]["level"] == "self" and entry["status"] == "conditional"
    assert r["status"] == "conditional"
    assert_grounded(reg, r)


# ── 수거 운행 배출 ─────────────────────────────────────────────────────


def test_transport_emissions_enter_carbon_and_objective():
    reg = _seeded()
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()], origin=ORIGIN)
    out = r["output"]
    assert out["carbonInObjective"] is True
    price = claim(r, out["carbonPrice"])
    assert price["unit"] == "원/tCO2e" and price["evidence"][1].startswith("ref:KAU:")
    entry = out["assignments"][0]
    process = claim(r, entry["carbonProcess"])["value"]
    moved = claim(r, entry["carbonTransport"])["value"]
    total = claim(r, entry["carbon"])["value"]
    assert moved > 0 and total == pytest.approx(process + moved, abs=2e-6)
    # 왕복 거리 × 월 수거 횟수(가드레일 2회) × 0.5 kgCO2e/km
    km = claim(r, "v0RoundTripKm")["value"]
    assert claim(r, "v0MonthlyKm")["value"] == pytest.approx(km * 2, abs=1e-5)
    assert claim(r, "v0TransportCarbon")["value"] == pytest.approx(km * 2 * 0.5 / 1000, abs=1e-5)
    assert claim(r, out["totals"]["carbon"])["value"] == pytest.approx(total, abs=1e-5)
    value = claim(r, entry["carbonValue"])["value"]
    assert value == pytest.approx(-total * price["value"], rel=1e-6)
    assert_grounded(reg, r)


def test_far_vendor_loses_a_tie_on_transport_emissions():
    """같은 단가·같은 조건이면 차고지가 가까운 업체를 고른다(전에는 업체 ID 순서로 먼 곳을 골랐다)."""
    reg = _seeded()
    reg.put(
        Evidence(
            id=f"doc:{VENDOR2}:매입기준표:PP-A",
            kind="doc",
            value=320,
            unit="원/kg",
            excerpt="가상 문서",
            source="가상 시연 문서",
            as_of="2026-11-10",
            access=(VENDOR2, EMITTER),
        )
    )
    near = vendor()  # org_saegil, 차고지 [128.9, 35.1]
    far = vendor(
        orgId=VENDOR2,  # org_miso가 ID 순서로 앞선다
        permits=[
            {
                "wasteItem": "폐플라스틱",
                "materials": ["PP"],
                "evidence": [f"doc:{VENDOR2}:허가증:품목"],
            }
        ],
        criteria=[{**vendor()["criteria"][0], "evidence": [f"doc:{VENDOR2}:매입기준표:PP-A"]}],
        fleet={**vendor()["fleet"], "depot": [129.3, 35.5]},
    )
    without = run(
        reg,
        "optimize_assignment",
        streams=[stream()],
        vendors=[near, far],
        origin=ORIGIN,
        carbonWeight=0,
    )
    assert without["output"]["assignments"][0]["vendorOrgId"] == VENDOR2
    assert without["output"]["carbonInObjective"] is False
    chosen = run(reg, "optimize_assignment", streams=[stream()], vendors=[near, far], origin=ORIGIN)
    assert chosen["output"]["assignments"][0]["vendorOrgId"] == VENDOR
    assert_grounded(reg, chosen)


def test_without_origin_transport_emissions_are_flagged_not_guessed():
    reg = _seeded()
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()])
    entry = r["output"]["assignments"][0]
    assert entry["carbonTransport"] is None and entry["carbonProcess"] is not None
    check = next(c for c in r["checks"] if c["key"] == "transport_carbon")
    assert check["status"] == "conditional" and "공장 위치" in check["text"]


def test_cost_carbon_reports_transport_emissions_for_alternatives():
    reg = _seeded()
    r = run(reg, "cost_carbon", stream=stream(), vendor=vendor(), origin=ORIGIN)
    out = r["output"]
    total = claim(r, out["carbonTotal"])["value"]
    assert total == pytest.approx(
        claim(r, "carbonDifference")["value"] + claim(r, "transportCarbon")["value"]
    )
    assert_grounded(reg, r)
