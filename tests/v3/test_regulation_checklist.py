"""③ 규정 확인: 경로별 요건 체크리스트(judge/requirements.v1.json)와 관련 조항 검색(2026-11-19)."""

import pytest

from app.v3 import service
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import load_seed_dir
from app.v3.evidence.validator import validate_text
from app.v3.judge import regulation
from tests.v3.runtime_fixtures import HANBIT, NAMHAE, approve_all, make_app, to_approval

TODAY = "2026-11-17"
ROUTES = {"sell", "treat", "keep", "reuse"}
STREAM = {
    "id": "st_x",
    "orgId": HANBIT,
    "name": "불량 사출품",
    "wasteItem": "폐플라스틱",
    "material": "PP",
    "form": "덩어리",
}


@pytest.fixture(scope="module")
def registry():
    found = InMemoryRegistry()
    load_seed_dir(found)
    return found


def test_every_requirement_quotes_the_law_word_for_word(registry):
    items = regulation.requirements()
    assert {route for item in items for route in item["routes"]} == ROUTES
    for route in ROUTES:
        assert 3 <= sum(route in item["routes"] for item in items) <= 8
    for item in items:
        assert item["decidedBy"] in ("permit_match", "staff")
        assert item["title"] and item["action"].endswith("다.")
        # 코드가 쓰는 문장에는 숫자를 쓰지 않는다(규칙 1). 숫자는 인용한 조문이 보여 준다
        for field in ("title", "action", "applies"):
            assert not any(ch.isdigit() for ch in item.get(field) or ""), item[field]
        for clause in item["clauses"]:
            law = registry.get(clause["evidenceId"])
            assert law.kind == "law" and clause["quote"] in law.excerpt, clause
            assert 10 <= len(clause["quote"]) <= 300


def vendor(permitted: bool):
    return {
        "orgId": "org_v",
        "name": "(주)시험자원",
        "permits": [
            {
                "wasteItem": "폐플라스틱" if permitted else "폐목재",
                "materials": ["PP"] if permitted else ["목재"],
                "evidence": ["doc:org_v:허가증:폐기물처리업"],
            }
        ],
    }


def items_by_key(result):
    return {item["key"]: item for item in result["output"]["items"]}


def test_permit_is_decided_by_the_vendors_talked_to_and_the_rest_are_staff_tasks(registry):
    met = regulation.check_requirements(
        registry, org_id=HANBIT, today=TODAY, stream=STREAM, route="sell", vendors=[vendor(True)]
    )
    assert met["status"] == "calculated"
    found = items_by_key(met)
    assert found["permit_holder"]["status"] == "met"
    assert "(주)시험자원" in found["permit_holder"]["note"]
    staff = [i for i in found.values() if i["decidedBy"] == "staff"]
    assert staff and all(i["status"] == "conditional" for i in staff)
    assert "circular_resource" in found  # 매각에만 걸리는 요건
    # 조항 인용은 claim이고 검증기를 통과한다
    claims = met["claims"]
    assert claims and all(c["kind"] == "clause" for c in claims)
    text = " ".join("{{" + c["id"] + "}}" for c in claims)
    assert validate_text(text, claims, registry, org_id=HANBIT, today=TODAY).ok

    unmet = regulation.check_requirements(
        registry, org_id=HANBIT, today=TODAY, stream=STREAM, route="sell", vendors=[vendor(False)]
    )
    assert items_by_key(unmet)["permit_holder"]["status"] == "unmet"
    assert unmet["status"] == "conditional"

    unknown = regulation.check_requirements(
        registry, org_id=HANBIT, today=TODAY, stream=STREAM, route="keep"
    )
    assert items_by_key(unknown)["permit_holder"]["status"] == "conditional"
    assert "recycle_first" in items_by_key(unknown)  # 소각을 이어 가면 재활용 먼저


def test_reuse_has_its_own_requirements_and_related_clauses_are_new(registry):
    reuse = regulation.check_requirements(
        registry, org_id=HANBIT, today=TODAY, stream=STREAM, route="reuse"
    )
    keys = set(items_by_key(reuse))
    assert {"self_recycling_standard", "self_recycling_ledger"} <= keys
    assert "permit_holder" not in keys and reuse["status"] == "calculated"
    related = reuse["output"]["related"]
    assert 1 <= len(related) <= 3
    cited = {e for i in reuse["output"]["items"] for e in i["evidence"]}
    assert not {r["evidenceId"] for r in related} & cited


def test_result_view_document_and_graph_carry_the_checklist():
    app = make_app()
    view = to_approval(app)
    sections = view["result"]["regulation"]
    assert {s["streamId"] for s in sections} == {s["id"] for s in view["streams"]}
    for section in sections:
        assert section["route"] in ("sell", "treat", "keep")
        permit = next(i for i in section["items"] if i["key"] == "permit_holder")
        assert permit["kind"] == "check" and permit["clauses"]
        assert all(c["label"] for i in section["items"] for c in i["clauses"])
    # 처리 업체에게는 배출 기업의 규정 확인을 보이지 않는다
    vendor_view = service.get_view(app, view["session"]["id"], NAMHAE)
    assert "regulation" not in (vendor_view["result"] or {})
    # 판단 그래프에 요건이 조항 노드로 이어진다
    regulation_edges = [e for e in view["graph"]["edges"] if e.get("kind") == "regulation"]
    assert any(e["status"] == "met" for e in regulation_edges)
    # 확정 뒤 이행 계획서에 요건과 조항 인용이 실린다
    done = approve_all(app, view)
    plans = [d for d in done["documents"] if d["kind"] == "plan"]
    assert plans and all(d["validation"]["ok"] for d in plans)
    text = plans[0]["rendered"]
    assert "지켜야 할 법령 요건" in text and "올바로" in text
