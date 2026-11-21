"""허가 근거 수준과 운송 배출이 최종 판단(배정, 법령 요건)에 들어가는지.

2026-11-19 평가 의견: "허가 근거의 수준과 수거 거리의 환경 영향이 최종 판단에 충분히 반영되지 않는다".
"""

import pytest

from app.v3.evidence.model import Evidence
from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from app.v3.intake.documents import business_names, profile_from_fields
from app.v3.judge.permits import permit_level, registry_records
from tests.v3.judge_fixtures import (
    EMITTER,
    ORIGIN,
    TODAY,
    VENDOR,
    VENDOR2,
    assert_grounded,
    registry,
    run,
)
from tests.v3.judge_fixtures import (
    stream as base_stream,
)
from tests.v3.judge_fixtures import (
    vendor as base_vendor,
)

# 공공 허가 명부(registry.v1.json)에 있는 실제 업체 두 곳. 상호와 좌표만 빌려 대조를 시험한다
PLASTIC_NAME, PLASTIC_AT = "㈜우리환경산업", [129.12468, 35.229955]  # 51-03-01, 중간재활용업
TIRE_NAME, TIRE_AT = "감만타이어", [129.090554, 35.11976]  # 51-15-02 폐타이어만
TRUCK = 0.192419625  # 환경성적표지 평가계수 · 육상수송 트럭(kgCO2e/ton·km)


def stream(**over):
    return base_stream(
        **{
            "currentTreatment": {"method": "소각", "unitCostKrwPerKg": 250},
            "pretreatHoursPerMonth": 0,
            **over,
        }
    )


def vendor(**over):
    return base_vendor(
        **{
            "process": "재활용",
            "transport": {"status": "visit", "basis": "included"},
            **over,
        }
    )


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


# ── 허가 확인 상태(원문 대조 기록이 없으면 대조 완료가 아니다) ─────────────────


def test_legacy_document_permit_is_not_verified():
    """업체가 예전 방식으로 허가증에서 읽어 확인한 값(doc 근거)만 있고 원문 대조 기록이 없다."""
    reg = registry()
    level = permit_level(reg, stream(), vendor(name="(주)새길자원"))
    assert level["level"] == "registered" and level["status"] == "conditional"
    assert level["source"] == "document"
    assert "담당자의 원문 대조 기록도 없습니다" in level["text"]
    assert level["todo"]["items"]


def test_vendor_typed_permit_needs_original():
    reg = registry()
    level = permit_level(reg, stream(), _human_permit_vendor(reg))
    assert level["level"] == "registered" and level["status"] == "conditional"
    assert level["source"] == "typed" and level["label"] == "등록 품목 일치 · 허가증 원본 확인 필요"


def test_business_must_fit_the_vendor_process():
    """업체가 확인한 허가증 업종이 처리 방식과 맞지 않으면(소각인데 재활용업) 진행 불가다."""
    reg = registry()
    burner = vendor(process="소각")
    burner["permits"][0]["business"] = "폐기물종합재활용업"
    level = permit_level(reg, stream(), burner)
    assert level["level"] == "mismatch" and level["status"] == "unmet"
    assert "처분업" in level["text"]
    burner["permits"][0]["business"] = "폐기물중간처분업"
    assert permit_level(reg, stream(), burner)["level"] == "registered"


def test_registry_match_is_only_a_candidate():
    """공공 명부의 같은 상호(차고지 근처)는 원본 확인 필요에 머문다. 차고지가 없으면 명부를 쓰지 않는다."""
    reg = registry()
    near = vendor(name=PLASTIC_NAME, fleet={**vendor()["fleet"], "depot": PLASTIC_AT})
    near["permits"][0]["evidence"] = ["human:x:허가품목"]
    level = permit_level(reg, stream(), near, TODAY)
    assert level["level"] == "registered" and level["status"] == "conditional"
    assert level["source"] == "registry"
    assert "부산광역시 금정구청이 올린" in level["text"] and "후보 자료" in level["text"]
    ref = next(e for e in level["evidence"] if e.startswith("ref:허가명부:"))
    found = reg.get(ref, EMITTER)
    assert found.kind == "ref" and "51-03-01" in found.excerpt and "*" in found.access
    assert "명부 기준일 2026-07-21" in found.excerpt
    assert "한 해 넘게 지났습니다" in permit_level(reg, stream(), near, "2027-12-01")["text"]
    # 같은 상호라도 차고지에서 먼 사업장, 차고지를 모르는 업체는 명부와 대조하지 않는다
    assert registry_records(PLASTIC_NAME, [126.97, 37.56]) == []
    assert registry_records(PLASTIC_NAME, None) == []
    far = {**near, "fleet": {**vendor()["fleet"], "depot": [126.97, 37.56]}}
    assert permit_level(reg, stream(), far)["source"] == "typed"


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
    assert item["status"] == "conditional"
    assert item["note"].startswith("(주)새길자원: 등록 품목 일치 · 허가증 원본 확인 필요")
    typed = {**_human_permit_vendor(reg), "name": "(주)새길자원"}
    r = run(reg, "check_requirements", stream=stream(), route="sell", vendors=[typed])
    item = next(i for i in r["output"]["items"] if i["decidedBy"] == "permit_match")
    assert item["status"] == "conditional" and item["permit"]["source"] == "typed"
    assert r["status"] == "conditional"
    assert_grounded(reg, r)
    tire = {**vendor(fleet={**vendor()["fleet"], "depot": TIRE_AT}), "name": TIRE_NAME}
    r = run(reg, "check_requirements", stream=stream(), route="sell", vendors=[tire])
    item = next(i for i in r["output"]["items"] if i["decidedBy"] == "permit_match")
    assert item["status"] == "unmet" and "맡기지 않습니다" in item["note"]


