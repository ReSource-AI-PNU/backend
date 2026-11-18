"""Tests validate synthetic demo rules, not real-world accuracy."""

from dataclasses import replace
from itertools import permutations

import pytest

from app.agents.demo_routes import Detection, evaluate_factory, evaluate_flow
from app.demo.synthetic import CONFIG, FLOWS


def test_frontend_demo_factory() -> None:
    result = evaluate_factory(FLOWS, CONFIG)
    assert [d.route for d in result.decisions] == ["reuse", "sell", "pyrolysis", "bundle"]
    assert [(d.monthly_now, d.monthly_after) for d in result.decisions] == [
        (-150000, 510000),
        (-100000, 90000),
        (-62500, -20000),
        (-75000, 15000),
    ]
    assert (result.total_now, result.total_after, result.difference) == (-387500, 595000, 982500)
    assert result.total_now == sum(d.monthly_now for d in result.decisions)
    assert result.total_after == sum(d.monthly_after for d in result.decisions)
    assert result.status == "conditional"
    reuse, sell, pyrolysis, bundle = result.decisions
    assert (reuse.cash_sale_revenue, reuse.purchase_avoidance_net, reuse.labor_cost) == (
        0,
        540000,
        30000,
    )
    assert (sell.cash_sale_revenue, sell.purchase_avoidance_net, sell.labor_cost) == (
        120000,
        0,
        30000,
    )
    assert (pyrolysis.disposal_cost, pyrolysis.monthly_after) == (20000, -20000)
    assert (bundle.cash_sale_revenue, bundle.transport_cost, bundle.labor_cost) == (
        45000,
        15000,
        15000,
    )
    assert bundle.after_per_kg == 100
    assert bundle.bundle_status == "conditional"


def test_structured_actions_follow_config_and_calculated_labor() -> None:
    reuse, sell, pyrolysis, bundle = evaluate_factory(FLOWS, CONFIG).decisions
    assert (reuse.action.title, reuse.action.labor_hours, reuse.action.labor_cost) == (
        "색별로 분리해 분쇄기에 넣기",
        2,
        30000,
    )
    assert (sell.action.title, sell.action.labor_hours, sell.action.labor_cost) == (
        "종이 라벨·금속 인서트 제거 후 따로 모으기",
        2,
        30000,
    )
    assert (bundle.action.title, bundle.action.labor_hours, bundle.action.labor_cost) == (
        "흐름을 마대에 따로 모으기",
        1,
        15000,
    )
    assert pyrolysis.action is None and pyrolysis.labor_cost == 0
    for decision in (reuse, sell, bundle):
        assert decision.action.labor_cost == decision.labor_cost
        assert decision.action.labor_cost == decision.action.labor_hours * CONFIG.labor_per_hour

    changed = replace(CONFIG, reuse_labor_hours=1.5, labor_per_hour=12000)
    result = evaluate_flow(FLOWS[0], changed)
    assert (result.action.labor_hours, result.action.labor_cost, result.labor_cost) == (
        1.5,
        18000,
        18000,
    )
    assert result.monthly_after == 522000
    zero = evaluate_flow(FLOWS[0], replace(CONFIG, reuse_labor_hours=0))
    assert zero.action is not None and zero.action.labor_cost == 0


def test_route_priorities_and_thresholds() -> None:
    sprue = FLOWS[0]
    assert (
        evaluate_flow(
            replace(sprue, item="금속 스크랩", material="Fe", single_material=False), CONFIG
        ).route
        == "sell"
    )
    assert (
        evaluate_flow(replace(sprue, origin="PCR", single_material=False), CONFIG).route
        == "pyrolysis"
    )
    for ratio, expected in [
        (0.019, "reuse"),
        (0.02, "sell"),
        (0.119, "sell"),
        (0.12, "incinerate"),
    ]:
        flow = replace(sprue, detections=(Detection("라벨", ratio),))
        assert evaluate_flow(flow, CONFIG).route == expected
    for ratio, expected in [(0.05, "sell"), (0.05001, "pyrolysis")]:
        flow = replace(sprue, single_material=False, detections=(Detection("ABS 혼입", ratio),))
        assert evaluate_flow(flow, CONFIG).route == expected
    removed = replace(sprue, single_material=False, detections=(Detection("ABS 혼입", 0.9, True),))
    assert evaluate_flow(removed, CONFIG).route == "reuse"
    assert evaluate_flow(replace(FLOWS[3], monthly_kg=999), CONFIG).route == "bundle"
    assert evaluate_flow(replace(FLOWS[3], monthly_kg=1000), CONFIG).route == "sell"


