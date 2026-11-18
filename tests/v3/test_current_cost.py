"""현행 처리비를 차량·횟수 단위나 월 정액으로 받아 kg당으로 바꾼다(2026-11-19 배출 사업장 설문)."""

import pytest

from app.v3 import current_cost, service
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.validator import validate_text
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app, start


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "회당 30만원, 한 달에 2번",
            {"kind": "per_trip", "krwPerTrip": 300000, "tripsPerMonth": 2},
        ),
        (
            "한 번 수거에 25만원, 월 3회",
            {"kind": "per_trip", "krwPerTrip": 250000, "tripsPerMonth": 3},
        ),
        (
            "차량 한 대 40만원 월 2회",
            {"kind": "per_trip", "krwPerTrip": 400000, "tripsPerMonth": 2},
        ),
        ("월 2회 수거, 1회 35만원", {"kind": "per_trip", "krwPerTrip": 350000, "tripsPerMonth": 2}),
        ("1년에 24번, 회당 30만원", {"kind": "per_trip", "krwPerTrip": 300000, "tripsPerMonth": 2}),
        ("한 달에 60만원", {"kind": "monthly", "krwPerMonth": 600000}),
        ("월 60만원 정도", {"kind": "monthly", "krwPerMonth": 600000}),
        ("1년에 720만원", {"kind": "monthly", "krwPerMonth": 600000}),
        ("kg당 250원", None),
        ("톤당 25만원", None),
        ("250원", None),
    ],
)
def test_parse_billing_reads_contract_forms(text, expected):
    assert current_cost.parse_billing(text) == expected


def test_parse_billing_asks_for_the_count_when_only_a_trip_price_is_given():
    with pytest.raises(ValueError, match="한 달에 몇 번"):
        current_cost.parse_billing("회당 30만원")


def _question(view, field, stream_id):
    return next(
        q
        for q in view["questions"]
        if q["field"] == field and q["streamId"] == stream_id and not q["answered"]
    )


def test_question_answer_per_trip_becomes_a_per_kg_cost_with_calc_evidence():
    app = make_app()
    view = start(app)
    sid = view["session"]["id"]
    # 첫 묶음 질문은 모름으로 넘기고, 현행 처리비 질문이 나올 때까지 돈다
    for _ in range(3):
        asked = [q for q in view["questions"] if not q["answered"]]
        cost = next((q for q in asked if q["field"] == "currentCost"), None)
        if cost is not None:
            break
        for q in asked:
            service.answer_question(app, sid, q["id"], "unknown", HANBIT)
        view = service.run_session(app, sid, HANBIT)
    assert cost is not None, "현행 처리비 질문이 나와야 한다"
    stream = app.store.get_stream(cost["streamId"], HANBIT)
    if stream.get("monthlyKg") is None:
        pytest.skip("월 발생량이 없는 흐름이라 환산할 수 없다")
    service.answer_question(app, sid, cost["id"], "회당 30만원, 한 달에 2번", HANBIT)
    stream = app.store.get_stream(cost["streamId"], HANBIT)
    current = stream["currentTreatment"]
    expected = round(600000 / float(stream["monthlyKg"]), 1)
    assert current["unitCostKrwPerKg"] == expected
    assert current["billing"] == {"kind": "per_trip", "krwPerTrip": 300000.0, "tripsPerMonth": 2.0}
    calc = app.registry.get(current["evidence"][0], HANBIT)
    said = app.registry.get(current["evidence"][1], HANBIT)
    assert calc.kind == "calc" and calc.value == expected and calc.unit == "원/kg"
    assert said.kind == "human" and said.value == 600000 and said.unit == "원/월"
    assert said.id in calc.meta["inputEvidence"]
    # 업체는 현행 처리비 근거를 보지 못한다
    assert SAEGIL not in calc.access and SAEGIL not in said.access
    # 질문 기록에는 사람이 읽는 글이 남는다
    recorded = next(q for q in app.store.list_questions(sid) if q["id"] == cost["id"])
    assert recorded["answer"] == "회당 300,000원 × 월 2회(월 600,000원)"
    # 손익 계산에 쓰는 claim이 근거 검증을 통과한다
    pool = ClaimPool(app.registry, HANBIT)
    facts = pool.add_stream(stream, private=True)
    claim = pool.get(facts["currentUnitCost"]).claim.to_dict()
    result = validate_text(
        "{{c}}", [{**claim, "id": "c"}], app.registry, org_id=HANBIT, today=app.today
    )
    assert result.ok, result.rejections


def test_answer_without_monthly_kg_explains_what_is_missing():
    app = make_app()
    stream = app.store.list_streams(HANBIT)[0]
    stream = {**stream, "monthlyKg": None, "monthlyKgEvidence": []}
    with pytest.raises(ValueError, match="월 발생량을 알아야"):
        current_cost.to_treatment(
            app.registry,
            stream,
            {"kind": "monthly", "krwPerMonth": 600000},
            app.today,
            method="혼합 소각",
            source="시험",
        )


def test_materials_form_accepts_monthly_billing_and_the_view_shows_it():
    app = make_app()
    stream = next(s for s in app.store.list_streams(HANBIT) if s.get("monthlyKg"))
    saved = service.update_stream(
        app,
        HANBIT,
        stream["id"],
        {
            "currentTreatment": {
                "method": "혼합 소각",
                "billing": {"kind": "monthly", "krwPerMonth": 450000},
            }
        },
        HANBIT,
    )
    assert saved["currentTreatment"]["unitCostKrwPerKg"] == round(
        450000 / float(stream["monthlyKg"]), 1
    )
    with pytest.raises(service.ServiceError) as err:
        service.update_stream(
            app,
            HANBIT,
            stream["id"],
            {
                "currentTreatment": {
                    "method": "혼합 소각",
                    "billing": {"kind": "per_trip", "krwPerTrip": 300000},
                }
            },
            HANBIT,
        )
    assert err.value.code == "INVALID_VALUE"
    view = service.get_view(app, start(app)["session"]["id"], HANBIT)
    shown = next(s for s in view["streams"] if s["id"] == stream["id"])["currentTreatment"]
    assert shown["billing"]["text"] == "월 정액 450,000원" and shown["assumed"] is False
