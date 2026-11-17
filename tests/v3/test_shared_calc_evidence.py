"""상대 조직과 나누는 계산 근거에 배출 기업만의 값이 없다(평가 L6c 발견의 회귀 시험).

배출 기업이 부른 판단 도구의 calc 근거는 대화 상대 업체에게도 열린다. 예전에는 근거의 meta.inputs에
흐름 레코드가 통째로 실려 현행 처리 방법·단가, 전처리 인건비가 업체의 근거 조회·검색으로 보였고,
calc ID의 지문도 그 값까지 넣어 만들었다. 회사, 업체, 수치는 모두 가상이다.
"""

from __future__ import annotations

import json

import pytest

from app.v3 import service
from app.v3.agents.pool import is_private
from app.v3.evidence import search
from app.v3.judge import economics, route
from app.v3.judge.common import (
    EMITTER_ONLY_KEYS,
    PRIVATE_CHECKS,
    shareable,
    without_emitter_only,
)
from tests.v3.runtime_fixtures import HANBIT, MISO, NAMHAE, SAEGIL, TODAY, make_app, start

VENDORS = (SAEGIL, NAMHAE, MISO)
METHOD = "위탁 열분해(가상)"  # 시연 데이터에 없는 현행 처리 방법 이름
COST = 173.5  # 담당자가 자료 화면에서 넣은 현행 단가(가상)
ANSWERED = 187.25  # 담당자가 질문에 답한 현행 처리비(가상)
HOURS, RATE = 7.5, 13579  # 전처리 작업 시간과 시간당 인건비(가상)
SECRETS = (METHOD, "173.5", "187.25", "13579", "101842")  # 101842.5 = 7.5 × 13579


@pytest.fixture(scope="module")
def world():
    app = make_app()
    service.update_stream(
        app,
        HANBIT,
        "st_film",
        {"currentTreatment": {"method": METHOD, "unitCostKrwPerKg": COST}},
        HANBIT,
    )
    service.update_stream(
        app,
        HANBIT,
        "st_sprue",
        {"pretreatHoursPerMonth": HOURS, "laborCostKrwPerHour": RATE},
        HANBIT,
    )
    view = start(app)
    for _ in range(4):
        if view["session"]["status"] != "awaiting_answers":
            break
        sid = view["session"]["id"]
        for question in view["questions"]:
            if question["answered"]:
                continue
            value = {"moisturePct": 2.0, "currentCost": ANSWERED}.get(question["field"], "unknown")
            service.answer_question(app, sid, question["id"], value, HANBIT)
        view = service.run_session(app, sid, HANBIT)
    assert view["session"]["status"] == "awaiting_approval"
    answered = [
        s for s in app.store.list_streams(HANBIT) if (s.get("answers") or {}).get("currentCost")
    ]
    assert answered, "현행 처리비 질문에 답한 흐름이 있어야 시험이 뜻이 있다"
    return app, view


def test_vendors_see_no_private_emitter_value_through_evidence(world):
    app, _ = world
    for vendor in VENDORS:
        items = app.registry.list(org_id=vendor)
        assert items
        served = [search.shown(e, vendor) for e in items]
        served += [service.get_evidence(app, e.id, vendor) for e in items if e.kind == "calc"]
        served += service.search_evidence(app, "", None, vendor)
        text = json.dumps(served, ensure_ascii=False)
        assert not [s for s in SECRETS if s in text], vendor
        for item in items:
            # 저장된 근거 자체에도 없다(조회 경로가 가리는 것에 기대지 않는다)
            raw = json.dumps(item.meta.get("inputs") or {}, ensure_ascii=False)
            if (item.meta.get("inputs") or {}).get("orgId") == vendor:
                continue
            assert not [k for k in EMITTER_ONLY_KEYS if f'"{k}"' in raw], item.id
            assert '"currentCost"' not in raw, item.id
            if item.kind == "calc":
                # 업체가 열 수 있는 배출 기업의 계산 근거에 현행 처리·인건비에서 나온 값이 없다
                key = item.id.rsplit(":", 1)[-1]
                assert not is_private(key), item.id
                assert key not in ("pretreatHours", "laborRate", "monthlyLabor"), item.id
                assert not [c for c in item.meta.get("checks") or [] if c["key"] in PRIVATE_CHECKS]