@pytest.mark.parametrize("material", ["LDPE", "LLDPE", " ldpe "])
def test_film_resin_exact_match(material: str) -> None:
    assert evaluate_flow(replace(FLOWS[3], material=material), CONFIG).route == "bundle"


@pytest.mark.parametrize("material", ["PET", "HDPE", "XL-LDPE", "LDPE+PET"])
def test_other_resins_are_not_assumed_to_be_film(material: str) -> None:
    result = evaluate_flow(replace(FLOWS[3], material=material), CONFIG)
    assert result.status == "needs_information"
    assert result.route is None


def test_recalculate_quantity_and_unit_price() -> None:
    flow = replace(FLOWS[1], monthly_kg=500)
    changed = replace(CONFIG, sale_per_kg={**CONFIG.sale_per_kg, "폐플라스틱": 400})
    result = evaluate_flow(flow, changed)
    assert (result.monthly_now, result.monthly_after, result.monthly_difference) == (
        -125000,
        170000,
        295000,
    )


def test_override_keeps_original_route() -> None:
    result = evaluate_flow(FLOWS[0], CONFIG, "incinerate")
    assert (result.route, result.ai_route, result.monthly_after) == ("incinerate", "reuse", -150000)
    assert evaluate_flow(FLOWS[0], CONFIG, "reuse").ai_route is None


def test_missing_inputs_and_rates_do_not_produce_partial_totals() -> None:
    for flow in (
        replace(FLOWS[0], monthly_kg=None),
        replace(FLOWS[0], detections=None),
        replace(FLOWS[1], detections=(Detection("라벨", None),)),
    ):
        assert evaluate_flow(flow, CONFIG).status == "needs_information"
    result = evaluate_factory([FLOWS[0], replace(FLOWS[1], monthly_kg=None)], CONFIG)
    assert (result.status, result.total_now, result.total_after, result.difference) == (
        "needs_information",
        None,
        None,
        None,
    )
    result = evaluate_flow(FLOWS[1], replace(CONFIG, sale_per_kg={}))
    assert result.status == "needs_information"


@pytest.mark.parametrize(
    "changes",
    [
        {"monthly_kg": -1},
        {"monthly_kg": 0},
        {"monthly_kg": float("nan")},
        {"monthly_kg": True},
        {"origin": "other"},
        {"detections": (Detection("라벨", 1.1),)},
        {"detections": (Detection("라벨", -0.1),)},
    ],
)
def test_invalid_flow(changes: dict) -> None:
    with pytest.raises(ValueError):
        evaluate_flow(replace(FLOWS[0], **changes), CONFIG)


def test_invalid_configuration_and_factory() -> None:
    with pytest.raises(ValueError):
        evaluate_flow(FLOWS[0], replace(CONFIG, bundle_transport_per_kg=-1))
    with pytest.raises(ValueError):
        evaluate_factory([], CONFIG)
    with pytest.raises(ValueError):
        evaluate_factory([FLOWS[0], FLOWS[0]], CONFIG)
    with pytest.raises(ValueError):
        evaluate_factory(FLOWS, CONFIG, {"missing": "sell"})
    with pytest.raises(ValueError):
        evaluate_flow(FLOWS[0], CONFIG, "invalid")


def test_mixed_detection_order_and_removed_detection() -> None:
    detections = (
        Detection("ABS 혼입", 0.01),
        Detection("PET 혼입", 0.4),
        Detection("다른 혼합", 0.8, removed=True),
    )
    for order in permutations(detections):
        result = evaluate_flow(replace(FLOWS[0], single_material=False, detections=order), CONFIG)
        assert result.route == "pyrolysis"
        assert result.status == "calculated"


