"""Independent profit comparison for a hypothetical processed scrap sale."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class CostInputs:
    quantity_kg: float
    current_disposal_cost_per_kg: float
    sale_price_per_kg: float
    processing_cost_per_kg: float
    recovery_rate: float
    transport_cost: float


@dataclass(frozen=True)
class CostResult:
    current_profit: float
    sale_quantity_kg: float
    processed_profit: float
    profit_difference: float
    break_even_processing_cost_per_kg: float


def calculate_costs(inputs: CostInputs) -> CostResult:
    """Calculate without rounding; monetary values are KRW and quantities kg.

    Invalid, non-finite, or non-numeric inputs raise ValueError. The break-even
    cost can be negative, meaning no nonnegative processing cost breaks even.
    """
    values = {}
    for name in CostInputs.__dataclass_fields__:
        value = getattr(inputs, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite number")  # noqa: TRY004
        try:
            value = float(value)
        except OverflowError as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        values[name] = value

    quantity = values["quantity_kg"]
    recovery = values["recovery_rate"]
    if quantity <= 0:
        raise ValueError("quantity_kg must be greater than zero")
    if not 0 <= recovery <= 1:
        raise ValueError("recovery_rate must be between zero and one")
    for name in (
        "current_disposal_cost_per_kg",
        "sale_price_per_kg",
        "processing_cost_per_kg",
        "transport_cost",
    ):
        if values[name] < 0:
            raise ValueError(f"{name} must be nonnegative")

    disposal_cost = quantity * values["current_disposal_cost_per_kg"]
    sale_quantity = quantity * recovery
    sale_revenue = sale_quantity * values["sale_price_per_kg"]
    current_profit = -disposal_cost
    processed_profit = (
        sale_revenue - quantity * values["processing_cost_per_kg"] - values["transport_cost"]
    )
    difference = processed_profit - current_profit
    break_even = (sale_revenue + disposal_cost - values["transport_cost"]) / quantity
    result = CostResult(current_profit, sale_quantity, processed_profit, difference, break_even)
    if not all(isfinite(getattr(result, name)) for name in CostResult.__dataclass_fields__):
        raise ValueError("calculation exceeds the finite floating-point range")
    return result
