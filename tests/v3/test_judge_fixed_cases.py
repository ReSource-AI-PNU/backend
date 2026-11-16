"""docs/c-track/pp-validation-cases.md의 C01~C10을 도구 수준에서 확인한다. 수치는 모두 가상이다."""

import re

from tests.v3.judge_fixtures import ORIGIN, assert_grounded, claim, registry, run, stream, vendor

PRESS = {
    "id": "pc_pp_press",
    "material": "PP",
    "form": "압축",
    "maxContaminationPct": 12,
    "maxMoisturePct": 3,
    "unitPriceKrwPerKg": 200,
    "minMonthlyKg": 300,
    "evidence": [],
}


def case(**over):
    base = {"monthlyKg": 500, "monthlyKgBasis": "measured", "form": "압축"}
    return stream(**{**base, **over})


def buyer():
    return vendor(criteria=[PRESS])


def asks(result):
    return [c["ask"] for c in result["checks"] if c["ask"]]


def money(result):
    return [c for c in result["claims"] if c["unit"] in ("원/월", "원/kg")]


def test_c01_raw_defect_with_metal_insert_is_conditional_not_a_confirmed_sale():
    reg = registry()
    s = case(form="가공 전", monthlyKgBasis="estimated", contaminants=[{"label": "금속 인서트"}])
    r = run(reg, "judge_route", stream=s, vendor=buyer())
    assert r["status"] == "conditional"  # 압축품 단가 × 물량을 확정 매각액으로 내지 않는다
    keys = {c["key"] for c in r["checks"] if c["status"] == "conditional"}
    assert {"form", "mixture", "monthly_kg_basis"} <= keys
    assert any("금속 인서트" in a for a in asks(r))
    assert_grounded(reg, r)


def test_c02_pressed_but_quality_unknown_stays_conditional():
    reg = registry()
    r = run(reg, "judge_route", stream=case(measure=None), vendor=buyer())
    assert r["status"] == "conditional"
    assert {"contaminationPct", "moisturePct"} <= {
        c["key"] for c in r["checks"] if c["status"] == "conditional"
    }


def test_c03_flake_is_not_treated_as_pressed_or_pellet():
    reg = registry()
    r = run(reg, "judge_route", stream=case(form="분쇄"), vendor=buyer())
    form = next(c for c in r["checks"] if c["key"] == "form")
    assert (
        r["status"] == "conditional" and form["status"] == "conditional" and "전처리" in form["ask"]
    )


def test_c04_all_conditions_stated_needs_no_question():
    reg = registry()
    r = run(reg, "judge_route", stream=case(), vendor=buyer())
    assert r["status"] == "calculated" and asks(r) == []
    assert claim(r, "monthlyAfter")["value"] == 100000
    assert any(c["key"] == "current_assumed" for c in r["checks"])  # 현행은 가정값이라고 표시
    assert_grounded(reg, r)


def test_c05_estimated_material_is_kept_as_unconfirmed():
    reg = registry()
    r = run(reg, "judge_route", stream=case(materialBasis="estimated"), vendor=buyer())
    assert r["status"] == "conditional" and any(c["key"] == "material_basis" for c in r["checks"])


def test_c06_unknown_form_does_not_pick_the_best_price():
    reg = registry()
    pellet = {**PRESS, "id": "pc_pp_pellet", "form": "펠렛", "unitPriceKrwPerKg": 900}
    r = run(reg, "judge_route", stream=case(form=None), vendor=vendor(criteria=[PRESS, pellet]))
    assert r["status"] == "conditional" and any(
        c["key"] == "form" and c["ask"] for c in r["checks"]
    )
    ident = run(reg, "identify_stream", stream=case(form=None))
    assert ident["output"]["form"] is None and any(c["key"] == "form" for c in ident["checks"])


def test_c07_mixture_becomes_vendor_question_not_a_route_verdict():
    reg = registry()
    s = case(
        material="PP",
        singleMaterial=False,
        contaminants=[{"label": "ABS"}, {"label": "금속 인서트"}],
    )
    r = run(reg, "judge_route", stream=s, vendor=buyer())
    assert r["status"] == "conditional" and r["output"]["route"] not in ("pyrolysis", "incinerate")
    assert any("ABS" in a and "받을 수 있는지" in a for a in asks(r))


def test_c08_unknown_volume_gives_no_totals():
    reg = registry()
    for name, args in (
        ("judge_route", {"stream": case(monthlyKg=None), "vendor": buyer()}),
        ("cost_carbon", {"stream": case(monthlyKg=None), "vendor": buyer(), "origin": ORIGIN}),
        ("check_loading", {"streams": [case(monthlyKg=None)], "vendor": buyer()}),
        ("judge_route", {"stream": case(monthlyKg=0), "vendor": buyer()}),
    ):
        r = run(reg, name, **args)
        assert r["status"] == "needs_information" and money(r) == [], name
    r = run(reg, "optimize_assignment", streams=[case(monthlyKg=None)], vendors=[buyer()])
    assert r["status"] == "needs_information" and r["output"]["assignments"] == []


def test_c09_corrected_material_and_volume_replace_the_old_judgement():
    reg = registry()
    before = run(reg, "judge_route", stream=case(monthlyKgBasis="estimated"), vendor=buyer())
    after = run(reg, "judge_route", stream=case(material="PE", monthlyKg=800), vendor=buyer())
    assert before["output"]["acceptable"] is True
    assert after["output"]["acceptable"] is False and not any(
        c["id"] == "unitPrice" for c in after["claims"]
    )
    assert claim(after, "monthlyKg")["value"] == 800
    assert set(before["evidence"]).isdisjoint(after["evidence"])  # 이전 계산 근거를 덮어쓰지 않는다
    assert reg.get(before["evidence"][0]).version == 1


def test_c10_transport_is_an_assumption_not_a_quote():
    reg = registry()
    r = run(
        reg,
        "cost_carbon",
        stream=case(),
        vendor=vendor(criteria=[PRESS], fleet={"depot": [128.9, 35.1]}),
        origin=ORIGIN,
        pickupsPerMonth=2,
        transportPaidBy="emitter",
    )
    transport = next(c for c in r["checks"] if c["key"] == "transport")
    assert r["status"] == "conditional" and r["output"]["transportAssumed"] is True
    assert "견적이 아닙니다" in transport["text"] and "견적" in transport["ask"]
    assert any(e.startswith("assumption:") for e in claim(r, "monthlyTransport")["evidence"])
    nowhere = run(reg, "cost_carbon", stream=case(), vendor=buyer())
    assert not any(c["id"] == "monthlyTransport" for c in nowhere["claims"])
    assert not re.search(r"\d", transport["ask"])
