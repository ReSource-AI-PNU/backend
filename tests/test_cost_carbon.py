"""⑤ 비용·탄소 시나리오. 숫자는 설계안 v2 6.1절의 시연 가정값이며 실제 효과가 아니다."""

from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.agents.cost_carbon import evaluate_cost_carbon
from app.agents.economics_adapter import evaluate_streams
from app.api.contract import demo_session
from app.api.feasibility import DEMO_REQUEST
from app.demo.synthetic import CARBON_CONFIG, CONFIG, levy_per_kg
from app.main import app


@pytest.fixture
def streams():
    return demo_session().streams


def scenarios(streams, baseline="신재 대비", overrides=None):
    feasibility = evaluate_streams(streams, CONFIG, overrides)
    result = evaluate_cost_carbon(streams, feasibility, CARBON_CONFIG, baseline)
    return feasibility, {r.id: r for r in result.routes}, result


def test_monthly_totals_include_disposal_levy(streams):
    feasibility, by_id, _ = scenarios(streams)
    assert {k: r.monthly_krw for k, r in by_id.items()} == {
        "A": 595000, "B": 520360, "C": -308140, "D": -395250
    }
    assert by_id["A"].monthly_krw == feasibility.total_after
    assert by_id["C"].monthly_krw == feasibility.total_now
    for route in by_id.values():
        assert route.monthly_krw == sum(line.monthly_krw for line in route.breakdown)


def test_carbon_is_not_rounded(streams):
    # 설계안의 −2.06·+1.21·−1.40·+0.79는 흐름별로 먼저 반올림한 표시값이다.
    # 계산값은 반올림하지 않으므로 A는 −2.07이고, 나머지는 화면 반올림 후 문서값과 같다.
    _, by_id, _ = scenarios(streams)
    assert by_id["A"].carbon_t == pytest.approx(-2.07)
    assert by_id["B"].carbon_t == pytest.approx(-1.401)
    assert by_id["C"].carbon_t == pytest.approx(1.209)
    assert by_id["D"].carbon_t == pytest.approx(0.7905)
    assert [round(by_id[k].carbon_t, 2) for k in "BCD"] == [-1.40, 1.21, 0.79]
    sprue = by_id["A"].breakdown[0]
    assert (sprue.route, sprue.carbon_t) == ("reuse", pytest.approx(-0.96))


def test_kau_is_avoided_carbon_times_price(streams):
    _, by_id, _ = scenarios(streams)
    for route in by_id.values():
        assert route.kau_krw == pytest.approx(-route.carbon_t * CARBON_CONFIG.kau_per_t)
    assert by_id["A"].kau_krw == pytest.approx(62100)
    assert by_id["C"].kau_krw < 0  # 늘어난 탄소는 음수 금액


def test_scenario_b_burns_only_the_bundle_flow(streams):
    _, by_id, _ = scenarios(streams)
    a_lines, b_lines = by_id["A"].breakdown, by_id["B"].breakdown
    assert [line.route for line in a_lines] == ["reuse", "sell", "pyrolysis", "bundle"]
    assert [line.route for line in b_lines] == ["reuse", "sell", "pyrolysis", "incinerate"]
    assert b_lines[3].monthly_krw == -59640
    assert a_lines[:3] == b_lines[:3]


def test_landfill_baseline_keeps_differences_and_zeroes_d(streams):
    _, virgin, _ = scenarios(streams, "신재 대비")
    _, landfill, result = scenarios(streams, "매립 대비")
    assert result.baseline == "매립 대비"
    assert landfill["D"].carbon_t == 0 and landfill["D"].kau_krw == 0
    assert str(landfill["D"].kau_krw) == "0.0"  # −0.0이 아니다
    for a in "ABCD":
        for b in "ABCD":
            assert landfill[a].carbon_t - landfill[b].carbon_t == pytest.approx(
                virgin[a].carbon_t - virgin[b].carbon_t
            )
        # 금액은 기준과 무관하다.
        assert landfill[a].monthly_krw == virgin[a].monthly_krw
    assert all(line.carbon_t == 0 for line in landfill["D"].breakdown)


