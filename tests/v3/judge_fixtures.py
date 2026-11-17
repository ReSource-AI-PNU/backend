"""판단 도구 테스트용 가상 데이터. 회사와 수치는 모두 가상이다."""

from __future__ import annotations

import copy

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.validator import validate_text
from app.v3.judge import basis, defaults
from app.v3.judge.toolbox import call_tool

TODAY = "2026-11-17"
EMITTER, OTHER, VENDOR, VENDOR2 = "org_hanbit", "org_other", "org_saegil", "org_miso"
ORIGIN = [128.85, 35.09]


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
