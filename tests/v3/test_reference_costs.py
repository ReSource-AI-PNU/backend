"""공식 처리단가 고시(참고값). 결과에 보이기만 하고 현행 처리비와 손익 계산에는 쓰지 않는다(2026-11-22)."""

from __future__ import annotations

from app.v3 import service
from app.v3.evidence.acceptance import usable_basis
from app.v3.judge import basis
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app, to_approval

GOSI = "ref:처리단가고시:폐합성수지-처분업@2025-10-01"


def test_reference_cost_is_checked_official_ref():
    app = make_app()
    evidence = app.registry.get(GOSI, HANBIT)
    assert evidence.kind == "ref" and evidence.value == 297000 and evidence.unit == "원/톤"
    assert usable_basis(evidence)
    assert "제2025-165호" in evidence.source
    found = basis.reference_costs(app.registry, HANBIT)
    assert [item["evidence"].id for item in found] == [GOSI]


def test_result_shows_reference_cost_without_changing_money():
    app = make_app()
    view = to_approval(app)
    result = view["result"]
    costs = result["referenceCosts"]
    assert len(costs) == 1
    claim = costs[0]["cost"]
    assert claim["value"] == 297000 and claim["unit"] == "원/톤" and claim["evidence"] == [GOSI]
    assert claim["role"] == "referenceCost"
    assert "손익 계산에는 쓰지 않았습니다" in costs[0]["note"]
    # 현행 처리비는 회사 입력 그대로다(고시값이 대신하지 않는다)
    for assignment in result["assignments"]:
        current = assignment.get("currentUnitCost") or {}
        assert current.get("value") != 297 and GOSI not in (current.get("evidence") or [])
    # 업체가 보는 결과에는 싣지 않는다
    vendor = service.get_view(app, view["session"]["id"], SAEGIL)
    assert "referenceCosts" not in (vendor.get("result") or {})