def test_unsupported_demo_combinations_do_not_borrow_pp_value() -> None:
    metal = replace(FLOWS[0], item="금속 스크랩", material="Fe")
    result = evaluate_flow(metal, CONFIG, "reuse")
    assert (result.status, result.route, result.ai_route) == ("needs_information", "reuse", "sell")
    assert result.after_per_kg is None
    assert result.monthly_after is None
    assert evaluate_factory([metal], CONFIG, {metal.id: "reuse"}).total_after is None
    unknown = replace(FLOWS[0], material="HDPE")
    assert evaluate_flow(unknown, CONFIG).status == "needs_information"
    lldpe = evaluate_flow(replace(FLOWS[3], material="LLDPE"), CONFIG)
    assert (lldpe.route, lldpe.status, lldpe.after_per_kg) == ("bundle", "needs_information", None)
    mixed = replace(FLOWS[2], single_material=True, detections=())
    assert evaluate_flow(mixed, CONFIG).status == "needs_information"


def test_supported_override_retains_ai_route() -> None:
    result = evaluate_flow(FLOWS[0], CONFIG, "sell")
    assert (result.status, result.route, result.ai_route) == ("calculated", "sell", "reuse")
    assert result.after_per_kg == 300


def test_ladder_matches_frontend_demo() -> None:
    reuse, sell, pyrolysis, bundle = evaluate_factory(FLOWS, CONFIG).decisions
    assert [(s.label, s.per_kg) for s in reuse.ladder] == [
        ("현재(혼합 소각)", -250),
        ("분리해 매각", 300),
        ("분쇄 재투입", 900),
    ]
    assert [(s.label, s.per_kg) for s in sell.ladder] == [
        ("현재(혼합 소각)", -250),
        ("이물 일부 제거", 150),
        ("이물 제거 후 매각", 300),
    ]
    assert [(s.label, s.per_kg) for s in pyrolysis.ladder] == [
        ("현재(혼합 소각)", -250),
        ("열분해 위탁", -80),
    ]
    assert [(s.label, s.per_kg) for s in bundle.ladder] == [
        ("현재(혼합 소각)", -250),
        ("분리해 단독 매각", -40),
        ("단지 묶음 매각", 100),
    ]
    # 사다리의 마지막 칸은 고른 경로의 kg당 값과 같다.
    for decision in (reuse, sell, pyrolysis, bundle):
        assert decision.ladder[0].per_kg == decision.current_per_kg
        assert decision.ladder[-1].per_kg == decision.after_per_kg


def test_alternatives_match_frontend_demo() -> None:
    reuse, sell, pyrolysis, bundle = evaluate_factory(FLOWS, CONFIG).decisions

    def pairs(decision):
        return [(a.route, a.per_kg) for a in decision.alternatives]

    # PIR 흐름에는 묶음이 없고, 포장재(PCR)에는 재투입이 없다.
    assert pairs(reuse) == [("sell", 300), ("pyrolysis", -80), ("incinerate", -250)]
    assert pairs(sell) == [("reuse", 900), ("pyrolysis", -80), ("incinerate", -250)]
    assert pairs(pyrolysis) == [("reuse", 900), ("sell", 300), ("incinerate", -250)]
    assert pairs(bundle) == [("sell", -40), ("pyrolysis", -80), ("incinerate", -250)]
    notes = {d.route: {a.route: a.note for a in d.alternatives} for d in (pyrolysis, bundle)}
    assert notes["pyrolysis"]["sell"] == "재질이 섞여 매입처가 받지 않습니다."
    assert notes["bundle"]["sell"] == "혼자 팔면 출장비 때문에 단가가 깎입니다."
    for decision in (reuse, sell, pyrolysis, bundle):
        assert decision.route not in [a.route for a in decision.alternatives]
        assert all(a.note for a in decision.alternatives)


