"""단위 환산. 같은 차원끼리만 바꾼다."""

from __future__ import annotations

import math


class UnitError(ValueError):
    """모르는 단위이거나 차원이 달라 환산할 수 없다."""


# 단위 -> (차원, 기준 단위로 바꾸는 배수)
UNITS: dict[str, tuple[str, float]] = {
    "원": ("krw", 1.0),
    "원/kg": ("krw/mass", 1.0),
    "원/t": ("krw/mass", 0.001),
    "원/월": ("krw/month", 1.0),
    "원/회": ("krw/trip", 1.0),
    "kg": ("mass", 1.0),
    "t": ("mass", 1000.0),
    "kg/월": ("mass/month", 1.0),
    "t/월": ("mass/month", 1000.0),
    "%": ("ratio", 1.0),
    "m": ("length", 1.0),
    "km": ("length", 1000.0),
    "m3": ("volume", 1.0),
    "kg/m3": ("density", 1.0),
    "kgCO2e": ("co2", 1.0),
    "tCO2e": ("co2", 1000.0),
    "tCO2e/월": ("co2/month", 1.0),
    "kgCO2e/kg": ("co2/mass", 1.0),
    "tCO2e/t": ("co2/mass", 1.0),
    "회/월": ("trip/month", 1.0),
    "일": ("day", 1.0),
    "개": ("count", 1.0),
}


def known(unit: str | None) -> bool:
    return unit in UNITS


def dimension(unit: str) -> str:
    try:
        return UNITS[unit][0]
    except KeyError:
        raise UnitError(f"모르는 단위: {unit!r}") from None


def convert(value: float, from_unit: str | None, to_unit: str | None) -> float:
    """value를 from_unit에서 to_unit으로 바꾼다. 두 단위가 같으면 그대로 돌려준다."""
    if from_unit == to_unit:
        return float(value)
    if from_unit is None or to_unit is None:
        raise UnitError(f"단위가 한쪽에만 있다: {from_unit!r} -> {to_unit!r}")
    from_dim, from_factor = _lookup(from_unit)
    to_dim, to_factor = _lookup(to_unit)
    if from_dim != to_dim:
        raise UnitError(f"차원이 달라 환산할 수 없다: {from_unit} -> {to_unit}")
    return float(value) * from_factor / to_factor


def same_value(a: float, unit_a: str | None, b: float, unit_b: str | None) -> bool:
    """a(unit_a)와 b(unit_b)가 환산 뒤 같은 값인지. 환산할 수 없으면 UnitError."""
    converted = convert(a, unit_a, unit_b)
    if not (math.isfinite(converted) and math.isfinite(float(b))):
        return False
    return math.isclose(converted, float(b), rel_tol=1e-6, abs_tol=1e-9)


def _lookup(unit: str) -> tuple[str, float]:
    try:
        return UNITS[unit]
    except KeyError:
        raise UnitError(f"모르는 단위: {unit!r}") from None
