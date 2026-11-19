"""시연(가상) 회사 사이의 검수·교정은 실제 학습·검증·배포 게이트에 세지 않는다.

2026-11-19 멘토링 점검: 빈 저장소의 bff 모드에서 시연 되돌리기만 해도 가상 검수 8건이 생기고, 그중 등급
행 4개가 synthetic=false로 내보내져 게이트에 "실제 등급 기록 4건, 실제 등급 기준 정확도 1.0"이 보였다.
출처(dataOrigin: real | demo)를 기록할 때 남기고, 시연 기록은 따로 세어 실제 기록과 섞지 않는다.
"""

import uuid
from dataclasses import replace

from app.v3 import feedback, service
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, approve_all, make_app, thread_of, to_approval

ADMIN = "org_admin"


def _bff_app():
    return service.App(InMemoryStore(), InMemoryRegistry(), auth_mode="bff", today="2026-11-17")


def _as_real(app, *org_ids):
    """시연 회사를 실제 회사처럼 둔다(실제 기록 경로를 시험하려는 것이다)."""
    for org_id in org_ids:
        org = app.store.get_org(org_id)
        app.store.put_org({**org, "synthetic": False, "demo": False})


def _record(app, view, **over):
    body = {
        "requestId": f"req-{uuid.uuid4().hex}",  # 새 반입 한 건
        "threadId": thread_of(view, SAEGIL, "confirmed")["id"],
        "streamId": "st_sprue",
        "actualGrade": "A",
        "actualUnitPriceKrwPerKg": 300,
        "actualKg": 580,
        "inspectedOn": "2026-11-16",
        **over,
    }
    return service.record_outcome(app, view["session"]["id"], body, SAEGIL)["outcome"]


def test_demo_reset_alone_gives_no_real_labels():
    app = _bff_app()
    service.reset_demo(app)
    recorded = feedback.outcomes(app)
    assert recorded, "시연 되돌리기는 지난 거래의 가상 검수를 만든다"
    assert {r["dataOrigin"] for r in recorded} == {"demo"}
    gate = feedback.gate_status(app)
    assert gate["realLabelCount"] == 0 and gate["allowed"] is False
    assert gate["demoLabelCount"] > 0
    # 실제 등급 기준 정확도를 내지 않는다(평가할 실제 행이 없다)
    assert gate["evaluated"]["n"] == 0 and gate["evaluated"]["accuracy"] is None
    assert feedback.export_grade_labels(app) == []
    demo_rows = feedback.export_grade_labels(app, origin="demo")
    assert demo_rows and all(r["synthetic"] is True for r in demo_rows)
    assert {r["data_origin"] for r in demo_rows} == {"demo"}
    exported = service.export_retraining(app, ADMIN)
    assert exported["gradeLabels"] == [] and exported["gate"]["realLabelCount"] == 0
    assert len(exported["demo"]["gradeLabels"]) == len(demo_rows)
    table = feedback.retrain_table(app)
    assert table["gradeLabels"]["count"] == 0 and table["outcomes"]["count"] == 0
    assert table["demo"]["outcomes"] == len(recorded)
    assert table["demo"]["gradeLabels"] == len(demo_rows)
    assert table["calibration"]["count"] == 0


def test_repeated_demo_history_never_meets_the_real_sample_bar():
    app = _bff_app()
    for _ in range(3):
        service.reset_demo(app, ADMIN)
    for _ in range(10):  # 시연 지난 거래를 거듭 쌓는다(한 번에 등급 행 여러 개)
        service._demo_history(app)
    gate = feedback.gate_status(app)
    assert gate["demoLabelCount"] >= gate["minRealLabels"]
    assert gate["realLabelCount"] == 0 and gate["allowed"] is False
    assert "아직 없습니다" in gate["reason"]


def test_outcome_between_real_companies_is_real():
    app = make_app()
    _as_real(app, HANBIT, SAEGIL)
    view = approve_all(app, to_approval(app))
    record = _record(app, view)
    assert record["dataOrigin"] == "real"
    rows = feedback.export_grade_labels(app)
    assert len(rows) == 1 and rows[0]["synthetic"] is False and rows[0]["data_origin"] == "real"
    assert feedback.gate_status(app)["realLabelCount"] == 1


def test_a_demo_party_makes_the_record_demo():
    # 실제 배출 기업이 시연 업체와 거래해도 업체 검수는 가상 회사가 적은 것이다
    app = make_app()
    _as_real(app, HANBIT)
    view = approve_all(app, to_approval(app))
    assert _record(app, view)["dataOrigin"] == "demo"
    assert feedback.gate_status(app)["realLabelCount"] == 0


def test_corrections_keep_their_origin():
    app = make_app()
    view = approve_all(app, to_approval(app))
    sid = view["session"]["id"]
    body = {"streamId": "st_film", "field": "origin", "before": "PCR", "after": "PIR"}
    record = service.record_correction(app, sid, body, HANBIT)["correction"]
    assert record["dataOrigin"] == "demo"
    assert feedback.export_detection_corrections(app) == []
    assert len(feedback.export_detection_corrections(app, origin="demo")) == 1
    table = feedback.retrain_table(app)
    assert table["corrections"]["count"] == 0 and table["demo"]["corrections"] == 1


def test_old_records_without_origin_are_judged_by_the_parties_not_counted_as_real():
    app = make_app()
    view = approve_all(app, to_approval(app))
    record = _record(app, view)
    # 출처 표시가 생기기 전에 저장된 기록을 흉내 낸다
    for evidence_id in record["evidence"].values():
        old = app.registry.get(evidence_id)
        record_meta = {k: v for k, v in old.meta["feedback"].items() if k != "dataOrigin"}
        app.registry.put(replace(old, meta={**old.meta, "feedback": record_meta}))
    assert "dataOrigin" not in app.registry.get(record["evidence"]["grade"]).meta["feedback"]
    assert feedback.gate_status(app)["realLabelCount"] == 0
    assert [r["dataOrigin"] for r in feedback.outcomes(app)] == ["demo"]


def test_retrain_job_does_not_take_demo_rows_as_real():
    import pytest

    from scripts.v3.ml import compare_and_gate as job

    app = make_app()
    view = approve_all(app, to_approval(app))
    _record(app, view)
    demo_rows = feedback.export_grade_labels(app, origin="demo")
    assert demo_rows and all(job.is_synthetic(r) for r in demo_rows)
    # synthetic 열을 잃어도 data_origin으로 막는다
    stripped = [{**r, "synthetic": False} for r in demo_rows]
    assert all(job.is_synthetic(r) for r in stripped)
    with pytest.raises(ValueError):
        job.merge_rows([], stripped)