def test_override_ladder_and_alternatives_follow_the_chosen_route() -> None:
    changed = evaluate_flow(FLOWS[0], CONFIG, "incinerate")
    assert [(s.label, s.per_kg) for s in changed.ladder] == [("현재(혼합 소각)", -250)]
    assert [(a.route, a.per_kg) for a in changed.alternatives] == [
        ("reuse", 900),
        ("sell", 300),
        ("pyrolysis", -80),
    ]
    # 최소 물량 이상인 PCR 포장재는 단독 매각이 깎이지 않는다.
    big_film = evaluate_flow(replace(FLOWS[3], monthly_kg=1200), CONFIG)
    assert big_film.route == "sell"
    assert [(a.route, a.per_kg) for a in big_film.alternatives] == [
        ("bundle", 100),
        ("pyrolysis", -80),
        ("incinerate", -250),
    ]


def test_pending_flow_has_no_ladder_or_alternatives() -> None:
    pending = evaluate_flow(replace(FLOWS[0], material=None), CONFIG)
    assert pending.status == "needs_information"
    assert pending.ladder is None and pending.alternatives is None
    unsupported = evaluate_flow(FLOWS[2], CONFIG, "reuse")
    assert unsupported.status == "needs_information"
    assert unsupported.ladder is None and unsupported.alternatives is None


def test_solo_sale_penalty_is_validated() -> None:
    with pytest.raises(ValueError):
        evaluate_flow(FLOWS[0], replace(CONFIG, solo_sale_penalty_per_kg=-1))


def test_sell_ladder_has_removal_steps_only_when_something_is_removed() -> None:
    # 이물이 없는 흐름에는 "이물 일부 제거" 칸을 만들지 않는다.
    clean = evaluate_flow(FLOWS[0], CONFIG, "sell")
    assert clean.action is None
    assert [(s.label, s.per_kg) for s in clean.ladder] == [
        ("현재(혼합 소각)", -250),
        ("분리해 매각", 300),
    ]
    metal = evaluate_flow(replace(FLOWS[0], item="금속 스크랩", material="Fe"), CONFIG)
    assert [(s.label, s.per_kg) for s in metal.ladder] == [
        ("현재(혼합 소각)", -250),
        ("분리해 매각", 260),
    ]
    assert metal.ladder[-1].per_kg == metal.after_per_kg


def test_small_pcr_sold_alone_pays_the_same_trip_penalty_as_shown() -> None:
    # 대안에 보인 단독 매각 값(-40원/kg)과 실제로 그 경로를 골랐을 때의 값이 같아야 한다.
    shown = {a.route: a.per_kg for a in evaluate_flow(FLOWS[3], CONFIG).alternatives}["sell"]
    solo = evaluate_flow(FLOWS[3], CONFIG, "sell")
    assert solo.status == "calculated"
    assert solo.after_per_kg == shown == 150 - 190
    assert (solo.cash_sale_revenue, solo.transport_cost, solo.labor_cost) == (45000, 57000, 30000)
    assert solo.monthly_after == -40 * 300 - 30000
    assert [(s.label, s.per_kg) for s in solo.ladder] == [
        ("현재(혼합 소각)", -250),
        ("분리해 단독 매각", -40),
    ]
    # 최소 물량 이상이면 출장비가 붙지 않는다.
    big = evaluate_flow(replace(FLOWS[3], monthly_kg=1200), CONFIG)
    assert (big.route, big.after_per_kg, big.transport_cost) == ("sell", 150, 0)


def test_pyrolysis_is_offered_only_for_plastic_flows() -> None:
    metal = evaluate_flow(replace(FLOWS[0], item="금속 스크랩", material="Fe"), CONFIG)
    assert [a.route for a in metal.alternatives] == ["incinerate"]
    wood = evaluate_flow(replace(FLOWS[0], item="폐목재", material="목재"), CONFIG)
    assert wood.route == "sell" and [a.route for a in wood.alternatives] == ["incinerate"]
    film = evaluate_flow(FLOWS[3], CONFIG)
    assert "pyrolysis" in [a.route for a in film.alternatives]