def test_assignment_with_vendor_typed_permit_is_conditional():
    reg = _seeded()
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[_human_permit_vendor(reg)])
    entry = r["output"]["assignments"][0]
    assert entry["vendorOrgId"] == VENDOR
    assert entry["permit"]["level"] == "registered" and entry["status"] == "conditional"
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
    assert excluded[0]["permit"]["label"] == "이 경로로 진행 불가"
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


def test_truck_factor_is_retained_but_unverified_boundary_is_not_calculated():
    reg = registry()
    r = run(reg, "cost_carbon", stream=stream(), vendor=vendor(), origin=ORIGIN)
    factor = reg.get("ref:환경성적표지:트럭-운송@2024-09", EMITTER)
    assert factor.value == pytest.approx(TRUCK) and factor.unit == "kgCO2e/tkm"
    assert r["output"]["carbonTotal"] is None
    assert not any(c["id"] == "transportCarbon" for c in r["claims"])


def test_unverified_transport_disables_carbon_objective():
    reg = _seeded()
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()], origin=ORIGIN)
    out = r["output"]
    assert out["carbonInObjective"] is False
    entry = out["assignments"][0]
    assert entry["carbonProcess"] is None and entry["carbonTransport"] is None
    assert entry["carbon"] is None and out["greenest"] is None
    assert_grounded(reg, r)


def test_centroid_distance_cannot_break_a_tie_as_verified_carbon():
    """동일 금액에서 중심점 거리만으로 탄소 최적 업체를 주장하지 않는다."""
    reg = _seeded()
    near, far = vendor(), _second_vendor(reg, [129.3, 35.5])  # far가 ID 순서로 앞선다
    kwargs = {"streams": [stream()], "vendors": [near, far], "origin": ORIGIN}
    without = run(reg, "optimize_assignment", carbonWeight=0, **kwargs)
    assert without["output"]["assignments"][0]["vendorOrgId"] == VENDOR2
    assert without["output"]["carbonInObjective"] is False
    chosen = run(reg, "optimize_assignment", **kwargs)
    assert chosen["output"]["assignments"][0]["vendorOrgId"] == VENDOR2
    assert chosen["output"]["greenest"] is None
    assert_grounded(reg, chosen)


def test_money_comparison_does_not_invent_a_greenest_alternative():
    """확인된 금액은 비교하되 미산정 탄소 차이는 만들지 않는다."""
    reg = _seeded()
    near, far = vendor(), _second_vendor(reg, [129.3, 35.5], price=340)
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[near, far], origin=ORIGIN)
    out = r["output"]
    assert out["assignments"][0]["vendorOrgId"] == VENDOR2
    assert out["greenest"] is None and out["carbonInObjective"] is False
    assert_grounded(reg, r)


def test_without_origin_transport_emissions_are_flagged_not_guessed():
    reg = _seeded()
    r = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()])
    entry = r["output"]["assignments"][0]
    assert entry["carbonTransport"] is None and entry["carbonProcess"] is None
    check = next(c for c in r["checks"] if c["key"] == "transport_carbon")
    assert check["status"] == "conditional" and "공장 위치" in check["text"]


def test_cost_carbon_reports_transport_emissions_for_alternatives():
    reg = _seeded()
    r = run(reg, "cost_carbon", stream=stream(), vendor=vendor(), origin=ORIGIN)
    out = r["output"]
    assert out["carbonTotal"] is None
    assert not any(c["id"] in ("carbonDifference", "transportCarbon") for c in r["claims"])
    assert_grounded(reg, r)
