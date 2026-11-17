"""업체가 보는 판단 그래프에는 자기 매입 기준 노드와, 흐름과 그 기준을 대조한 엣지가 있다. 수치는 가상이다."""

from __future__ import annotations

import json

from app.v3 import service
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    approve_all,
    make_app,
    to_approval,
)

VENDORS = (SAEGIL, NAMHAE, MISO)


def edges_of(graph, kind):
    return [e for e in graph["edges"] if e["kind"] == kind]


def test_vendor_graph_has_its_own_criteria_and_the_comparison_edges():
    app = make_app()
    view = to_approval(app)
    session_id = view["session"]["id"]
    graph = service.get_view(app, session_id, SAEGIL)["graph"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    own = {c["id"] for c in app.store.get_vendor_profile(SAEGIL)["criteria"]}
    criteria = {i for i, n in nodes.items() if n["type"] == "criterion"}
    assert criteria == {"pc_saegil_pp_a", "pc_saegil_pp_b", "pc_saegil_film_b"} and criteria <= own
    assert nodes["pc_saegil_pp_a"]["label"] == "PP · A등급"
    assert nodes["pc_saegil_film_b"]["label"] == "LDPE · B등급 · 필름"
    compared = {(e["from"], e["to"]): e for e in edges_of(graph, "criterion")}
    # 흐름 → 기준(대조 결과), 기준 → 업체(매입 기준표)
    assert compared[("st_sprue", "pc_saegil_pp_a")]["status"] == "met"
    assert compared[("st_sprue", "pc_saegil_pp_a")]["label"] == "매입 기준 충족"
    assert compared[("st_film", "pc_saegil_film_b")]["status"] == "conditional"
    assert compared[("st_defect", "pc_saegil_pp_b")]["status"] == "conditional"
    for criterion in criteria:
        assert compared[(criterion, SAEGIL)]["label"] == "매입 기준표"
    assigned = {(e["from"], e["to"]) for e in edges_of(graph, "assignment")}
    assert assigned == {("st_sprue", SAEGIL), ("st_film", SAEGIL)}
    # 엣지의 근거는 업체가 열 수 있는 것뿐이고 자기 기준표 근거가 들어 있다
    sprue = compared[("st_sprue", "pc_saegil_pp_a")]["evidence"]
    assert "doc:org_saegil:매입기준표:pc_saegil_pp_a-단가" in sprue
    for edge in graph["edges"]:
        assert len(edge["evidence"]) == len(set(edge["evidence"]))
        for evidence_id in edge["evidence"]:
            app.registry.get(evidence_id, SAEGIL)  # 볼 수 없으면 예외


def test_each_vendor_sees_only_its_own_criteria_and_nothing_about_current_treatment():
    app = make_app()
    view = approve_all(app, to_approval(app))
    session_id = view["session"]["id"]
    mine = service.get_view(app, session_id, HANBIT)["graph"]
    for vendor in VENDORS:
        graph = service.get_view(app, session_id, vendor)["graph"]
        own = {c["id"] for c in app.store.get_vendor_profile(vendor)["criteria"]}
        criteria = {n["id"] for n in graph["nodes"] if n["type"] == "criterion"}
        assert criteria and criteria <= own, vendor
        assert {n["id"] for n in graph["nodes"] if n["type"] == "vendor"} == {vendor}
        text = json.dumps(graph, ensure_ascii=False)
        assert "현행" not in text and "소각" not in text and "regulation" not in text
        # 같은 판단 도구의 결과다. 흐름과 기준의 짝은 배출 기업이 보는 그래프와 같다
        pairs = {(e["from"], e["to"]) for e in edges_of(graph, "criterion")}
        theirs = {
            (e["from"], e["to"])
            for e in edges_of(mine, "criterion")
            if vendor
            in (
                e["to"],
                *[x["to"] for x in edges_of(mine, "criterion") if x["from"] == e["to"]],
            )
        }
        assert pairs == theirs, vendor
        by_pair = {(e["from"], e["to"]): e for e in edges_of(mine, "criterion")}
        for edge in edges_of(graph, "criterion"):
            # 업체가 보는 대조 결과가 배출 기업 쪽보다 좋게 보이지 않는다(불충족은 그대로 불충족)
            if by_pair[(edge["from"], edge["to"])]["status"] == "unmet":
                assert edge["status"] == "unmet"


def test_vendor_graph_does_not_depend_on_the_vendor_agents_memory():
    app = make_app()
    view = to_approval(app)
    session_id = view["session"]["id"]
    before = service.get_view(app, session_id, MISO)["graph"]
    app.vendor_memo.clear()  # 서버를 다시 띄웠거나 업체 측이 다른 서비스에 있을 때
    assert service.get_view(app, session_id, MISO)["graph"] == before
    assert [n for n in before["nodes"] if n["type"] == "criterion"]


def test_a_stream_without_a_matching_criterion_points_at_the_vendor():
    app = make_app()
    view = to_approval(app)
    session = app.store.get_session(view["session"]["id"])
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == MISO and t["messages"])
    for entry in session["work"]["threadTools"][thread["id"]]:
        if entry["name"] == "judge_route" and entry["arguments"] == {"streamId": "st_sprue"}:
            # 맞는 기준이 없다고 판정된 경우의 도구 결과 모양(route.py)
            entry["result"]["output"].update(
                criterionId=None, reason="매입 기준이 없어 현행을 유지합니다."
            )
            entry["result"]["checks"] = [
                {"key": "permit", "status": "met", "text": "허가 품목이 맞습니다.", "evidence": []},
                {
                    "key": "criteria",
                    "status": "unmet",
                    "text": "이 재질의 매입 기준이 없습니다.",
                    "evidence": [],
                },
                {
                    "key": "current_cost",
                    "status": "conditional",
                    "text": "현행 처리 단가가 없어 현행 대비를 내지 못했습니다.",
                    "evidence": [],
                },
            ]
    app.store.put_session(session)
    theirs = service.get_view(app, session["id"], MISO)["graph"]
    edge = next(
        e for e in edges_of(theirs, "criterion") if (e["from"], e["to"]) == ("st_sprue", MISO)
    )
    assert edge["status"] == "unmet" and edge["label"] == "이 재질의 매입 기준이 없습니다."
    assert "현행" not in json.dumps(theirs, ensure_ascii=False)
    mine = service.get_view(app, session["id"], HANBIT)["graph"]
    edge = next(
        e for e in edges_of(mine, "criterion") if (e["from"], e["to"]) == ("st_sprue", MISO)
    )
    assert edge["label"] == "매입 기준이 없어 현행을 유지합니다."  # 배출 기업에게는 그대로
