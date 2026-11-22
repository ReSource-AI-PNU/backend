"""검수 한 건마다 고유 ID: 같은 날 여러 번 반입, 재전송, 정정, 검수일 키로 만든 이전 기록. 수치는 모두 가상이다.

2026-11-19 재현: 같은 진단·폐기물에 같은 검수일로 580kg과 200kg을 기록하면 검수일 키가 같아 2건·780kg 대신
1건·200kg만 남았고, 300원/kg 매각 수익도 234,000원 대신 60,000원이었다.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from app.v3 import devserver, feedback, ids, outcome_carbon, service
from app.v3.evidence.model import Evidence
from app.v3.evidence.validator import validate_text
from tests.v3.evidence_fixtures import numerical_history_app as make_app
from tests.v3.evidence_fixtures import simulated_legacy_factor_gate  # noqa: F401
from tests.v3.runtime_fixtures import (
    DAON,
    HANBIT,
    MISO,
    SAEGIL,
    approve_all,
    thread_of,
    to_approval,
)

DAY = "2026-11-16"


def fails(code, status, call, *args):
    with pytest.raises(service.ServiceError) as error:
        call(*args)
    assert (error.value.code, error.value.status) == (code, status), error.value.to_dict()
    return error.value


@pytest.fixture
def confirmed():
    app = make_app()
    view = approve_all(app, to_approval(app))
    assert view["session"]["status"] == "confirmed"
    return app, view


def body(view, request_id, kg, **over):
    found = {
        "requestId": request_id,
        "threadId": thread_of(view, SAEGIL, "confirmed")["id"],
        "streamId": "st_sprue",
        "actualUnitPriceKrwPerKg": 300,
        "actualKg": kg,
        "inspectedOn": DAY,
    }
    found.update(over)
    return found


def totals(app):
    report = service.get_report(app, HANBIT, HANBIT)
    t = report["totals"]
    return t["count"], t["kg"]["value"], t["revenue"]["value"], report


def ok(app, claim):
    return validate_text(
        "{{c}}", [{**claim, "id": "c"}], app.registry, org_id=HANBIT, today=app.today
    ).ok


def test_two_deliveries_on_the_same_day_are_two_inspections(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    first = service.record_outcome(app, sid, body(view, "req-first-0001", 580), SAEGIL)
    second = service.record_outcome(app, sid, body(view, "req-second-0001", 200), SAEGIL)
    a, b = first["outcome"], second["outcome"]
    assert a["inspectionId"] != b["inspectionId"]
    assert a["id"] == a["inspectionId"] and a["revision"] == b["revision"] == 1
    assert first["replayed"] is second["replayed"] is False
    assert set(first["evidence"]).isdisjoint(second["evidence"])  # 근거도 건마다 따로
    # 화면(진단 뷰)과 실적 API 모두 2건, 780kg, 234,000원
    for viewer in (HANBIT, SAEGIL):
        shown = service.get_view(app, sid, viewer)["feedback"]["outcomes"]
        assert [o["actualKg"] for o in shown] == [580, 200]
    count, kg, revenue, report = totals(app)
    assert (count, kg, revenue) == (2, 780, 234000)
    assert [r["inspectionId"] for r in report["records"]] == [a["inspectionId"], b["inspectionId"]]
    for row in report["records"]:
        assert ok(app, row["amount"]) and row["revision"] == 1 and row["history"] == []
    for claim in (report["totals"]["kg"], report["totals"]["revenue"]):
        assert ok(app, claim)


def test_resending_the_same_registration_does_not_add_a_second_record(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    service.record_outcome(app, sid, body(view, "req-first-0001", 580), SAEGIL)
    saved = service.record_outcome(app, sid, body(view, "req-second-0001", 200), SAEGIL)
    events = len(app.store.list_events(sid))
    # 네트워크 재시도·두 번 누름: 같은 요청 번호와 같은 내용
    again = service.record_outcome(app, sid, body(view, "req-second-0001", 200), SAEGIL)
    assert again["replayed"] is True
    assert again["outcome"]["inspectionId"] == saved["outcome"]["inspectionId"]
    assert again["evidence"] == saved["evidence"]
    assert all(app.registry.get(e).version == 1 for e in saved["evidence"])  # 근거도 그대로
    assert len(app.store.list_events(sid)) == events  # 기록 알림도 한 번
    assert totals(app)[:3] == (2, 780, 234000)
    # 같은 번호로 다른 내용을 보내면 새 반입으로 받지 않는다
    fails(
        "REQUEST_ID_REUSED",
        409,
        service.record_outcome,
        app,
        sid,
        body(view, "req-second-0001", 210),
        SAEGIL,
    )
    assert totals(app)[:3] == (2, 780, 234000)


def test_requests_without_a_request_id_still_register_new_deliveries(confirmed):
    """하위 호환: requestId를 보내지 않는 예전 화면도 새 반입 한 건씩 기록되고 앞 기록을 고치지 않는다."""
    app, view = confirmed
    sid = view["session"]["id"]
    old_screen = {k: v for k, v in body(view, "unused-0000", 580).items() if k != "requestId"}
    first = service.record_outcome(app, sid, old_screen, SAEGIL)["outcome"]
    second = service.record_outcome(app, sid, {**old_screen, "actualKg": 200}, SAEGIL)["outcome"]
    same = service.record_outcome(app, sid, {**old_screen, "actualKg": 200}, SAEGIL)["outcome"]
    assert first["requestId"].startswith("auto-") and first["revision"] == 1
    # 번호가 없으면 재전송을 묶지 못한다(그래서 새 화면은 늘 보낸다). 대신 기존 기록을 덮지 않는다
    assert len({first["inspectionId"], second["inspectionId"], same["inspectionId"]}) == 3
    assert totals(app)[:3] == (3, 980, 294000)


def test_correction_makes_a_new_revision_and_keeps_the_first_record(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    service.record_outcome(app, sid, body(view, "req-first-0001", 580), SAEGIL)
    second = service.record_outcome(app, sid, body(view, "req-second-0001", 200), SAEGIL)
    original = second["outcome"]
    inspection = original["inspectionId"]
    fix = {
        **body(view, "req-fix-0001", 180),
        "baseRevision": 1,
        "reason": "계량표 재확인(200kg은 오기)",
    }
    fixed = service.correct_outcome(app, sid, inspection, fix, SAEGIL)
    record = fixed["outcome"]
    assert fixed["replayed"] is False
    assert (record["inspectionId"], record["revision"], record["supersedes"]) == (inspection, 2, 1)
    assert record["actualKg"] == 180 and record["reason"] == "계량표 재확인(200kg은 오기)"
    # 정정 이력: 처음 200kg 기록이 남고, 그 근거를 그대로 열 수 있다
    (before,) = record["history"]
    assert before["revision"] == 1 and before["actualKg"] == 200
    assert before["evidence"] == original["evidence"]
    old_kg = service.get_evidence(app, original["evidence"]["kg"], HANBIT)["evidence"]
    assert old_kg["value"] == 200 and old_kg["version"] == 1
    new_kg = service.get_evidence(app, record["evidence"]["kg"], HANBIT)["evidence"]
    assert new_kg["value"] == 180 and "정정" in new_kg["excerpt"]
    assert record["evidence"]["kg"] != original["evidence"]["kg"]
    # 실적은 유효한 최신 판만 한 번: 2건, 760kg, 228,000원
    count, kg, revenue, report = totals(app)
    assert (count, kg, revenue) == (2, 760, 228000)
    row = next(r for r in report["records"] if r["inspectionId"] == inspection)
    assert row["revision"] == 2 and row["kg"]["value"] == 180 and ok(app, row["amount"])
    assert row["history"][0]["kg"] == 200 and row["correctionReason"].startswith("계량표")
    # 양쪽 화면에도 검수 한 건이 한 줄이고 이력이 붙어 있다
    for viewer in (HANBIT, SAEGIL):
        shown = service.get_view(app, sid, viewer)["feedback"]["outcomes"]
        assert sorted(o["actualKg"] for o in shown) == [180, 580]
        assert next(o for o in shown if o["inspectionId"] == inspection)["history"]
    # 재학습 쪽도 검수 건마다 최신 판 한 줄이다. 시험 조직은 시연 회사라 기준값 보정 후보에는 들지 않는다
    assert feedback.calibration_candidates(app)["count"] == 0
    demo = feedback.outcomes(app, origin="demo")
    assert len(demo) == 2 and all(r["dataOrigin"] == "demo" for r in demo)


def test_correction_resend_conflict_and_validation(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    saved = service.record_outcome(app, sid, body(view, "req-second-0001", 200), SAEGIL)
    inspection = saved["outcome"]["inspectionId"]
    fix = {**body(view, "req-fix-0001", 180), "baseRevision": 1, "reason": "오기"}
    service.correct_outcome(app, sid, inspection, fix, SAEGIL)
    # 같은 정정을 다시 보내면(응답을 못 받아 재시도) 3판을 만들지 않는다
    again = service.correct_outcome(app, sid, inspection, fix, SAEGIL)
    assert again["replayed"] is True and again["outcome"]["revision"] == 2
    # 다른 담당자가 1판을 보고 고친 정정은 겹친다고 알린다
    stale = {**fix, "requestId": "req-fix-0002", "actualKg": 190}
    error = fails(
        "REVISION_CONFLICT", 409, service.correct_outcome, app, sid, inspection, stale, SAEGIL
    )
    assert "2판" in error.message
    latest = {**stale, "baseRevision": 2}
    for patch, code, status in (
        ({"reason": ""}, "INVALID_VALUE", 422),
        ({"baseRevision": None}, "INVALID_VALUE", 422),
        ({"actualKg": 180}, "NO_CHANGE", 422),
        ({"streamId": "st_film"}, "INVALID_VALUE", 422),  # 정정으로 폐기물을 바꿀 수 없다
        ({"requestId": "req-fix-0001"}, "REQUEST_ID_REUSED", 409),
        ({"requestId": "req-second-0001"}, "REQUEST_ID_REUSED", 409),  # 등록 번호를 정정에 다시 씀
    ):
        fails(
            code, status, service.correct_outcome, app, sid, inspection, {**latest, **patch}, SAEGIL
        )
    # 배출 기업은 보지만 고치지 못하고, 당사자가 아닌 조직에는 없는 기록이다
    fails("FORBIDDEN", 403, service.correct_outcome, app, sid, inspection, latest, HANBIT)
    fails("OUTCOME_NOT_FOUND", 404, service.correct_outcome, app, sid, inspection, latest, MISO)
    fails("SESSION_NOT_FOUND", 404, service.correct_outcome, app, sid, inspection, latest, DAON)
    fails("OUTCOME_NOT_FOUND", 404, service.correct_outcome, app, sid, "insp-없음", latest, SAEGIL)
    assert totals(app)[:3] == (1, 180, 54000)


def test_weigh_ticket_catches_the_same_slip_entered_twice(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    service.record_outcome(
        app, sid, body(view, "req-first-0001", 580, weighTicket="T-0007"), SAEGIL
    )
    # 다른 요청 번호로 같은 계량표를 손으로 다시 적은 경우
    fails(
        "DUPLICATE_TICKET",
        409,
        service.record_outcome,
        app,
        sid,
        body(view, "req-first-0002", 580, weighTicket="T-0007"),
        SAEGIL,
    )
    # 날마다 번호가 다시 시작하는 계량표: 다른 날 같은 번호는 다른 반입이다
    later = service.record_outcome(
        app,
        sid,
        body(view, "req-third-0001", 300, weighTicket="T-0007", inspectedOn="2026-11-17"),
        SAEGIL,
    )
    assert later["outcome"]["weighTicket"] == "T-0007"
    assert totals(app)[0] == 2


def test_each_inspection_has_its_own_carbon_basis_kept_through_corrections(confirmed):
    """검수 시점의 탄소 계산 기준(배출 기업만 보는 calc 근거)이 검수 ID마다 따로 붙는다."""
    app, view = confirmed
    sid = view["session"]["id"]
    a = service.record_outcome(app, sid, body(view, "req-first-0001", 580), SAEGIL)["outcome"]
    b = service.record_outcome(app, sid, body(view, "req-second-0001", 200), SAEGIL)["outcome"]
    basis_a = outcome_carbon.latest(app.registry, a)
    basis_b = outcome_carbon.latest(app.registry, b)
    assert basis_a["evidenceId"] != basis_b["evidenceId"]
    assert basis_a["outcomeId"] == a["inspectionId"] and basis_b["outcomeId"] == b["inspectionId"]
    # 같은 날 들어온 두 번째 반입 뒤에 현행 처리를 고쳐도, 정정해도 각 검수의 기준은 그대로다
    service.update_stream(
        app,
        HANBIT,
        "st_sprue",
        {"currentTreatment": {"method": "매립", "unitCostKrwPerKg": 200}},
        HANBIT,
    )
    fix = {**body(view, "req-fix-0001", 180), "baseRevision": 1, "reason": "오기"}
    service.correct_outcome(app, sid, b["inspectionId"], fix, SAEGIL)
    rows = {r["inspectionId"]: r for r in service.get_report(app, HANBIT, HANBIT)["records"]}
    for inspection, basis in ((a["inspectionId"], basis_a), (b["inspectionId"], basis_b)):
        row = rows[inspection]
        assert row["carbonBasis"]["evidenceId"] == basis["evidenceId"]
        assert row["carbonBasis"]["revision"] == 1 and row["currentMethod"] == "혼합 소각"
        assert basis["evidenceId"] in row["carbon"]["evidence"] and ok(app, row["carbon"])
    per_kg = basis_b["perKg"]  # kg당 배출 차이(kgCO2e/kg)
    assert rows[b["inspectionId"]]["carbon"]["value"] == round(per_kg * 180 / 1000, 6)
    # 기준은 배출 기업만 연다(업체와 나누는 기록에는 현행 처리를 싣지 않는다)
    fails("EVIDENCE_NOT_FOUND", 404, service.get_evidence, app, basis_b["evidenceId"], SAEGIL)


def _legacy_outcome(app, view, inspected, kg, price):
    """2026-11-19 전 코드가 남기던 모양 그대로(검수일 키, inspectionId 없음)."""
    sid = view["session"]["id"]
    thread = thread_of(view, SAEGIL, "confirmed")
    key = f"실제결과-{sid}-st_sprue-{inspected}"
    parts = {
        "unitPrice": ids.human_id(SAEGIL, f"{key}-단가"),
        "kg": ids.human_id(SAEGIL, f"{key}-중량"),
    }
    record = {
        "type": "outcome",
        "id": key,
        "sessionId": sid,
        "threadId": thread["id"],
        "vendorOrgId": SAEGIL,
        "emitterOrgId": HANBIT,
        "streamId": "st_sprue",
        "inspectedOn": inspected,
        "actualGrade": None,
        "actualUnitPriceKrwPerKg": float(price),
        "actualKg": float(kg),
        "note": "",
        "evidenceKind": "human",
        "evidence": parts,
        "features": {"material": "PP", "contaminationPct": 1.0, "moisturePct": 0.3},
        "criterionId": None,
        "tablePrice": None,
        "offeredPrice": None,
        "deviation": None,
        "recordedOn": "2026-11-15",
    }
    for part, (label, value, unit) in {
        "unitPrice": ("실제 단가", float(price), "원/kg"),
        "kg": ("실제 중량", float(kg), "kg"),
    }.items():
        app.registry.put(
            Evidence(
                id=parts[part],
                kind="human",
                label=f"사출 스프루 {label}",
                value=value,
                unit=unit,
                excerpt=f"{inspected} 검수: 사출 스프루 {label}",
                source="업체 담당자 입력(확정 뒤 실제 결과)",
                as_of=inspected,
                access=(SAEGIL, HANBIT),
                meta={"ownerOrgId": SAEGIL, "feedback": {**record, "part": part}},
            )
        )
    return key, parts


def test_legacy_date_keyed_record_is_still_read_once_and_kept(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    key, parts = _legacy_outcome(app, view, "2026-11-15", 500, 300)
    (old,) = feedback.outcomes(app)
    assert (
        old["inspectionId"] == old["id"] == key and old["legacy"] is True and old["revision"] == 1
    )
    # 이전 기록이 실적에 한 번 들어가고, 이전 근거 ID가 그대로 계산 사슬에 이어진다
    count, kg, revenue, report = totals(app)
    assert (count, kg, revenue) == (1, 500, 150000)
    row = report["records"][0]
    assert (
        row["legacy"] is True and row["kg"]["evidence"] == [parts["kg"]] and ok(app, row["amount"])
    )
    chain = {
        c["id"] for c in service.get_evidence(app, row["amount"]["evidence"][0], HANBIT)["chain"]
    }
    assert {parts["kg"], parts["unitPrice"]} <= chain
    # 같은 날 새 반입을 등록해도 이전 기록을 고친 것으로 보지 않는다
    service.record_outcome(
        app, sid, body(view, "req-new-0001", 200, inspectedOn="2026-11-15"), SAEGIL
    )
    assert totals(app)[:3] == (2, 700, 210000)
    assert all(app.registry.get(e).version == 1 for e in parts.values())
    # 이전 기록도 검수 ID(이전 키)로 정정하고, 처음 근거는 그대로 남는다
    fixed = service.correct_outcome(
        app,
        sid,
        key,
        {
            **body(view, "req-fix-legacy-1", 480, inspectedOn="2026-11-15"),
            "baseRevision": 1,
            "reason": "계량 오차",
        },
        SAEGIL,
    )["outcome"]
    assert fixed["inspectionId"] == key and fixed["revision"] == 2 and fixed["legacy"] is True
    assert fixed["evidence"]["kg"] == ids.human_id(SAEGIL, f"{key}-r2-중량")
    assert fixed["history"][0]["evidence"] == parts
    assert app.registry.get(parts["kg"]).value == 500
    assert totals(app)[:3] == (2, 680, 204000)


def test_legacy_without_computed_agreement_stays_held_after_correction(confirmed):
    """합의에 탄소 계산이 없으면 오늘 설정으로 과거 수치를 만들지 않는다."""
    app, view = confirmed
    sid = view["session"]["id"]
    key, _ = _legacy_outcome(app, view, "2026-11-15", 500, 300)
    service.update_stream(  # 지금 설정을 바꿔 두어도 이전 기록의 기준은 합의 당시의 것이다
        app,
        HANBIT,
        "st_sprue",
        {"currentTreatment": {"method": "매립", "unitCostKrwPerKg": 200}},
        HANBIT,
    )
    (row,) = service.get_report(app, HANBIT, HANBIT)["records"]
    assert row["carbonBasis"]["status"] == "unverified" and row["carbon"] is None
    service.correct_outcome(
        app,
        sid,
        key,
        {
            **body(view, "req-fix-legacy-1", 480, inspectedOn="2026-11-15"),
            "baseRevision": 1,
            "reason": "계량 오차",
        },
        SAEGIL,
    )
    (row,) = service.get_report(app, HANBIT, HANBIT)["records"]
    assert row["revision"] == 2 and row["carbonBasis"]["status"] == "unverified"
    assert row["carbon"] is None and row["amount"]["value"] == 480 * 300


def test_demo_history_still_records_its_inspections():
    """시연 지난 거래는 실패를 삼키므로(시연 되돌리기를 멈추지 않게) 검수 기록이 실제로 남는지 따로 본다."""
    app = make_app()
    sid = service._demo_history(app)
    assert sid is not None
    found = feedback.outcomes(app, sid)
    per_stream = {}
    for r in found:
        per_stream[r["streamId"]] = per_stream.get(r["streamId"], 0) + 1
    # 회사 기록 시연 폐기물은 세 번, 나머지는 두 번 검수한다
    assert per_stream and all(
        n
        == (
            len(service.DEMO_BELOW)
            if stream == service.DEMO_BELOW_STREAM
            else len(service.DEMO_HISTORY_DAYS_AGO)
        )
        for stream, n in per_stream.items()
    )
    assert len({r["inspectionId"] for r in found}) == len(found)
    assert all(r["revision"] == 1 and r["requestId"].startswith("demo-") for r in found)


def test_devserver_routes_register_resend_and_correct(confirmed):
    app, view = confirmed
    server = devserver.make_server(app, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"

    def call(method, path, payload, org, expect=200):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"X-Org-Id": org, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, data = response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            status, data = error.code, json.loads(error.read())
        assert status == expect, data
        return data

    try:
        sid = view["session"]["id"]
        path = f"/sessions/{sid}/outcomes"
        call("POST", path, body(view, "req-first-0001", 580), SAEGIL)
        saved = call("POST", path, body(view, "req-second-0001", 200), SAEGIL)
        again = call("POST", path, body(view, "req-second-0001", 200), SAEGIL)
        assert again["replayed"] is True
        inspection = saved["outcome"]["inspectionId"]
        fix = {**body(view, "req-fix-0001", 180), "baseRevision": 1, "reason": "오기"}
        fixed = call("PUT", f"{path}/{inspection}", fix, SAEGIL)
        assert fixed["outcome"]["revision"] == 2
        denied = call("PUT", f"{path}/{inspection}", fix, HANBIT, 403)
        assert denied["error"]["code"] == "FORBIDDEN"
        assert totals(app)[:3] == (2, 760, 228000)
    finally:
        server.shutdown()
        server.server_close()
