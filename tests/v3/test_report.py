"""재활용·탄소 실적 리포트(GET /orgs/{orgId}/report). 시연 시나리오를 규칙 정책으로 확정하고 업체가 검수 결과를 기록한다."""

import pytest

from app.v3 import service
from app.v3.evidence.validator import validate_text
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, approve_all, make_app, start


@pytest.fixture(scope="module")
def confirmed():
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    view = approve_all(app, view)
    sid = view["session"]["id"]
    session = app.store.get_session(sid)
    thread = next(t for t in app.store.list_threads(sid) if t["vendorOrgId"] == SAEGIL)
    stream_id = session["work"]["assigned"][thread["id"]][0]
    service.record_outcome(
        app,
        sid,
        {
            "threadId": thread["id"],
            "streamId": stream_id,
            "inspectedOn": "2026-11-10",
            "actualUnitPriceKrwPerKg": 300,
            "actualKg": 580,
        },
        SAEGIL,
    )
    return app, sid, stream_id


def _ok(app, claim):
    return validate_text(
        "{{c}}", [{**claim, "id": "c"}], app.registry, org_id=HANBIT, today=app.today
    ).ok


def test_report_turns_inspected_outcomes_into_grounded_amounts_and_carbon(confirmed):
    app, _, stream_id = confirmed
    report = service.get_report(app, HANBIT, HANBIT)
    assert len(report["records"]) == 1
    row = report["records"][0]
    assert row["streamId"] == stream_id and row["inspectedOn"] == "2026-11-10"
    assert row["kg"]["value"] == 580 and row["unitPrice"]["value"] == 300
    assert row["amount"]["value"] == 174000
    assert row["carbon"] is not None and row["carbon"]["value"] < 0  # 소각 대신 재활용: 탄소가 준다
    for claim in (row["kg"], row["unitPrice"], row["amount"], row["carbon"]):
        assert _ok(app, claim), claim
    totals = report["totals"]
    assert (
        totals["count"] == 1
        and totals["kg"]["value"] == 580
        and totals["revenue"]["value"] == 174000
    )
    assert totals["fee"] is None and _ok(app, totals["carbon"])
    # 확정한 합의는 예상으로 따로 실린다
    assert {p["streamId"] for p in report["planned"]} >= {stream_id}


def test_report_period_filters_by_inspection_date_and_checks_the_format(confirmed):
    app, _, _ = confirmed
    assert service.get_report(app, HANBIT, HANBIT, "2026-11-11", None)["records"] == []
    assert len(service.get_report(app, HANBIT, HANBIT, "2026-11-01", "2026-11-10")["records"]) == 1
    with pytest.raises(service.ServiceError) as err:
        service.get_report(app, HANBIT, HANBIT, "2026/11/01", None)
    assert err.value.code == "INVALID_VALUE"


def test_report_is_for_the_emitter_itself(confirmed):
    app, _, _ = confirmed
    with pytest.raises(service.ServiceError) as err:
        service.get_report(app, HANBIT, SAEGIL)
    assert err.value.code == "FORBIDDEN"
    with pytest.raises(service.ServiceError) as err:
        service.get_report(app, SAEGIL, SAEGIL)
    assert err.value.code == "WRONG_ROLE"
