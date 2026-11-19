"""허가 근거 수준과 운송 배출이 최종 판단(배정, 법령 요건)에 들어가는지.

2026-11-19 평가 의견: "허가 근거의 수준과 수거 거리의 환경 영향이 최종 판단에 충분히 반영되지 않는다".
"""

import pytest

from app.v3.evidence.model import Evidence
from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from app.v3.intake.documents import business_names, profile_from_fields
from app.v3.judge.common import haversine_km
from app.v3.judge.permits import permit_level, registry_records
from tests.v3.judge_fixtures import (
    EMITTER,
    ORIGIN,
    TODAY,
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
PLASTIC_NAME, PLASTIC_AT = "㈜우리환경산업", [129.12468, 35.229955]  # 51-03-01, 중간재활용업
TIRE_NAME, TIRE_AT = "감만타이어", [129.090554, 35.11976]  # 51-15-02 폐타이어만
TRUCK = 0.192419625  # 환경성적표지 평가계수 · 육상수송 트럭(kgCO2e/ton·km)


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


def _second_vendor(reg, depot, price=320):
    """VENDOR2(ID 순서가 앞선다)를 같은 조건의 PP 업체로 만든다. 차고지와 단가만 다르다."""
    reg.put(
        Evidence(
            id=f"doc:{VENDOR2}:매입기준표:PP-A",
            kind="doc",
            value=price,
            unit="원/kg",
            excerpt="가상 문서",
            source="가상 시연 문서",
            as_of="2026-11-10",
            access=(VENDOR2, EMITTER),
        )
    )
    return vendor(
        orgId=VENDOR2,
        permits=[
            {
                "wasteItem": "폐플라스틱",
                "materials": ["PP"],
                "evidence": [f"doc:{VENDOR2}:허가증:품목"],
            }
        ],
        criteria=[
            {
                **vendor()["criteria"][0],
                "unitPriceKrwPerKg": price,
                "evidence": [f"doc:{VENDOR2}:매입기준표:PP-A"],
            }
        ],
        fleet={**vendor()["fleet"], "depot": depot},
    )


# ── 허가 근거 수준 ─────────────────────────────────────────────────────


def test_document_permit_is_met_but_says_registry_was_not_checked():
    reg = registry()
    level = permit_level(reg, stream(), vendor(name="(주)새길자원"))
    assert level["level"] == "document" and level["status"] == "met"
    assert "업종은 읽지 못했습니다" in level["text"]
    assert "공공 허가 명부와는 대조하지 못했으니" in level["text"]
    with_business = vendor(name="(주)새길자원")
    with_business["permits"][0]["business"] = "폐기물종합재활용업"
    level = permit_level(reg, stream(), with_business)
    assert level["level"] == "document"
    assert "업종(폐기물종합재활용업)을 확인했습니다" in level["text"]


def test_vendor_typed_permit_is_conditional():
    reg = registry()
    level = permit_level(reg, stream(), _human_permit_vendor(reg))
    assert level["level"] == "self" and level["status"] == "conditional"


def test_business_must_fit_the_vendor_process():
    """소각하는 업체가 재활용업 허가만 있으면(또는 그 반대면) 허가 근거가 맞지 않는다."""
    reg = registry()
    burner = vendor(process="소각")
    burner["permits"][0]["business"] = "폐기물종합재활용업"
    level = permit_level(reg, stream(), burner)
    assert level["level"] == "mismatch" and level["status"] == "unmet"
    assert "처분업" in level["text"]
    burner["permits"][0]["business"] = "폐기물중간처분업"
    assert permit_level(reg, stream(), burner)["level"] == "document"


def test_registry_match_needs_same_name_nearby_and_matching_code():
    reg = registry()
    near = vendor(name=PLASTIC_NAME, fleet={**vendor()["fleet"], "depot": PLASTIC_AT})
    level = permit_level(reg, stream(), near, TODAY)
    assert level["level"] == "registry" and level["status"] == "met"
    # 명부를 올린 허가 기관과 기준일(한 해 안이면 그 날 허가 업소였다는 문장)
    assert "부산광역시 금정구청이 올린" in level["text"]
    assert "기준일에 허가 업소로 올라 있었습니다" in level["text"]
    ref = next(e for e in level["evidence"] if e.startswith("ref:허가명부:"))
    found = reg.get(ref, EMITTER)
    assert found.kind == "ref" and "51-03-01" in found.excerpt and "*" in found.access
    assert "명부 기준일 2026-07-21" in found.excerpt
    # 기준일이 한 해 넘게 지났으면 최근 상태를 허가 기관에 확인하라고 적는다
    assert "한 해 넘게 지나" in permit_level(reg, stream(), near, "2027-12-01")["text"]
    # 같은 상호라도 차고지에서 먼 사업장은 다른 곳으로 본다(허가증 문서 수준에 머문다)
    far = vendor(name=PLASTIC_NAME, fleet={**vendor()["fleet"], "depot": [126.97, 37.56]})
    assert registry_records(PLASTIC_NAME, [126.97, 37.56]) == []
    assert permit_level(reg, stream(), far)["level"] == "document"


def test_registry_without_this_waste_code_is_mismatch():
    reg = registry()
    tire = vendor(name=TIRE_NAME, fleet={**vendor()["fleet"], "depot": TIRE_AT})
    level = permit_level(reg, stream(), tire)
    assert level["level"] == "mismatch" and level["status"] == "unmet"


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
    tire = {**vendor(fleet={**vendor()["fleet"], "depot": TIRE_AT}), "name": TIRE_NAME}
    r = run(reg, "check_requirements", stream=stream(), route="sell", vendors=[tire])
    item = next(i for i in r["output"]["items"] if i["decidedBy"] == "permit_match")
    assert item["status"] == "unmet" and "넘기지 않습니다" in item["note"]


def test_assignment_with_vendor_typed_permit_is_conditional():
    reg = _seeded()
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[_human_permit_vendor(reg)])
    entry = r["output"]["assignments"][0]
    assert entry["vendorOrgId"] == VENDOR
    assert entry["permit"]["level"] == "self" and entry["status"] == "conditional"
    assert r["status"] == "conditional"
    assert_grounded(reg, r)


