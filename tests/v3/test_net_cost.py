"""순비용 = 처리비 − 매각 수익 + 운반비 + 전처리 인건비 (설계안 v3 8.6). 회사와 수치는 모두 가상이다."""

import pytest

from tests.v3.judge_fixtures import claim, registry, run, stream, vendor


def paid_by_emitter(fee=30000, **over):
    return vendor(transport={"paidBy": "emitter", "feePerPickupKrw": fee, "evidence": []}, **over)


def test_transport_in_the_price_costs_nothing_extra():
    reg = registry()
    included = vendor(transport={"paidBy": "vendor", "evidence": []})
    cost = run(reg, "cost_carbon", stream=stream(), vendor=included)
    assert claim(cost, "monthlyAfter")["value"] == 320 * 600
    check = next(c for c in cost["checks"] if c["key"] == "transport")
    assert check["status"] == "met" and "수거 포함" in check["text"]
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[included])
    item = done["output"]["assignments"][0]
    assert item["transportPaidBy"] == "vendor"
    assert claim(done, item["transport"])["value"] == 0
    assert claim(done, item["monthlyAfter"])["value"] == claim(done, item["revenue"])["value"]


def test_separate_transport_fee_is_subtracted_once_per_vendor():
    reg = registry()
    hauler = paid_by_emitter(30000)  # 월 2회 수거: 한 달 운반비는 업체당 한 번만 든다
    a = stream()
    b = stream(id="st_runner", name="런너", monthlyKg=200)
    done = run(reg, "optimize_assignment", streams=[a, b], vendors=[hauler])
    items = {i["streamId"]: i for i in done["output"]["assignments"]}
    assert {i["vendorOrgId"] for i in items.values()} == {hauler["orgId"]}
    shares = [claim(done, i["transport"])["value"] for i in items.values()]
    assert sum(shares) == 60000 and shares == [45000, 15000]  # 물량 비율로 나눈다
    after = sum(claim(done, i["monthlyAfter"])["value"] for i in items.values())
    assert after == 320 * 800 - 60000
    assert claim(done, done["output"]["totals"]["monthlyAfter"])["value"] == after
    # 한 흐름만 볼 때(cost_carbon)는 운반비 전부를 그 흐름에 얹어 보여 준다
    alone = run(reg, "cost_carbon", stream=b, vendor=hauler)
    assert claim(alone, "monthlyTransport")["value"] == 60000
    assert claim(alone, "monthlyAfter")["value"] == 320 * 200 - 60000


def test_keeps_the_current_route_when_transport_eats_the_gain():
    reg = registry()
    small = stream(monthlyKg=100, currentTreatment={"method": "혼합 소각", "unitCostKrwPerKg": 250})
    # 100kg을 kg당 100원에 사 가도, 월 운반비가 더 크면 옮기지 않는다
    buyer = paid_by_emitter(
        20000,
        criteria=[
            {
                "id": "pc_small",
                "material": "PP",
                "unitPriceKrwPerKg": 100,
                "minMonthlyKg": 0,
                "evidence": [],
            }
        ],
    )
    done = run(reg, "optimize_assignment", streams=[small], vendors=[buyer])
    assert done["output"]["assignments"][0]["route"] == "keep"
    cheaper = paid_by_emitter(10000, criteria=buyer["criteria"])
    done = run(reg, "optimize_assignment", streams=[small], vendors=[cheaper])
    item = done["output"]["assignments"][0]
    assert item["route"] == "sell"
    assert claim(done, item["difference"])["value"] == (100 * 100 - 20000) - (-250 * 100)


def test_unknown_transport_fee_is_left_out_and_flagged():
    reg = registry()
    silent = vendor(transport={"paidBy": "emitter", "evidence": []})
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[silent])
    item = done["output"]["assignments"][0]
    assert item["status"] == "conditional" and item["transport"] is None
    assert any(c["key"].startswith("transport:") for c in done["checks"])
    cost = run(reg, "cost_carbon", stream=stream(), vendor=silent)
    check = next(c for c in cost["checks"] if c["key"] == "transport")
    assert check["status"] == "conditional" and "확인 필요" in check["text"]


def test_pretreatment_labor_uses_the_minimum_wage_unless_the_company_gives_its_own():
    reg = registry()
    sorted_by_hand = stream(pretreatHoursPerMonth=6)
    done = run(reg, "optimize_assignment", streams=[sorted_by_hand], vendors=[vendor()])
    item = done["output"]["assignments"][0]
    labor = claim(done, item["labor"])
    assert labor["value"] == 6 * 10320 and labor["unit"] == "원/월"
    assert any(e == "ref:최저임금:시간급@2026" for e in labor["evidence"])
    assert claim(done, item["monthlyAfter"])["value"] == 320 * 600 - 6 * 10320
    own = stream(pretreatHoursPerMonth=6, laborCostKrwPerHour=15000)
    done = run(reg, "optimize_assignment", streams=[own], vendors=[vendor()])
    assert claim(done, done["output"]["assignments"][0]["labor"])["value"] == 90000
    cost = run(reg, "cost_carbon", stream=sorted_by_hand, vendor=vendor())
    assert claim(cost, "monthlyLabor")["value"] == 6 * 10320
    # 작업 시간이 없으면 인건비를 지어내지 않는다
    plain = run(reg, "optimize_assignment", streams=[stream()], vendors=[vendor()])
    assert plain["output"]["assignments"][0]["labor"] is None


