"""All figures below are hypothetical."""

import math
import unittest
from dataclasses import replace

from app.agents.cost_calculator import CostInputs, calculate_costs


class CostCalculatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = CostInputs(1000, 200, 500, 100, 0.8, 30000)

    def test_gain_case(self) -> None:
        result = calculate_costs(self.inputs)
        self.assertEqual(result.current_profit, -200000)
        self.assertEqual(result.sale_quantity_kg, 800)
        self.assertEqual(result.processed_profit, 270000)
        self.assertEqual(result.profit_difference, 470000)
        self.assertEqual(result.break_even_processing_cost_per_kg, 570)

    def test_loss_case(self) -> None:
        result = calculate_costs(replace(self.inputs, processing_cost_per_kg=700))
        self.assertEqual(result.processed_profit, -330000)
        self.assertEqual(result.profit_difference, -130000)

    def test_break_even(self) -> None:
        cost = calculate_costs(self.inputs).break_even_processing_cost_per_kg
        result = calculate_costs(replace(self.inputs, processing_cost_per_kg=cost))
        self.assertAlmostEqual(result.profit_difference, 0)

    def test_negative_break_even_is_not_clamped(self) -> None:
        inputs = replace(self.inputs, transport_cost=700000, processing_cost_per_kg=0)
        result = calculate_costs(inputs)
        self.assertEqual(result.break_even_processing_cost_per_kg, -100)
        self.assertEqual(result.profit_difference, -100000)

    def test_recovery_boundaries_and_zero_costs(self) -> None:
        for recovery in (0, 1):
            with self.subTest(recovery=recovery):
                result = calculate_costs(CostInputs(1, 0, 0, 0, recovery, 0))
                self.assertEqual(result.sale_quantity_kg, recovery)
                self.assertEqual(result.profit_difference, 0)

    def test_invalid_inputs(self) -> None:
        invalid = {
            "quantity_kg": (0, -1),
            "recovery_rate": (-0.1, 1.1),
            "current_disposal_cost_per_kg": (-1,),
            "sale_price_per_kg": (-1,),
            "processing_cost_per_kg": (-1,),
            "transport_cost": (-1,),
        }
        for field, values in invalid.items():
            for value in (*values, math.nan, math.inf, -math.inf, True, "1"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    calculate_costs(replace(self.inputs, **{field: value}))

    def test_nonfinite_result_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calculate_costs(replace(self.inputs, quantity_kg=1e308))


if __name__ == "__main__":
    unittest.main()
