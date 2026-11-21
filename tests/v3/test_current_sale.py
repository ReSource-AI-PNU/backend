"""지금 재활용 업체에 팔고 돈을 받는 현행 처리(2026-11-19 설문: "지금 처리: 재활용 업체에 판다").

현행 처리비만 받던 때에는 판매 중인 사업장이 현행을 비우면 혼합 소각 가정값(내는 돈)으로, 0원으로 적으면
판매 수입이 빠진 채로 견줘 "현행 대비 월 이익"이 크게 부풀었다.
"""

import pytest

from app.v3 import current_cost, service
from app.v3.agents import questions
from app.v3.judge import route
from app.v3.judge.common import current_process
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app, to_approval

SPRUE_KG = 600  # 시연 스프루·런너 월 발생량. 새길자원 매입 단가는 kg당 320원


def sell_now(app, price, stream_id="st_sprue"):
    return service.update_stream(
        app,
        HANBIT,
        stream_id,
        {"currentTreatment": {"sale": True, "salePriceKrwPerKg": price}},
        HANBIT,
    )


def assignment(view, stream_id="st_sprue"):
    return next(a for a in view["result"]["assignments"] if a["streamId"] == stream_id)


def test_selling_now_is_the_baseline_and_the_gain_is_only_the_price_difference():
    app = make_app()
    saved = sell_now(app, 300)
    assert saved["currentTreatment"]["sale"] is True
    assert saved["currentTreatment"]["method"] == current_cost.SALE_METHOD
    view = to_approval(app)
    sprue = assignment(view)
    assert sprue["vendorOrgId"] == SAEGIL and sprue["unitPrice"]["value"] == 320
    # 현행 월 손익은 판매 수입(+), 차이는 변경 뒤 손익에서 그 수입을 뺀 값
    assert sprue["monthlyNow"]["value"] == 300 * SPRUE_KG
    assert sprue["difference"]["value"] == pytest.approx(
        sprue["monthlyAfter"]["value"] - 300 * SPRUE_KG
    )
    assert sprue["currentAssumed"] is False
    # 지금 판매는 재활용으로 견준다(현행 처리 방식은 확인됨). 원문을 확인한 처리 배출계수가 없어 탄소는 0으로
    # 두지 않고 보류한다
    stream = app.store.get_stream("st_sprue", HANBIT)
    assert current_process(route.current_cost(None, stream)[3]) == "recycle"
    assert sprue["carbonProcess"] is None and sprue["carbon"] is None
    carbon = next(c for c in sprue["checks"] if c["key"] == "carbon")
    assert carbon["status"] == "conditional" and "현행 처리 방식" not in carbon["text"]


def test_a_buyer_paying_less_than_today_is_not_recommended():
    app = make_app()
    sell_now(app, 400)  # 지금 kg당 400원을 받는데 새길자원은 320원
    view = to_approval(app)
    sprue = assignment(view)
    assert sprue["vendorOrgId"] is None and sprue["route"] != "sell"


def test_the_view_shows_what_the_emitter_receives_and_the_vendor_sees_nothing():
    app = make_app()
    sell_now(app, 300)
    view = to_approval(app)
    shown = next(s for s in view["streams"] if s["id"] == "st_sprue")["currentTreatment"]
    assert shown["sale"] is True and shown["unitCost"] is None and shown["assumed"] is False
    assert shown["unitPrice"]["value"] == 300 and shown["unitPrice"]["role"] == "currentUnitPrice"
    assert shown["unitPrice"]["label"].startswith("현행 판매 단가")
    theirs = service.get_view(app, view["session"]["id"], SAEGIL)
    assert all(s["currentTreatment"] is None for s in theirs["streams"])
    assert "currentUnitPrice" not in str(theirs)


def test_switching_between_paying_and_selling_keeps_separate_evidence():
    app = make_app()
    paid = service.update_stream(
        app,
        HANBIT,
        "st_sprue",
        {"currentTreatment": {"method": "위탁 소각", "unitCostKrwPerKg": 200}},
        HANBIT,
    )
    sold = sell_now(app, 200)
    assert sold["currentTreatment"]["evidence"] != paid["currentTreatment"]["evidence"]
    said = app.registry.get(sold["currentTreatment"]["evidence"][0], HANBIT)
    assert said.label.startswith("현행 판매 단가") and SAEGIL not in said.access
    with pytest.raises(service.ServiceError) as err:
        sell_now(app, -10)
    assert err.value.code == "INVALID_VALUE"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("판매 kg당 200원", {"salePriceKrwPerKg": 200.0}),
        ("톤당 20만원에 팔아요", {"salePriceKrwPerKg": 200.0}),
        ("200원 받고 팔고 있어요", {"salePriceKrwPerKg": 200.0}),
        ("업체가 kg당 250원 받아요", 250.0),  # 업체가 받는다 = 배출 기업이 낸다
        (180, 180.0),
    ],
)
def test_question_answers_tell_selling_from_paying(answer, expected):
    assert questions.parse_answer("currentCost", answer) == expected


def test_answering_selling_sets_the_current_treatment():
    app = make_app()
    stream = app.store.get_stream("st_sprue", HANBIT)
    value = questions.parse_answer("currentCost", "판매 kg당 210원")
    saved = questions.apply_answer(app.registry, stream, "currentCost", value, app.today)
    current = saved["currentTreatment"]
    assert current["sale"] is True and current["salePriceKrwPerKg"] == 210
    assert current_cost.signed_cost(current) == -210
    assert questions.known(saved, "currentCost")


def test_a_paying_method_left_in_the_form_is_stored_as_selling():
    """시연 데이터처럼 처리 방법 칸에 "혼합 소각"이 남은 채 판매를 저장해도 지금을 소각으로 견주지 않는다."""
    app = make_app()
    saved = service.update_stream(
        app,
        HANBIT,
        "st_sprue",
        {"currentTreatment": {"method": "혼합 소각", "sale": True, "salePriceKrwPerKg": 300}},
        HANBIT,
    )
    assert saved["currentTreatment"]["method"] == current_cost.SALE_METHOD
    assert current_cost.sale_method("고물상 판매") == "고물상 판매"
    # 저장된 방법 이름이 소각이어도(옛 기록) 탄소는 재활용으로 견준다
    stream = app.store.get_stream("st_sprue", HANBIT)
    stream["currentTreatment"]["method"] = "혼합 소각"
    app.store.put_stream(stream)
    assert current_process(route.current_cost(None, stream)[3]) == "recycle"
    # 원문 확인 배출계수가 없어 처리 탄소는 보류(0으로도, 소각 대비 감축으로도 두지 않는다)
    assert assignment(to_approval(app))["carbonProcess"] is None


def test_carbon_reads_selling_as_recycling_but_leaves_other_methods_alone():
    assert current_process(current_cost.SALE_METHOD) == "recycle"
    # 처리 방법 이름에 재활용·열분해가 적혀 있으면 그대로 읽는다. 무엇인지 모르는 이름은 읽지 않는다
    assert current_process("위탁 재활용") == "recycle"
    assert current_process("위탁 처리") is None
    assert current_process("혼합 소각") == "incinerate"
