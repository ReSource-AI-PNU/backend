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
    # 시드 처리 배출계수는 원문 경계를 확인하지 못했고(값은 원본과 같지만 경계 미공개, value_checked), 운송 배출도 실제 사업장 거리·적재·회송
    # 조건이 확인되지 않아 탄소는 흐름별로도 합계로도 산정 보류한다(0으로 합치지 않는다)
    assert result["totals"]["carbon"] is None
    assert all(a["carbon"] is None and a["carbonTransport"] is None for a in result["assignments"])
    money = {k: v for k, v in result["totals"].items() if k != "carbon"}
    assert set(money) == {"monthlyNow", "monthlyAfter", "difference"}
    for key, claim in money.items():
        total_id = claim["evidence"][0]
        assert total_id.startswith("calc:optimize_assignment:") and ":total" in total_id
        found = service.get_evidence(app, total_id, HANBIT)
        inputs = found["evidence"]["meta"]["inputEvidence"]
        # 합계의 입력 근거는 흐름별 계산 근거 전부다(빠짐도 덤도 없다)
        assert inputs and sorted(inputs) == sorted(per_stream[key]), key
        assert len(inputs) == len(set(inputs))
        chain = {item["id"]: item for item in found["chain"]}
        assert set(inputs) <= set(chain)
        # 사슬이 흐름별 계산을 거쳐 원 근거(문서·담당자 답)까지 이어지고, 가정값은 들어가지 않는다
        kinds = {item["kind"] for item in chain.values()}
        assert kinds & {"doc", "human"}, (key, kinds)
        assert "assumption" not in kinds, key
        # 합계 값은 흐름별 값의 합과 같다
        parts = sum(chain[i]["value"] for i in inputs)
        assert abs(parts - claim["value"]) < 1e-6


def test_carbon_total_is_held_instead_of_summing_unknown_carbon_as_zero():
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
    # 흐름마다 탄소를 내지 못했으면(원문 미확인 배출계수, 운송 배출 경계 미확인) 합계 claim을 만들지 않는다
    assert done["output"]["totals"]["carbon"] is None
    assert all(a["carbon"] is None for a in done["output"]["assignments"])
    assert not any(c["id"] == "totalCarbon" for c in done["claims"])
    held = next(c for c in done["checks"] if c["key"] == "carbon")
    assert held["status"] == "conditional"
    assert all(s["name"] in held["text"] for s in streams if s["id"] != "st_film")


def test_totals_inputs_are_stable_for_the_same_input():
    first, second = make_app(), make_app()
    one = to_approval(first)["result"]["totals"]
    two = to_approval(second)["result"]["totals"]
    assert one == two
