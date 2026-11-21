"""판단 도구 테스트용 가상 데이터. 회사와 수치는 모두 가상이다."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from app.v3.evidence.validator import validate_text
from app.v3.judge import basis, defaults
from app.v3.judge.toolbox import call_tool

TODAY = "2026-11-17"
EMITTER, OTHER, VENDOR, VENDOR2 = "org_hanbit", "org_other", "org_saegil", "org_miso"
ORIGIN = [128.85, 35.09]
# 담당자가 답한 현행 처리(가상): 혼합 소각, kg당 250원
CURRENT_COST_ID = f"human:{EMITTER}:st_sprue-현행처리비"
CURRENT = {"method": "혼합 소각", "unitCostKrwPerKg": 250, "evidence": [CURRENT_COST_ID]}


def doc(eid, access, value=None, unit=None, excerpt="가상 문서"):
    return Evidence(
        id=eid,
        kind="doc",
        value=value,
        unit=unit,
        excerpt=excerpt,
        source="가상 시연 문서",
        as_of="2026-11-10",
        access=tuple(access),
    )


def registry():
    basis.reset()
    reg = InMemoryRegistry()
    defaults.register_defaults(reg, TODAY)
    load_seed_file(reg, SEED_DIR / "official.json")  # 공식 배출계수와 최저임금(누구나 본다)
    reg.put(doc(f"doc:{VENDOR}:허가증:품목", [VENDOR, EMITTER, OTHER]))
    reg.put(doc(f"doc:{VENDOR}:매입기준표:PP-A", [VENDOR, EMITTER, OTHER], 320, "원/kg"))
    reg.put(doc(f"doc:{VENDOR}:노선:차량", [VENDOR]))
    reg.put(doc(f"doc:{VENDOR2}:허가증:품목", [VENDOR2, EMITTER, OTHER]))
    reg.put(
        Evidence(
            id=f"measure:{EMITTER}:st_sprue:오염도@2026-11-10",
            kind="measure",
            value=1.0,
            unit="%",
            excerpt="가상 측정",
            source="측정값 CSV",
            as_of="2026-11-10",
            access=(EMITTER,),
        )
    )
    reg.put(doc(f"doc:{OTHER}:위탁계약서:단가", [OTHER], 400, "원/kg"))
    reg.put(
        Evidence(
            id=CURRENT_COST_ID,
            kind="human",
            value=250,
            unit="원/kg",
            excerpt="담당자가 질문에 답한 값: 스프루·런너 현행 처리비 250원/kg",
            source="담당자 질문 답변",
            as_of="2026-11-10",
            access=(EMITTER,),
        )
    )
    return reg


def stream(**over):
    base = {
        "id": "st_sprue",
        "orgId": EMITTER,
        "name": "스프루·런너",
        "process": "사출",
        "wasteItem": "폐플라스틱",
        "material": "PP",
        "origin": "PIR",
        "form": "분쇄",
        "monthlyKg": 600,
        "monthlyKgBasis": "measured",
        "bulkDensityKgPerM3": 250,
        "measure": {
            "contaminationPct": 1.0,
            "moisturePct": 0.3,
            "measuredOn": "2026-11-10",
            "evidence": [f"measure:{EMITTER}:st_sprue:오염도@2026-11-10"],
        },
    }
    base.update(over)
    return base


def answered_stream(**over):
    """담당자가 현행 처리(CURRENT)를 답한 흐름. 현행 처리비는 가정값으로 채우지 않으므로 현행 대비를 보는
    시험은 이것을 쓴다(over의 currentTreatment가 우선한다)."""
    return stream(**{"currentTreatment": dict(CURRENT), **over})


def vendor(**over):
    base = {
        "orgId": VENDOR,
        "permits": [
            {
                "wasteItem": "폐플라스틱",
                "materials": ["PP", "PE"],
                "evidence": [f"doc:{VENDOR}:허가증:품목"],
            }
        ],
        "criteria": [
            {
                "id": "pc_pp_a",
                "material": "PP",
                "grade": "A",
                "form": "분쇄",
                "maxContaminationPct": 2,
                "maxMoisturePct": 1,
                "unitPriceKrwPerKg": 320,
                "minMonthlyKg": 500,
                "evidence": [f"doc:{VENDOR}:매입기준표:PP-A"],
            }
        ],
        "fleet": {
            "depot": [128.9, 35.1],
            "capacityKg": 5000,
            "capacityM3": 20,
            "costPerKm": 1200,
            "evidence": [f"doc:{VENDOR}:노선:차량"],
        },
        "guardrails": {
            "floorPriceKrwPerKg": 250,
            "days": ["화", "목"],
            "minPickupsPerMonth": 2,
            "autoDecline": [],
        },
    }
    base.update(over)
    return base


def film_vendor():
    """필름 허가 품목이 없는 업체."""
    return vendor(
        orgId=VENDOR2,
        permits=[
            {
                "wasteItem": "금속 스크랩",
                "materials": [],
                "evidence": [f"doc:{VENDOR2}:허가증:품목"],
            }
        ],
        criteria=[],
    )


# 처리 배출계수 시드(환경성적표지)는 원문 경계를 확인하지 못해(metadata_only) 새 계산에 쓰지 않는다. 계산식
# 자체를 보는 시험만 아래 두 도우미로 "원문 확인 모의" 시험 전용(testOnly) 계수를 쓴다(evidence_fixtures와 같은 방식)
TEST_FACTOR_META = {
    "verificationStatus": "original_checked",
    "metric": "process_ghg",
    "datasetVersion": "synthetic-judge-v1",
    "functionalUnit": "kg waste input",
    "systemBoundary": "synthetic treatment only",
    "country": "TEST",
    "material": "test plastic",
    "evidenceClass": "assumption",
    "testOnly": True,
}


@pytest.fixture
def factor_gate(monkeypatch):
    """이 픽스처를 받는 시험에서만 testOnly 근거를 기준값으로 받아들인다(제품 코드는 그대로 막는다)."""
    original = basis.usable_basis
    monkeypatch.setattr(
        basis, "usable_basis", lambda e: bool(e.meta.get("testOnly")) or original(e)
    )


@pytest.fixture
def grade_gate(monkeypatch):
    """팀 등급 임계값(assumption:등급기준)은 공통 등급으로 쓰지 않는다(산정 보류). 등급 계산 경로 자체를 보는
    시험만 이 픽스처로 그 근거를 받아들인다(제품 코드는 그대로 막는다)."""
    from app.v3.evidence import acceptance
    from app.v3.intake import grade_model

    original = acceptance.usable_basis

    def allow(evidence):
        return evidence.id.startswith("assumption:등급기준") or original(evidence)

    monkeypatch.setattr(acceptance, "usable_basis", allow)
    monkeypatch.setattr(grade_model, "usable_basis", allow)


def with_test_factors(reg):
    """레지스트리의 처리 배출계수(kgCO2e/kg)를 시험 전용 원문 확인 모의 계수로 바꾼다. 값은 그대로다."""
    for factor in reg.list(org_id=None):
        if factor.unit == "kgCO2e/kg":
            reg.put(
                replace(
                    factor,
                    source="가상 시험 계수 (원문 확인 모의)",
                    meta={**factor.meta, **TEST_FACTOR_META},
                )
            )
    return reg


def origin_of(reg, evidence_id, org=EMITTER):
    """계산 근거가 고정해 둔 입력(…:snapshot:…)의 원래 근거 ID. 고정본이 아니면 그대로."""
    return reg.get(evidence_id, org).meta.get("originalId") or evidence_id


def run(reg, name, org=EMITTER, **args):
    return call_tool(name, copy.deepcopy(args), registry=reg, org_id=org, today=TODAY)


def claim(result, cid):
    return next(c for c in result["claims"] if c["id"] == cid)


def assert_grounded(reg, result, org=EMITTER):
    """도구 결과를 실제 근거 검증기에 통과시킨다.

    claims는 자리표시자만 늘어놓은 본문으로 검사하고(근거 존재·접근·값·인용),
    checks의 text와 ask는 claim 없이 검사한다(자리표시자 밖 숫자가 없어야 한다).
    """
    assert result["status"] in ("calculated", "conditional", "needs_information")
    body = " ".join("{{" + c["id"] + "}}" for c in result["claims"])
    checked = validate_text(body, result["claims"], reg, org_id=org, today=TODAY)
    assert checked.ok, checked.to_dict()
    for c in result["claims"]:
        if c["kind"] == "number":
            first = reg.get(c["evidence"][0], org)
            assert first.kind == "calc" and first.meta["tool"] == result["tool"]
    for check in result["checks"]:
        assert check["status"] in ("met", "conditional", "unmet")
        for sentence in (check["text"], check["ask"]):
            if sentence:
                plain = validate_text(sentence, [], reg, org_id=org, today=TODAY)
                assert plain.ok, (sentence, plain.to_dict())
