import pytest

from app.v3.a2a.message import Claim
from app.v3.evidence.render import MISSING, format_number, format_value, render_text, split_text

CLAIMS = [
    Claim("price", "number", 320, ("doc:a:b:c",), "원/kg"),
    Claim("month", "number", 510000.4, ("calc:a:b:c",), "원/월"),
    {
        "id": "law",
        "kind": "clause",
        "value": "허가 품목을 확인하여야 한다",
        "evidence": ["law:a:b@c"],
    },
]


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (1234567, "원", "1,234,567"),
        (510000.5, "원/월", "510,001"),
        (-150000, "원/월", "-150,000"),
        (-0.2, "원", "0"),
        (320.0, "원/kg", "320"),
        (0.85, "원/kg", "0.85"),
        (1234.5, "kg/월", "1,234.5"),
        (0.1 + 0.2, "tCO2e", "0.3"),
        (2.50, "%", "2.5"),
        (0.123456, "t", "0.1235"),
        (12, None, "12"),
    ],
)
def test_format_number(value, unit, expected):
    assert format_number(value, unit) == expected


def test_format_value():
    assert format_value(320, "원/kg") == "320원/kg"
    assert format_value(2.5, "%") == "2.5%"
    assert format_value(600, "kg/월") == "600 kg/월"
    assert format_value(-0.96, "tCO2e/월") == "-0.96 tCO2e/월"
    assert format_value(7, "일") == "7일"
    assert format_value(None, "원") == MISSING
    assert format_value("필름") == "필름"


def test_render_text():
    text = "단가는 {{price}}이고 월 {{ month }}입니다. 근거: {{law}} / 운임 {{freight}}"
    assert render_text(text, CLAIMS) == (
        "단가는 320원/kg이고 월 510,000원/월입니다. 근거: 허가 품목을 확인하여야 한다 / 운임 확인 필요"
    )


def test_split_text():
    parts = split_text("단가는 {{price}}입니다. {{freight}}", CLAIMS)
    assert [p["type"] for p in parts] == ["text", "claim", "text", "claim"]
    assert parts[0] == {"type": "text", "text": "단가는 "}
    assert parts[1] == {
        "type": "claim",
        "claimId": "price",
        "kind": "number",
        "value": 320,
        "unit": "원/kg",
        "evidence": ["doc:a:b:c"],
        "missing": False,
        "display": "320원/kg",
    }
    assert parts[3] == {"type": "claim", "claimId": "freight", "missing": True, "display": MISSING}
    assert "".join(p.get("text", "") for p in split_text("숫자 없음", [])) == "숫자 없음"
    assert split_text("", CLAIMS) == []