def test_mismatched_permit_is_not_assigned():
    reg = _seeded()
    burner = vendor(process="소각")
    burner["permits"][0]["business"] = "폐기물종합재활용업"
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[burner], origin=ORIGIN)
    entry = r["output"]["assignments"][0]
    assert entry["vendorOrgId"] is None and entry["route"] == "keep"
    excluded = r["output"]["permitExcluded"]
    assert [(x["streamId"], x["vendorOrgId"]) for x in excluded] == [("st_sprue", VENDOR)]
    assert any(c["key"].startswith("permit_level:") and c["status"] == "unmet" for c in r["checks"])
    assert_grounded(reg, r)


def test_document_business_is_read_from_fixed_names_only():
    assert business_names("업종: 폐기물 종합 재활용업") == "폐기물종합재활용업"
    assert (
        business_names("폐기물 중간처분업, 수집ㆍ운반업") == "폐기물중간처분업 · 폐기물수집·운반업"
    )
    assert business_names("아무 글 123") is None
    fields = [
        {
            "field": "permit-1",
            "label": "허가 품목",
            "value": "폐플라스틱 (PP, PE)",
            "target": {"section": "permit", "wasteItem": "폐플라스틱", "materials": ["PP", "PE"]},
        },
        {
            "field": "permit-business",
            "label": "허가 업종",
            "value": "폐기물종합재활용업",
            "target": {"section": "permitInfo", "key": "business"},
        },
    ]
    evidence = {"permit-1": "doc:x:허가증:permit-1", "permit-business": "doc:x:허가증:업종"}
    permit = profile_from_fields(fields, evidence)["permits"][0]
    assert permit["business"] == "폐기물종합재활용업"
    assert permit["evidence"] == ["doc:x:허가증:permit-1", "doc:x:허가증:업종"]


# ── 운송 배출 ─────────────────────────────────────────────────────


def test_truck_factor_is_the_official_table_value():
    reg = registry()
    r = run(reg, "cost_carbon", stream=stream(), vendor=vendor(), origin=ORIGIN)
    moved = claim(r, "transportCarbon")
    assert "ref:환경성적표지:트럭-운송@2024-09" in moved["evidence"]
    factor = reg.get("ref:환경성적표지:트럭-운송@2024-09", EMITTER)
    assert factor.value == pytest.approx(TRUCK) and factor.unit == "kgCO2e/tkm"
    assert not any(e.startswith("assumption:emission.transport") for e in moved["evidence"])


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
    # 트럭 운송 계수 × 월 발생량(0.6 t) × 공장 → 업체 거리(직선 × 우회 계수 1.3)
    km = claim(r, entry["transportKm"])["value"]
    assert km == pytest.approx(haversine_km(ORIGIN, vendor()["fleet"]["depot"]) * 1.3, rel=1e-6)
    assert moved == pytest.approx(TRUCK * 0.6 * km / 1000, abs=1e-6)
    assert claim(r, out["totals"]["carbon"])["value"] == pytest.approx(total, abs=1e-5)
    value = claim(r, entry["carbonValue"])["value"]
    assert value == pytest.approx(-total * price["value"], rel=1e-5)
    assert_grounded(reg, r)


def test_far_vendor_loses_a_tie_on_transport_emissions():
    """같은 단가·같은 조건이면 차고지가 가까운 업체를 고른다(전에는 업체 ID 순서로 먼 곳을 골랐다)."""
    reg = _seeded()
    near, far = vendor(), _second_vendor(reg, [129.3, 35.5])  # far가 ID 순서로 앞선다
    kwargs = {"streams": [stream()], "vendors": [near, far], "origin": ORIGIN}
    without = run(reg, "optimize_assignment", carbonWeight=0, **kwargs)
    assert without["output"]["assignments"][0]["vendorOrgId"] == VENDOR2
    assert without["output"]["carbonInObjective"] is False
    chosen = run(reg, "optimize_assignment", **kwargs)
    assert chosen["output"]["assignments"][0]["vendorOrgId"] == VENDOR
    assert chosen["output"]["greenest"] == {"same": True, "changes": []}
    assert_grounded(reg, chosen)


def test_greenest_assignment_is_compared_when_money_wins():
    """먼 업체가 더 많이 쳐주면 돈으로 고르고, 탄소를 가장 줄이는 배정(가까운 업체)과의 차이를 남긴다."""
    reg = _seeded()
    near, far = vendor(), _second_vendor(reg, [129.3, 35.5], price=340)
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[near, far], origin=ORIGIN)
    out = r["output"]
    assert out["assignments"][0]["vendorOrgId"] == VENDOR2
    green = out["greenest"]
    assert green["same"] is False
    assert green["changes"] == [{"streamId": "st_sprue", "vendorOrgId": VENDOR, "route": "sell"}]
    money_gap = claim(r, green["moneyGap"])["value"]
    carbon_gap = claim(r, green["carbonGap"])["value"]
    assert money_gap == pytest.approx(-(340 - 320) * 600)
    assert carbon_gap < 0
    # 고른 배정은 돈 차이가 내부 탄소 가격으로 환산한 탄소 차이보다 크기 때문이다
    price = claim(r, out["carbonPrice"])["value"]
    assert -money_gap > -carbon_gap * price
    assert_grounded(reg, r)


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
