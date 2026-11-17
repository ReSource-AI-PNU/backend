import pytest

from app.v3.evidence.units import UNITS, UnitError, convert, dimension, known, same_value

CONTRACT_UNITS = [
    "원", "원/kg", "원/t", "원/월", "원/회", "원/km", "원/시간", "시간/월", "원/tCO2e", "kg", "t", "kg/월", "t/월", "%",
    "km", "m", "m3", "kg/m3", "tCO2e", "kgCO2e", "tCO2e/월", "kgCO2e/kg", "tCO2e/t", "회", "회/월",
    "일", "개",
]  # fmt: skip


def test_unit_list_matches_contract():
    assert sorted(UNITS) == sorted(CONTRACT_UNITS)
    assert known("원/kg") and not known("달러") and not known(None)


@pytest.mark.parametrize(
    ("value", "src", "dst", "expected"),
    [
        (850000, "원/t", "원/kg", 850),
        (320, "원/kg", "원/t", 320000),
        (1.5, "t", "kg", 1500),
        (600, "kg/월", "t/월", 0.6),
        (2, "tCO2e", "kgCO2e", 2000),
        (1.2, "kgCO2e/kg", "tCO2e/t", 1.2),
        (12500, "m", "km", 12.5),
        (7, "일", "일", 7),
    ],
)
def test_convert_same_dimension(value, src, dst, expected):
    assert convert(value, src, dst) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("src", "dst"),
    [("원/kg", "원/월"), ("kg", "m3"), ("%", "kg"), ("달러", "원"), ("원", None), (None, "kg")],
)
def test_convert_rejects_other_dimension_or_unknown(src, dst):
    with pytest.raises(UnitError):
        convert(1, src, dst)


def test_identity_without_units():
    assert convert(3, None, None) == 3.0


def test_same_value_tolerance():
    assert same_value(0.85, "원/kg", 850, "원/t")
    assert same_value(1 / 3, "t", 333.3333333, "kg")
    assert not same_value(850, "원/kg", 851, "원/kg")
    assert not same_value(float("nan"), "kg", 1, "kg")
    with pytest.raises(UnitError):
        same_value(1, "kg", 1, "km")


def test_dimension():
    assert dimension("원/t") == dimension("원/kg")
    with pytest.raises(UnitError):
        dimension("평")


def test_units_without_conversion_partner():
    # 원/km, 회, 원/tCO2e는 같은 단위끼리만 비교한다
    for unit in ("원/km", "회", "원/tCO2e"):
        assert known(unit) and same_value(3, unit, 3, unit)
    for src, dst in (("원/km", "원/kg"), ("원/tCO2e", "원/t"), ("회", "회/월")):
        with pytest.raises(UnitError):
            convert(1, src, dst)