def test_labor_can_make_selling_worse_than_keeping():
    reg = registry()
    heavy = stream(monthlyKg=100, pretreatHoursPerMonth=10)  # 인건비 103,200원
    done = run(
        reg,
        "optimize_assignment",
        streams=[heavy],
        vendors=[
            vendor(
                criteria=[
                    {
                        "id": "pc",
                        "material": "PP",
                        "unitPriceKrwPerKg": 320,
                        "minMonthlyKg": 0,
                        "evidence": [],
                    }
                ]
            )
        ],
    )
    # 팔면 +32,000 − 103,200 = −71,200, 지금은 −25,000(혼합 소각 가정값)
    assert done["output"]["assignments"][0]["route"] == "keep"


def test_vendor_monthly_capacity_is_a_constraint():
    reg = registry()
    full = vendor(monthlyCapacityKg=500)
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[full])
    assert done["output"]["assignments"][0]["route"] == "keep"
    roomy = vendor(monthlyCapacityKg=600)
    done = run(reg, "optimize_assignment", streams=[stream()], vendors=[roomy])
    assert done["output"]["assignments"][0]["route"] == "sell"


def test_carbon_needs_a_factor_for_both_sides():
    reg = registry()
    base = run(reg, "cost_carbon", stream=stream(), vendor=vendor())
    assert claim(base, "carbonDifference")["value"] == pytest.approx(
        (0.0186 - 3.412554039) * 0.6, abs=1e-6
    )
    # 열분해는 공식 표에 계수가 없다
    pyro = run(reg, "cost_carbon", stream=stream(), vendor=vendor(process="열분해"))
    assert pyro["output"]["carbon"] is None
    assert any(c["key"] == "carbon" and "확인 필요" in c["text"] for c in pyro["checks"])
    # 현행이 소각도 매립도 아니면 현행 쪽 계수가 없다
    reused = stream(currentTreatment={"method": "위탁 재활용", "unitCostKrwPerKg": 0})
    other = run(reg, "cost_carbon", stream=reused, vendor=vendor())
    assert other["output"]["carbon"] is None
    landfill = stream(currentTreatment={"method": "매립", "unitCostKrwPerKg": 100})
    buried = run(reg, "cost_carbon", stream=landfill, vendor=vendor())
    assert claim(buried, "carbonDifference")["value"] == pytest.approx(
        (0.0186 - 0.013203539) * 0.6, abs=1e-6
    )


# ── 현행 처리비 질문 ─────────────────────────────────────────────────────
def test_current_cost_answer_replaces_the_assumption_and_stays_with_the_emitter():
    import json

    from app.v3 import service
    from app.v3.agents import questions
    from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, make_app, start

    app = make_app()
    view = answer_all(app, start(app))  # 첫 묶음(수분)에 답하면 현행 처리비를 묻는다
    sid = view["session"]["id"]
    asked = [q for q in view["questions"] if not q["answered"]]
    assert {(q["streamId"], q["field"]) for q in asked} == {
        ("st_film", "currentCost"),
        ("st_purge", "currentCost"),
    }
    assert all(q["unit"] == "원/kg" and q["batch"] == 2 for q in asked)
    for question in asked:
        value = 180 if question["streamId"] == "st_film" else "unknown"
        service.answer_question(app, sid, question["id"], value, HANBIT)
    view = service.run_session(app, sid, HANBIT)
    assert view["session"]["status"] == "awaiting_approval"
    by_stream = {a["streamId"]: a for a in view["result"]["assignments"]}
    film, purge = by_stream["st_film"], by_stream["st_purge"]
    assert film["currentAssumed"] is False and film["monthlyNow"]["value"] == -180 * 300
    assert "human:org_hanbit:st_film-현행처리비" in film["monthlyNow"]["evidence"]
    assert purge["currentAssumed"] is True  # 모름이면 혼합 소각 가정값을 그대로 쓴다
    assert any(c["key"] == "current_method" for c in film["checks"])
    # 업체에게는 현행 처리비도, 그 답의 근거도 보이지 않는다
    seen = json.dumps(service.get_view(app, sid, SAEGIL), ensure_ascii=False)
    assert "현행처리비" not in seen and "현행 처리비" not in seen
    # 업체는 현행 처리비를 물을 수 없다
    assert "currentCost" not in questions.ASK_FIELDS
    assert questions.clean_asks([{"streamId": "st_film", "field": "currentCost"}]) == []
