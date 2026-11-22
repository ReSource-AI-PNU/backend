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


def test_complex_stats_show_official_totals_for_emitter_complex():
    # 같은 단지 후보 머리에 단지 전체 폐합성수지 집계(2024 통계)를 근거가 열리는 값으로 싣는다
    app = make_app()
    view = to_approval(app)
    partners = view["result"]["complexPartners"]
    assert partners["complex"]["id"] == "noksan"
    stats = partners["stats"]
    assert stats["sheetName"] == "(국가)부산 명지녹산국가산업단지" and stats["dataYear"] == "2024"
    values = stats["values"]
    assert values["generated"]["value"] == 8029.59 and values["generated"]["unit"] == "톤/년"
    assert values["recycled"]["value"] == 6554.13 and values["incinerated"]["value"] == 1435.27
    evidence = app.registry.get(values["generated"]["evidence"][0], HANBIT)
    assert (
        evidence.kind == "ref" and "명지녹산" in evidence.excerpt and "8,029.59" in evidence.excerpt
    )
    # 업체 뷰에는 같은 단지 후보가 없다
    vendor = service.get_view(app, view["session"]["id"], SAEGIL)
    assert "complexPartners" not in (vendor.get("result") or {})
