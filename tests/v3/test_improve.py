"""분리 개선 안내(결과 한 장의 improvements). 시연 시나리오를 규칙 정책으로 돌린다."""

import pytest

from app.v3 import service
from app.v3.evidence.validator import validate_text
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, make_app, start


@pytest.fixture(scope="module")
def waiting():
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    return app, view


def _valid(app, claim):
    return validate_text(
        "{{c}}", [{**claim, "id": "c"}], app.registry, org_id=HANBIT, today=app.today
    ).ok


def test_a_higher_grade_option_names_what_to_fix_and_the_monthly_gain(waiting):
    app, view = waiting
    by = {i["streamId"]: i for i in view["result"]["improvements"]}
    defect = by["st_defect"]
    option = defect["option"]
    assigned = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_defect")
    assert option["unitPrice"]["value"] > assigned["unitPrice"]["value"]
    assert option["needs"] == ["오염도"] and option["maxContamination"] is not None
    stream = next(s for s in view["streams"] if s["id"] == "st_defect")
    expected = round(
        (option["unitPrice"]["value"] - assigned["unitPrice"]["value"])
        * stream["monthlyKg"]["value"]
    )
    assert option["monthlyGain"]["value"] == expected
    for claim in (option["unitPrice"], option["maxContamination"], option["monthlyGain"]):
        assert _valid(app, claim), claim
    keys = [a["key"] for a in defect["actions"]]
    assert "lower_contamination" in keys and "measure" in keys
    assert all(not any(ch.isdigit() for ch in a["text"]) for a in defect["actions"])


def test_mixed_material_gets_per_material_split_options_without_a_made_up_total(waiting):
    app, view = waiting
    purge = next(i for i in view["result"]["improvements"] if i["streamId"] == "st_purge")
    assert "separate_material" in [a["key"] for a in purge["actions"]]
    parts = {s["material"]: s for s in purge["split"]}
    assert set(parts) == {"PP", "ABS"}
    pp = parts["PP"]
    assert (
        pp["vendorOrgId"] and _valid(app, pp["unitPrice"]) and _valid(app, pp["maxContamination"])
    )
    assert pp["meetsNow"] is False  # 사진 추정 오염도로는 어느 기준에도 못 미친다
    assert parts["ABS"]["vendorOrgId"] is None  # 대화한 업체 가운데 ABS를 받는 곳이 없다
    assert "monthlyGain" not in pp  # 나눈 무게 비율을 모르므로 한 달 금액을 만들지 않는다


def test_vendor_view_does_not_carry_improvements(waiting):
    app, view = waiting
    vendor_view = service.get_view(app, view["session"]["id"], SAEGIL)
    assert "improvements" not in (vendor_view["result"] or {})
