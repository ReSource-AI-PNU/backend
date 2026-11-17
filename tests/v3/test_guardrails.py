import pytest

from app.v3.a2a.guardrails import GuardrailViolation, auto_decline_reason, check_terms, enforce
from app.v3.a2a.message import Message

G = {
    "floorPriceKrwPerKg": 120,
    "ceilingPriceKrwPerKg": 340,
    "days": ["화", "목"],
    "minPickupsPerMonth": 2,
    "autoDecline": ["PVC 혼입", "금속 인서트"],
}


def codes(terms):
    return [v.code for v in check_terms(G, terms)]


def test_terms_inside_guardrails():
    ok = {
        "unitPriceKrwPerKg": 320,
        "days": ["화"],
        "pickupsPerMonth": 2,
        "conditions": ["분리 배출"],
    }
    assert check_terms(G, ok) == []
    assert check_terms(G, {}) == [] and check_terms(None, ok) == []


def test_each_violation():
    assert codes({"unitPriceKrwPerKg": 100}) == ["PRICE_BELOW_FLOOR"]
    assert codes({"unitPriceKrwPerKg": 400}) == ["PRICE_ABOVE_CEILING"]
    assert codes({"days": ["화", "금"]}) == ["DAY_NOT_ALLOWED"]
    assert codes({"pickupsPerMonth": 1}) == ["PICKUPS_BELOW_MINIMUM"]
    assert codes({"conditions": "pvc혼입 허용"}) == ["AUTO_DECLINE_CONDITION"]
    assert codes({"unitPriceKrwPerKg": True}) == []


def test_fee_guardrails_use_signed_price():
    fee = {"floorPriceKrwPerKg": -150, "ceilingPriceKrwPerKg": -50}
    assert check_terms(fee, {"unitPriceKrwPerKg": -80}) == []
    assert check_terms(fee, {"unitPriceKrwPerKg": -30})[0].code == "PRICE_ABOVE_CEILING"
    assert check_terms(fee, {"unitPriceKrwPerKg": -200})[0].code == "PRICE_BELOW_FLOOR"


def test_auto_decline_reason_reads_stream():
    stream = {
        "id": "st_defect",
        "name": "불량 사출품",
        "material": "PP",
        "contaminants": [{"name": "금속 인서트", "ratio": 0.02}],
    }
    assert "금속 인서트" in auto_decline_reason(G, stream)
    assert auto_decline_reason(G, {"id": "st_sprue", "material": "PP"}) is None
    assert auto_decline_reason(None, stream) is None


def test_enforce_blocks_only_terms_messages():
    def message(kind, terms=None):
        m = Message("th_1", "org_saegil", "org_hanbit", kind, 5, "조건입니다.")
        if terms is not None:
            m.meta["terms"] = terms
        return m

    assert enforce(G, message("question")).type == "question"
    assert enforce(G, message("offer", {"unitPriceKrwPerKg": 300, "days": ["목"]})).type == "offer"
    with pytest.raises(GuardrailViolation) as caught:
        enforce(G, message("counter", {"unitPriceKrwPerKg": 380, "days": ["금"]}))
    assert [v.code for v in caught.value.violations] == ["PRICE_ABOVE_CEILING", "DAY_NOT_ALLOWED"]
    with pytest.raises(GuardrailViolation) as missing:
        enforce(G, message("conditional"))
    assert missing.value.violations[0].code == "TERMS_MISSING"
    assert enforce(G, message("offer"), {"unitPriceKrwPerKg": 200}).type == "offer"