def test_status_recommendation_and_assumptions(streams):
    _, by_id, result = scenarios(streams)
    assert {k: r.status for k, r in by_id.items()} == {
        "A": "conditional", "B": "calculated", "C": "calculated", "D": "calculated"
    }
    assert [k for k, r in by_id.items() if r.recommended] == ["A"]
    assert "단지 순회 수거 참여사 동의" in by_id["A"].condition
    assert result.data_mode == "synthetic_demo"
    labels = {a.label for a in result.assumptions}
    assert {"매립 위탁비", "탄소 계수", "KAU", "탄소 기준", "반올림"} <= labels


def test_pending_stream_makes_totals_null_not_zero(streams):
    pending = [streams[0].model_copy(update={"monthly_kg_confirmed": False}), *streams[1:]]
    _, by_id, _ = scenarios(pending)
    for route in by_id.values():
        assert route.status == "needs_information"
        assert route.monthly_krw is None and route.carbon_t is None and route.kau_krw is None
        assert route.breakdown[0].monthly_krw is None and route.breakdown[0].carbon_t is None
        assert route.breakdown[1].monthly_krw is not None  # 다른 흐름 내역은 남긴다


def test_override_flows_through_to_scenarios(streams):
    film = streams[3].id
    _, by_id, _ = scenarios(streams, overrides={film: "sell"})
    # 소량 PCR 단독 매각: 출장비 차감으로 −42,000원, 묶음이 없으니 A와 B가 다르다.
    assert by_id["A"].breakdown[3].monthly_krw == -42000
    assert by_id["A"].monthly_krw == 595000 - 15000 - 42000
    assert by_id["A"].status == "calculated"


def test_invalid_inputs_are_rejected(streams):
    feasibility = evaluate_streams(streams, CONFIG)
    with pytest.raises(ValueError):
        evaluate_cost_carbon(streams, feasibility, CARBON_CONFIG, "현행 대비")
    with pytest.raises(ValueError):
        evaluate_cost_carbon(streams[:3], feasibility, CARBON_CONFIG)
    broken = replace(CARBON_CONFIG, carbon_per_t={"reuse": -1.6})
    with pytest.raises(ValueError):
        evaluate_cost_carbon(streams, feasibility, broken)
    with pytest.raises(ValueError):
        evaluate_cost_carbon(streams, feasibility, replace(CARBON_CONFIG, kau_per_t=-1))


@pytest.fixture
def client():
    return TestClient(app)


def test_preview_api(client):
    body = deepcopy(DEMO_REQUEST)
    response = client.post("/api/v1/cost-carbon/preview", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["baseline"] == "신재 대비" and result["dataMode"] == "synthetic_demo"
    assert [(r["id"], r["monthlyKrw"]) for r in result["routes"]] == [
        ("A", 595000), ("B", 520360), ("C", -308140), ("D", -395250)
    ]
    body["baseline"] = "매립 대비"
    rebased = client.post("/api/v1/cost-carbon/preview", json=body).json()
    assert rebased["routes"][3]["carbonT"] == 0
    body["baseline"] = "현행 대비"
    assert client.post("/api/v1/cost-carbon/preview", json=body).status_code == 422


def test_levy_and_kowax_price_are_the_only_gaps_from_the_design_doc(streams):
    # 처분부담금(소각 10원/kg, 매립 25원/kg)을 0으로, 폐합성수지 소각 위탁비를 KOWAX 188.8원에서
    # 설계안의 250원으로 되돌리면 설계안 6.1절 숫자가 그대로 나온다.
    assert (levy_per_kg("incinerate"), levy_per_kg("landfill")) == (10, 25)
    assert CONFIG.current_per_kg["폐플라스틱"] == CONFIG.current_per_kg["포장재"] == -188.8
    no_levy = replace(
        CONFIG, incinerate_levy_per_kg=0,
        current_per_kg={**CONFIG.current_per_kg, "폐플라스틱": -250, "포장재": -250},
    )
    feasibility = evaluate_streams(streams, no_levy)
    result = evaluate_cost_carbon(
        streams, feasibility, replace(CARBON_CONFIG, landfill_levy_per_kg=0)
    )
    assert [(r.id, r.monthly_krw) for r in result.routes] == [
        ("A", 595000), ("B", 505000), ("C", -387500), ("D", -356500)
    ]
    with pytest.raises(ValueError):
        evaluate_cost_carbon(
            streams, feasibility, replace(CARBON_CONFIG, landfill_levy_per_kg=-1)
        )
