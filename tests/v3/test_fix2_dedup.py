"""그래프 엣지와 claim의 근거 목록에 같은 ID가 두 번 실리지 않는다(순서 유지). 수치는 모두 가상이다."""

from __future__ import annotations

from app.v3 import service
from app.v3.a2a.message import Claim
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    approve_all,
    make_app,
    to_approval,
)

EVIDENCE_KEYS = ("evidence", "reasonEvidence", "inputEvidence")


def evidence_lists(node, path="view"):
    """(자리, 근거 ID 목록). 뷰 안의 evidence 꼴 목록을 모두 찾는다."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if (
                key in EVIDENCE_KEYS
                and isinstance(value, list)
                and all(isinstance(x, str) for x in value)
            ):
                yield here, value
            yield from evidence_lists(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from evidence_lists(value, f"{path}[{index}]")


def test_no_duplicate_evidence_ids_anywhere_in_any_view():
    app = make_app()
    view = approve_all(app, to_approval(app))
    session_id = view["session"]["id"]
    seen = 0
    for viewer in (HANBIT, SAEGIL, NAMHAE, MISO):
        for where, found in evidence_lists(service.get_view(app, session_id, viewer)):
            seen += 1
            assert len(found) == len(set(found)), (viewer, where)
    assert seen > 100


def test_graph_edges_keep_first_seen_order_without_repeats():
    app = make_app()
    view = to_approval(app)
    edges = [e for e in view["graph"]["edges"] if e["kind"] == "criterion" and e["evidence"]]
    assert edges
    for edge in edges:
        assert len(edge["evidence"]) == len(set(edge["evidence"]))
    sprue = next(e for e in edges if e["from"] == "st_sprue" and e["to"] == "pc_saegil_pp_a")
    # 허가증이 먼저, 그다음 매입 기준표, 그다음 측정값(검사 순서 그대로)
    assert sprue["evidence"][0] == "doc:org_saegil:허가증:폐플라스틱"
    assert sprue["evidence"].index("doc:org_saegil:매입기준표:pc_saegil_pp_a-단가") < sprue[
        "evidence"
    ].index("measure:org_hanbit:st_sprue:오염도@2026-11-10")


def test_claim_drops_repeated_evidence_ids_but_keeps_the_source_first():
    claim = Claim("c1", "number", 1.0, ("calc:a", "doc:b", "calc:a", "doc:b", "doc:c"), "kg")
    assert claim.evidence == ("calc:a", "doc:b", "doc:c")
    again = Claim.from_dict({**claim.to_dict(), "evidence": ["calc:a", "calc:a"]})
    assert again.to_dict()["evidence"] == ["calc:a"]