def test_vendor_chains_carry_no_id_of_an_owner_only_calc(world):
    app, _ = world
    for vendor in VENDORS:
        for item in app.registry.list(kinds=["calc"], org_id=vendor):
            chain = service.get_evidence(app, item.id, vendor)["chain"]
            for link in chain:
                if link.get("hidden"):
                    assert not link["id"].startswith("calc:"), (item.id, link["id"])


def test_the_emitter_still_sees_its_inputs_and_private_values(world):
    app, _ = world
    mine = app.registry.list(kinds=["calc"], org_id=HANBIT)
    text = json.dumps([search.shown(e, HANBIT) for e in mine], ensure_ascii=False)
    assert METHOD in text and "13579" in text
    keys = {e.id.rsplit(":", 1)[-1] for e in mine}
    assert {"currentUnitCost", "monthlyLabor", "carbonDifference"} <= keys
    found = service.get_evidence(
        app, next(e.id for e in mine if e.id.endswith(":monthlyLabor")), HANBIT
    )
    assert found["evidence"]["meta"]["inputs"]["orgId"] == HANBIT
    assert found["evidence"]["access"] == [HANBIT]


def test_vendors_do_not_get_the_answered_current_cost_with_the_stream(world):
    app, view = world
    for vendor in VENDORS:
        text = json.dumps(service.list_streams(app, HANBIT, vendor), ensure_ascii=False)
        assert "currentCost" not in text and "187.25" not in text
        theirs = service.get_view(app, view["session"]["id"], vendor)
        for stream in theirs["streams"]:
            assert "currentCost" not in stream["unknownFields"]
    stream = {
        "id": "st_x",
        "answers": {"currentCost": 1.0, "moisturePct": 2.0, "evidence": ["human:a:b"]},
        "unknownFields": ["currentCost", "form"],
        "currentTreatment": {"method": "x"},
    }
    shared = without_emitter_only(stream)
    assert shared["answers"] == {"moisturePct": 2.0, "evidence": ["human:a:b"]}
    assert shared["unknownFields"] == ["form"] and "currentTreatment" not in shared
    assert stream["answers"]["currentCost"] == 1.0  # 원본은 그대로


def test_shared_calc_ids_do_not_depend_on_values_the_vendor_cannot_see():
    """ID의 지문이 현행 단가를 넣어 만들어지면, 업체가 단가를 바꿔 가며 ID를 맞혀 볼 수 있다."""
    vendor = {
        "orgId": "org_v",
        "permits": [{"wasteItem": "폐플라스틱", "materials": ["PP"]}],
        "criteria": [
            {
                "id": "pc",
                "material": "PP",
                "form": None,
                "grade": "B",
                "maxContaminationPct": 8,
                "maxMoisturePct": 2,
                "unitPriceKrwPerKg": 260,
                "minMonthlyKg": 100,
            }
        ],
        "fleet": {"capacityKg": 1000, "costPerKm": 1500},
    }

    def stream(cost):
        return {
            "id": "st_a",
            "orgId": "org_e",
            "name": "PP 압축품",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "압축",
            "monthlyKg": 500,
            "monthlyKgBasis": "measured",
            "measure": {"contaminationPct": 1.0, "moisturePct": 0.3},
            "currentTreatment": {"method": "위탁 소각", "unitCostKrwPerKg": cost},
            "pretreatHoursPerMonth": 2,
            "laborCostKrwPerHour": 10000,
        }

    from app.v3.evidence.registry import InMemoryRegistry

    registry = InMemoryRegistry()
    runs = []
    for cost in (210, 230):
        for tool in (route.judge_route, economics.cost_carbon):
            result = tool(registry, org_id="org_e", today=TODAY, stream=stream(cost), vendor=vendor)
            runs.append((cost, tool.__name__, result))
    for name in ("judge_route", "cost_carbon"):
        (_, _, low), (_, _, high) = [r for r in runs if r[1] == name]
        shared_low = [e for e in low["evidence"] if "org_v" in registry.get(e).access]
        shared_high = [e for e in high["evidence"] if "org_v" in registry.get(e).access]
        assert shared_low and shared_low == shared_high, name
        private_low = set(low["evidence"]) - set(shared_low)
        private_high = set(high["evidence"]) - set(shared_high)
        assert private_low and private_low.isdisjoint(private_high), name
        for evidence_id in shared_low:
            record = registry.get(evidence_id)
            assert record.meta["inputs"] == shareable(record.meta["inputs"])
            assert "costPerKm" not in json.dumps(record.meta["inputs"])
        for evidence_id in private_low:
            assert registry.get(evidence_id).access == ("org_e",)
