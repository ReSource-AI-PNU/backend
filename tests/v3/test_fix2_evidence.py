"""합계 계산 근거의 계산 사슬: 합계 → 흐름별 계산 → 원 근거. 회사와 수치는 모두 가상이다."""

from __future__ import annotations

from app.v3 import service
from app.v3.agents.vendor import public_profile
from app.v3.judge.toolbox import call_tool
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    TODAY,
    make_app,
    to_approval,
)

VENDORS = (SAEGIL, NAMHAE, MISO)


def test_totals_chain_to_the_per_stream_calculations():
    app = make_app()
    view = to_approval(app)
    result = view["result"]
    per_stream = {
        key: [a[key]["evidence"][0] for a in result["assignments"] if a.get(key)]
        for key in ("monthlyNow", "monthlyAfter", "difference")
    }
    # 열분해는 공정 에너지·수율·열분해유 용도 근거가 없어 처리 방식 탄소를 산정 보류한다(2026-11-19 자원순환
    # 효과 보강). 퍼지 덩어리는 운송 배출만 합계에 든다
    per_stream["carbon"] = [
        (a["carbon"] or a["carbonTransport"])["evidence"][0]
        for a in result["assignments"]
        if a.get("carbon") or a.get("carbonTransport")
    ]
    for key, claim in result["totals"].items():
        if key == "carbon":
            purge = next(a for a in result["assignments"] if a["streamId"] == "st_purge")
            assert purge["carbon"] is None and purge["carbonTransport"] is not None
            assert len(per_stream[key]) == 4
        total_id = claim["evidence"][0]
        assert total_id.startswith("calc:optimize_assignment:") and ":total" in total_id
        found = service.get_evidence(app, total_id, HANBIT)
        inputs = found["evidence"]["meta"]["inputEvidence"]
        # 합계의 입력 근거는 흐름별 계산 근거 전부다(빠짐도 덤도 없다)
        assert inputs and sorted(inputs) == sorted(per_stream[key]), key
        assert len(inputs) == len(set(inputs))
        chain = {item["id"]: item for item in found["chain"]}
        assert set(inputs) <= set(chain)
        # 사슬이 흐름별 계산을 거쳐 원 근거(문서·가정값)까지 이어진다
        kinds = {item["kind"] for item in chain.values()}
        assert kinds & {"doc", "assumption", "ref"}, (key, kinds)
        # 합계 값은 흐름별 값의 합과 같다
        parts = sum(chain[i]["value"] for i in inputs)
        assert abs(parts - claim["value"]) < 1e-6


def test_carbon_total_chains_to_the_per_stream_carbon_calculations():
    app = make_app()
    to_approval(app)  # 대화가 열려 업체의 매입 기준 근거가 배출 기업에 열린 뒤
    streams = [s for s in app.store.list_streams(HANBIT) if s["wasteItem"] == "폐플라스틱"]
    vendors = [public_profile(app.store.get_vendor_profile(v), fleet_cost=False) for v in VENDORS]
    done = call_tool(
        "optimize_assignment",
        {"streams": streams, "vendors": vendors},
        registry=app.registry,
        org_id=HANBIT,
        today=TODAY,
    )
    claims = {c["id"]: c for c in done["claims"]}
    total = claims[done["output"]["totals"]["carbon"]]
    # 열분해(퍼지 덩어리)는 처리 방식 탄소를 산정 보류한다. 공장 위치(origin)를 주지 않아 운송 배출도 없으므로
    # 합계에 들지 않는다(2026-11-19 자원순환 효과 보강)
    per_stream = [
        claims[a["carbon"] or a["carbonTransport"]]["evidence"][0]
        for a in done["output"]["assignments"]
        if a["carbon"] or a["carbonTransport"]
    ]
    purge = next(a for a in done["output"]["assignments"] if a["streamId"] == "st_purge")
    assert purge["carbon"] is None and purge["carbonTransport"] is None
    assert len(per_stream) == len(streams) - 1
    assert total["evidence"] == [total["evidence"][0], *per_stream]
    found = app.registry.get(total["evidence"][0], HANBIT)
    assert found.meta["inputEvidence"] == per_stream


def test_totals_inputs_are_stable_for_the_same_input():
    first, second = make_app(), make_app()
    one = to_approval(first)["result"]["totals"]
    two = to_approval(second)["result"]["totals"]
    assert one == two
