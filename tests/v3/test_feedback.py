"""재학습 데이터 수집(7단계): 교정, 실제 결과, 내보내기, 배포 게이트, 관리자 표. 수치는 모두 가상이다."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from app.v3 import devserver, feedback, service
from app.v3.evidence.validator import validate_text
from app.v3.intake import grade_model
from tests.v3.runtime_fixtures import (
    DAON,
    HANBIT,
    MISO,
    SAEGIL,
    TODAY,
    approve_all,
    make_app,
    thread_of,
    to_approval,
)

ADMIN = "org_admin"


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


def outcome_body(view, vendor=SAEGIL, stream="st_sprue", **over):
    body = {
        "threadId": thread_of(view, vendor, "confirmed")["id"],
        "streamId": stream,
        "actualGrade": "A",
        "actualUnitPriceKrwPerKg": 300,
        "actualKg": 580,
        "inspectedOn": "2026-11-16",
        "note": "가상 검수 기록",
    }
    body.update(over)
    return body


def test_correction_becomes_human_evidence_and_detector_candidate(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    done = service.record_correction(
        app,
        sid,
        {"streamId": "st_defect", "field": "material", "before": "PP", "after": "PP+ABS"},
        HANBIT,
    )
    record = done["correction"]
    assert record["id"].startswith("human:org_hanbit:") and done["evidence"] == [record["id"]]
    found = app.registry.get(record["id"], HANBIT)
    assert found.kind == "human" and found.value == "PP+ABS" and found.access == (HANBIT,)
    # 흐름 값은 바꾸지 않는다(기록만 남긴다)
    assert app.store.get_stream("st_defect")["material"] == "PP"
    box = {"x": 10, "y": 20, "w": 30, "h": 40, "label": "금속 인서트"}
    service.record_correction(
        app, sid, {"streamId": "st_defect", "field": "box", "before": None, "after": box}, HANBIT
    )
    service.record_correction(
        app, sid, {"streamId": "st_defect", "field": "grade", "before": "B", "after": "C"}, HANBIT
    )
    rows = feedback.export_detection_corrections(app)
    assert [r["field"] for r in rows] == ["box", "material"]  # 등급 교정은 탐지 후보가 아니다
    assert all(r["hasPhoto"] and r["photoEvidence"][0].startswith("model:") for r in rows)
    assert rows[0]["after"] == box
    # 같은 항목을 다시 고치면 새 근거가 된다(앞 기록을 덮지 않는다)
    again = service.record_correction(
        app,
        sid,
        {"streamId": "st_defect", "field": "material", "before": "PP+ABS", "after": "PP"},
        HANBIT,
    )["correction"]
    assert again["seq"] == 2 and again["id"] != record["id"]
    mine = service.get_view(app, sid, HANBIT)
    assert len(mine["feedback"]["corrections"]) == 4
    assert any(e["kind"] == "feedback" for e in mine["events"])
    # 업체에는 교정 기록이 보이지 않는다
    theirs = service.get_view(app, sid, SAEGIL)
    assert theirs["feedback"]["corrections"] == []
    assert not any(e["kind"] == "feedback" for e in theirs["events"])
    fails("EVIDENCE_NOT_FOUND", 404, service.get_evidence, app, record["id"], SAEGIL)


def test_correction_permissions_and_validation(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    ok = {"streamId": "st_sprue", "field": "origin", "before": "PIR", "after": "PCR"}
    fails("FORBIDDEN", 403, service.record_correction, app, sid, ok, SAEGIL)
    fails("SESSION_NOT_FOUND", 404, service.record_correction, app, sid, ok, DAON)
    fails("SESSION_NOT_FOUND", 404, service.record_correction, app, "ses_404", ok, HANBIT)
    for body, code, status in (
        ({**ok, "streamId": "st_daon_pp"}, "STREAM_NOT_FOUND", 404),
        ({**ok, "field": "price"}, "INVALID_FIELD", 422),
        ({**ok, "after": "재활용"}, "INVALID_VALUE", 422),
        ({**ok, "after": None}, "INVALID_VALUE", 422),
        ({**ok, "after": "PIR"}, "NO_CHANGE", 422),
        ({**ok, "field": "grade", "after": "S"}, "INVALID_VALUE", 422),
        ({**ok, "field": "moisturePct", "after": 120}, "INVALID_VALUE", 422),
        ({**ok, "field": "box", "after": "왼쪽 위"}, "INVALID_VALUE", 422),
    ):
        fails(code, status, service.record_correction, app, sid, body, HANBIT)
    assert feedback.corrections(app) == []


def test_outcome_only_from_the_threads_vendor_after_confirmation():
    app = make_app()
    waiting = to_approval(app)
    sid = waiting["session"]["id"]
    thread = thread_of(waiting, SAEGIL)
    body = {
        "threadId": thread["id"],
        "streamId": "st_sprue",
        "actualGrade": "A",
        "inspectedOn": "2026-11-16",
    }
    fails("THREAD_NOT_CONFIRMED", 409, service.record_outcome, app, sid, body, SAEGIL)
    view = approve_all(app, waiting)
    fails("FORBIDDEN", 403, service.record_outcome, app, sid, body, HANBIT)
    fails("THREAD_NOT_FOUND", 404, service.record_outcome, app, sid, body, MISO)
    fails("SESSION_NOT_FOUND", 404, service.record_outcome, app, sid, body, DAON)
    for patch, code in (
        ({"streamId": "st_defect"}, "STREAM_NOT_IN_THREAD"),  # 다른 업체에 배정된 흐름
        ({"actualGrade": "특"}, "INVALID_VALUE"),
        ({"inspectedOn": "11월 16일"}, "INVALID_VALUE"),
        ({"inspectedOn": "2027-01-01"}, "INVALID_VALUE"),
        ({"actualGrade": None}, "INVALID_VALUE"),  # 등급도 단가도 없다
        ({"actualKg": -1}, "INVALID_VALUE"),
        ({"actualUnitPriceKrwPerKg": "삼백"}, "INVALID_VALUE"),
    ):
        fails(code, 422, service.record_outcome, app, sid, {**body, **patch}, SAEGIL)
    assert feedback.outcomes(app) == []
    assert thread_of(view, SAEGIL, "confirmed")["id"] == thread["id"]


def test_outcome_is_shared_with_the_emitter_and_feeds_grade_labels(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    done = service.record_outcome(app, sid, outcome_body(view), SAEGIL)
    record = done["outcome"]
    assert record["evidenceKind"] == "human" and len(done["evidence"]) == 3
    for evidence_id in done["evidence"]:
        found = app.registry.get(evidence_id, HANBIT)  # 배출 기업과 나눈다
        assert found.meta["ownerOrgId"] == SAEGIL and found.access == (SAEGIL, HANBIT)
        fails("EVIDENCE_NOT_FOUND", 404, service.get_evidence, app, evidence_id, DAON)
        fails("EVIDENCE_NOT_FOUND", 404, service.get_evidence, app, evidence_id, MISO)
    rows = feedback.export_grade_labels(app)
    assert rows == [
        {
            "material": "PP",
            "contamination_pct": 1.0,
            "moisture_pct": 0.3,
            "grade": "A",
            "synthetic": False,
            "thresholds_version": grade_model.thresholds()["version"],
            "quality_basis": "measured",
            "source": record["evidence"]["grade"],
            "stream_id": "st_sprue",
            "vendor_org_id": SAEGIL,
            "inspected_on": "2026-11-16",
        }
    ]
    # 기준표 단가(320)와 실제 단가(300)의 차이는 calc 근거로 남고 검증기를 통과한다
    deviation = record["deviation"]
    assert record["tablePrice"]["value"] == 320 and deviation["value"] == -20
    assert deviation["evidence"][0].startswith("calc:price_deviation:")
    checked = validate_text("{{difference}}", [deviation], app.registry, org_id=HANBIT, today=TODAY)
    assert checked.ok, checked.to_dict()
    chain = service.get_evidence(app, deviation["evidence"][0], HANBIT)["chain"]
    assert {record["evidence"]["unitPrice"]} <= {c["id"] for c in chain}
    # 같은 검수를 다시 보내면 새 기록이 아니라 같은 근거의 새 판이다
    service.record_outcome(app, sid, outcome_body(view, actualGrade="B"), SAEGIL)
    assert [r["grade"] for r in feedback.export_grade_labels(app)] == ["B"]
    assert app.registry.get(record["evidence"]["grade"]).version == 2
    for viewer, count in ((HANBIT, 1), (SAEGIL, 1), (MISO, 0)):
        assert len(service.get_view(app, sid, viewer)["feedback"]["outcomes"]) == count


def test_outcome_with_document_is_doc_evidence_and_incomplete_rows_are_not_labels(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    # 포장 필름은 측정 수분이 없다(담당자 답 2.0%만 있다): 사진 추정 기준으로 행이 된다
    film = service.record_outcome(
        app,
        sid,
        outcome_body(view, stream="st_film", actualGrade="B", documentName="검수표-가상"),
        SAEGIL,
    )["outcome"]
    assert film["evidenceKind"] == "doc" and film["evidence"]["grade"].startswith("doc:org_saegil:")
    assert film["features"]["basis"] == "photo_estimate"
    # 단가만 기록하면 등급 라벨이 아니다
    service.record_outcome(
        app, sid, outcome_body(view, actualGrade=None, inspectedOn="2026-11-17"), SAEGIL
    )
    table = feedback.retrain_table(app)
    assert table["outcomes"]["count"] == 2
    assert table["gradeLabels"] == {
        "count": 1,
        "usable": 1,
        "incomplete": 0,
        "note": table["gradeLabels"]["note"],
    }


def test_gate_stays_closed_until_real_labels_accumulate(confirmed):
    app, view = confirmed
    gate = feedback.gate_status(app)
    assert gate["allowed"] is False and gate["realLabelCount"] == 0
    assert gate["evaluated"]["accuracy"] is None
    service.record_outcome(app, view["session"]["id"], outcome_body(view), SAEGIL)
    gate = feedback.gate_status(app)
    assert gate["allowed"] is False and gate["realLabelCount"] == 1
    assert gate["evaluated"] == {
        "model": f"등급 기준값 규칙 {grade_model.thresholds()['version']}",
        "n": 1,
        "accuracy": 1.0,
        "label": "실제 등급 기준",
    }
    assert "1건" in gate["reason"]


def test_calibration_lists_each_case_without_averaging(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    for day, price in (("2026-11-14", 300), ("2026-11-15", 330), ("2026-11-16", 320)):
        service.record_outcome(
            app, sid, outcome_body(view, inspectedOn=day, actualUnitPriceKrwPerKg=price), SAEGIL
        )
    service.record_outcome(
        app, sid, outcome_body(view, stream="st_film", actualUnitPriceKrwPerKg=140), SAEGIL
    )
    found = feedback.calibration_candidates(app)
    assert found["count"] == 4 and found["autoApply"] is False
    assert [(g["streamId"], g["count"]) for g in found["groups"]] == [
        ("st_film", 1),
        ("st_sprue", 3),
    ]
    sprue = found["groups"][1]
    assert [i["difference"]["value"] for i in sprue["items"]] == [-20, 10, 0]
    assert set(sprue) == {"streamId", "vendorOrgId", "criterionId", "count", "items"}  # 평균 없음
    # 기준표 값은 그대로다
    criteria = app.store.get_vendor_profile(SAEGIL)["criteria"]
    assert next(c for c in criteria if c["id"] == sprue["criterionId"])["unitPriceKrwPerKg"] == 320
    # 당사자만 건별 목록을 본다
    assert feedback.calibration_candidates(app, MISO)["groups"] == []
    assert feedback.calibration_candidates(app, HANBIT)["count"] == 4


def test_admin_status_has_the_retrain_table_and_no_invented_versions(confirmed):
    app, view = confirmed
    sid = view["session"]["id"]
    empty = service.admin_status(make_app(), HANBIT)["retrain"]
    assert empty["gradeLabels"]["count"] == 0 and empty["corrections"]["count"] == 0
    assert empty["modelVersions"] == [] and empty["gate"]["allowed"] is False
    service.record_outcome(app, sid, outcome_body(view), SAEGIL)
    service.record_correction(
        app,
        sid,
        {"streamId": "st_film", "field": "origin", "before": "PCR", "after": "PIR"},
        HANBIT,
    )
    table = service.admin_status(app, HANBIT)["retrain"]
    assert table["gradeLabels"]["count"] == 1 and table["corrections"] == {
        "count": 1,
        "byField": {"origin": 1},
        "detectorCandidates": 1,
    }
    assert table["calibration"]["count"] == 1 and table["modelVersions"] == []
    # 건수는 전체지만 건별 목록은 당사자만 본다
    other = service.admin_status(app, MISO)["retrain"]
    assert other["gradeLabels"]["count"] == 1 and other["calibration"]["groups"] == []


def test_model_runs_and_export_are_admin_only(confirmed):
    app, view = confirmed
    app.store.put_org({"id": ADMIN, "name": "운영자(가상)", "role": "admin"})
    service.record_outcome(app, view["session"]["id"], outcome_body(view), SAEGIL)
    run = {
        "model": grade_model.MODEL_NAME,
        "version": "2",
        "runId": "run-가상-0001",
        "trainedOn": "2026-11-17",
        "dataBasis": "합성 데이터 기준",
        "realLabelCount": 1,
        "syntheticCount": 2000,
        "metrics": {"accuracy": 0.97},
        "gate": {"allowed": False, "reason": "실제 등급 기록이 부족합니다."},
    }
    fails("FORBIDDEN", 403, service.register_model_run, app, run, HANBIT)
    fails("FORBIDDEN", 403, service.export_retraining, app, SAEGIL)
    fails("INVALID_VALUE", 422, service.register_model_run, app, {**run, "model": "gpt"}, ADMIN)
    fails("INVALID_VALUE", 422, service.register_model_run, app, {**run, "gate": {}}, ADMIN)
    saved = service.register_model_run(app, run, ADMIN)["run"]
    assert saved["metrics"] == {"accuracy": 0.97} and saved["deployed"] is False
    versions = service.admin_status(app, HANBIT)["retrain"]["modelVersions"]
    assert [(v["model"], v["version"], v["gate"]["allowed"]) for v in versions] == [
        (grade_model.MODEL_NAME, "2", False)
    ]
    exported = service.export_retraining(app, ADMIN)
    assert len(exported["gradeLabels"]) == 1 and exported["detectionCorrections"] == []
    assert exported["gate"]["allowed"] is False


def test_devserver_routes_for_feedback():
    app = make_app()
    view = approve_all(app, to_approval(app))
    server = devserver.make_server(app, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"

    def call(method, path, body, org, expect=200):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
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
        fixed = call(
            "POST",
            f"/sessions/{sid}/corrections",
            {"streamId": "st_sprue", "field": "form", "before": "가공 전", "after": "분쇄"},
            HANBIT,
        )
        assert fixed["correction"]["target"] == "detector"
        saved = call("POST", f"/sessions/{sid}/outcomes", outcome_body(view), SAEGIL)
        assert saved["outcome"]["actualGrade"] == "A"
        denied = call("POST", f"/sessions/{sid}/outcomes", outcome_body(view), HANBIT, 403)
        assert denied["error"]["code"] == "FORBIDDEN"
        call("GET", "/admin/retrain/export", None, HANBIT, 403)
        status = call("GET", "/admin/status", None, HANBIT)
        assert status["retrain"]["gradeLabels"]["count"] == 1
    finally:
        server.shutdown()
        server.server_close()
