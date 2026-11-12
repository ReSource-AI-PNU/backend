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
        (-150000, 510000), (-100000, 90000), (-62500, -20000), (-75000, 15000)
    ]
    assert (result.total_now, result.total_after, result.difference) == (-387500, 595000, 982500)
    assert result.total_now == sum(d.monthly_now for d in result.decisions)
    assert result.total_after == sum(d.monthly_after for d in result.decisions)
    assert result.status == "conditional"
    reuse, sell, pyrolysis, bundle = result.decisions
    assert (reuse.cash_sale_revenue, reuse.purchase_avoidance_net, reuse.labor_cost) == (0, 540000, 30000)
    assert (sell.cash_sale_revenue, sell.purchase_avoidance_net, sell.labor_cost) == (120000, 0, 30000)
    assert (pyrolysis.disposal_cost, pyrolysis.monthly_after) == (20000, -20000)
    assert (bundle.cash_sale_revenue, bundle.transport_cost, bundle.labor_cost) == (45000, 15000, 15000)
    assert bundle.after_per_kg == 100
    assert bundle.bundle_status == "conditional"


def test_route_priorities_and_thresholds() -> None:
    sprue = FLOWS[0]
    assert evaluate_flow(replace(sprue, item="금속 스크랩", material="Fe", single_material=False), CONFIG).route == "sell"
    assert evaluate_flow(replace(sprue, origin="PCR", single_material=False), CONFIG).route == "pyrolysis"
    for ratio, expected in [(0.019, "reuse"), (0.02, "sell"), (0.119, "sell"), (0.12, "incinerate")]:
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
    assert (result.monthly_now, result.monthly_after, result.monthly_difference) == (-125000, 170000, 295000)


def test_override_keeps_original_route() -> None:
    result = evaluate_flow(FLOWS[0], CONFIG, "incinerate")
    assert (result.route, result.ai_route, result.monthly_after) == ("incinerate", "reuse", -150000)
    assert evaluate_flow(FLOWS[0], CONFIG, "reuse").ai_route is None


def test_missing_inputs_and_rates_do_not_produce_partial_totals() -> None:
    for flow in (replace(FLOWS[0], monthly_kg=None), replace(FLOWS[0], detections=None),
                 replace(FLOWS[1], detections=(Detection("라벨", None),))):
        assert evaluate_flow(flow, CONFIG).status == "needs_information"
    result = evaluate_factory([FLOWS[0], replace(FLOWS[1], monthly_kg=None)], CONFIG)
    assert (result.status, result.total_now, result.total_after, result.difference) == (
        "needs_information", None, None, None
    )
    result = evaluate_flow(FLOWS[1], replace(CONFIG, sale_per_kg={}))
    assert result.status == "needs_information"


@pytest.mark.parametrize("changes", [
    {"monthly_kg": -1}, {"monthly_kg": 0}, {"monthly_kg": float("nan")}, {"monthly_kg": True},
    {"origin": "other"}, {"detections": (Detection("라벨", 1.1),)},
    {"detections": (Detection("라벨", -0.1),)},
])
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
    detections = (Detection("ABS 혼입", 0.01), Detection("PET 혼입", 0.4),
                  Detection("다른 혼합", 0.8, removed=True))
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
