"""월 발생량이 바뀌면 계약(월 정액·회당)으로 받은 현행 처리비의 kg당 환산을 다시 한다.

2026-11-19 멘토링 점검에서 스프루 월 발생량 600kg에 월 정액 450,000원(750원/kg)을 넣은 뒤 월 발생량만
1,200kg으로 고치자, 저장된 750원/kg × 1,200kg으로 현행 월 비용이 900,000원이 됐다. 계약 원본(billing)이
기준이므로 월 정액은 그대로 450,000원, kg당 환산은 375원이어야 한다.
"""

from app.v3 import current_cost, service
from app.v3.agents import questions
from app.v3.intake import csv_measure
from tests.v3.runtime_fixtures import HANBIT, make_app, to_approval

SPRUE = "st_sprue"


def _patch(app, patch):
    return service.update_stream(app, HANBIT, SPRUE, patch, HANBIT)


def _sprue(view):
    return next(a for a in view["result"]["assignments"] if a["streamId"] == SPRUE)


def _contract(app, billing, kg=600):
    _patch(app, {"monthlyKg": kg})
    return _patch(app, {"currentTreatment": {"method": "혼합 소각", "billing": billing}})


def test_monthly_flat_fee_stays_the_same_when_only_the_volume_changes():
    app = make_app()
    saved = _contract(app, {"kind": "monthly", "krwPerMonth": 450000})
    assert saved["currentTreatment"]["unitCostKrwPerKg"] == 750
    saved = _patch(app, {"monthlyKg": 1200})
    current = saved["currentTreatment"]
    assert current["billing"] == {"kind": "monthly", "krwPerMonth": 450000.0}
    assert current["unitCostKrwPerKg"] == 375
    calc = app.registry.get(current["evidence"][0], HANBIT)
    assert calc.kind == "calc" and calc.value == 375
    assert "1,200kg" in calc.meta["formula"] and "450,000원" in calc.meta["formula"]
    # 환산의 입력은 새 월 발생량 근거다
    assert saved["monthlyKgEvidence"][0] in calc.meta["inputEvidence"]
    sprue = _sprue(to_approval(app))
    assert sprue["monthlyNow"]["value"] == -450000
    assert sprue["currentAssumed"] is False


def test_per_trip_contract_is_recomputed_from_the_trip_price_and_count():
    app = make_app()
    saved = _contract(app, {"kind": "per_trip", "krwPerTrip": 300000, "tripsPerMonth": 2})
    assert saved["currentTreatment"]["unitCostKrwPerKg"] == 1000
    saved = _patch(app, {"monthlyKg": 1000})
    assert saved["currentTreatment"]["unitCostKrwPerKg"] == 600
    assert saved["currentTreatment"]["billing"]["tripsPerMonth"] == 2
    assert _sprue(to_approval(app))["monthlyNow"]["value"] == -600000


def test_unknown_or_zero_volume_does_not_reuse_the_old_per_kg_cost():
    for kg in (None, 0):
        app = make_app()
        _contract(app, {"kind": "monthly", "krwPerMonth": 450000})
        saved = _patch(app, {"monthlyKg": kg})
        current = saved["currentTreatment"]
        # 계약은 남기고, kg당 환산은 비운다(예전 750원/kg을 쓰지 않는다)
        assert current["billing"] == {"kind": "monthly", "krwPerMonth": 450000.0}
        assert current["unitCostKrwPerKg"] is None
        assert current_cost.signed_cost(current) is None
        # 월 발생량을 다시 넣으면 그 값으로 환산한다
        again = _patch(app, {"monthlyKg": 900})
        assert again["currentTreatment"]["unitCostKrwPerKg"] == 500


def test_without_volume_the_contract_is_not_replaced_by_the_incineration_guess():
    app = make_app()
    _contract(app, {"kind": "monthly", "krwPerMonth": 450000})
    _patch(app, {"monthlyKg": 0})
    view = to_approval(app)
    shown = next(s for s in view["streams"] if s["id"] == SPRUE)["currentTreatment"]
    # 혼합 소각 가정값으로 바꿔 보이지 않는다. 계약은 그대로 보이고 kg당 값은 비어 있다
    assert shown["assumed"] is False and shown["unitCost"] is None
    assert shown["billing"]["text"] == "월 정액 450,000원"
    sprue = _sprue(view)
    assert sprue["currentAssumed"] is False
    assert sprue["monthlyNow"] is None


def test_answering_the_monthly_kg_question_recomputes_the_contract():
    app = make_app()
    _contract(app, {"kind": "monthly", "krwPerMonth": 450000})
    stream = app.store.get_stream(SPRUE, HANBIT)
    stream = questions.apply_answer(app.registry, stream, "monthlyKg", 1200.0, app.today)
    assert stream["currentTreatment"]["unitCostKrwPerKg"] == 375


def test_measured_monthly_kg_from_csv_recomputes_the_contract():
    app = make_app()
    _contract(app, {"kind": "monthly", "krwPerMonth": 450000})
    name = app.store.get_stream(SPRUE, HANBIT)["name"]
    header = csv_measure.template_csv().decode("utf-8-sig").splitlines()[0]
    row = f"{name},사출,PP,1.0,0.3,2026-11-16,PIR,가공 전,L-001,1200"
    out = service.upload_csv(app, HANBIT, "m.csv", f"{header}\n{row}\n".encode(), HANBIT)
    assert out["accepted"] == 1, out
    current = app.store.get_stream(SPRUE, HANBIT)["currentTreatment"]
    assert current["unitCostKrwPerKg"] == 375
