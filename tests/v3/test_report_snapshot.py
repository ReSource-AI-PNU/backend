"""지난 실적은 검수를 기록할 때의 계산 기준으로 남는다(지금 설정을 고쳐도 바뀌지 않는다).

2026-11-19 멘토링 점검: 580kg 검수의 탄소 변화가 -1.968493tCO2e였는데, 뒤에 스프루의 현행 처리 방식을 혼합
소각에서 재활용으로 고치자 같은 지난 검수의 탄소 변화가 null이 됐다(리포트가 지금의 흐름·업체 설정을 읽었다).
"""

from dataclasses import replace

from app.v3 import feedback, service
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, approve_all, make_app, thread_of, to_approval


def _setup():
    app = make_app()
    view = approve_all(app, to_approval(app))
    sid = view["session"]["id"]
    body = {
        "requestId": "req-snapshot-0001",
        "threadId": thread_of(view, SAEGIL, "confirmed")["id"],
        "streamId": "st_sprue",
        "inspectedOn": "2026-11-10",
        "actualGrade": "A",
        "actualUnitPriceKrwPerKg": 300,
        "actualKg": 580,
    }
    service.record_outcome(app, sid, body, SAEGIL)
    return app, sid, body


def _row(app):
    report = service.get_report(app, HANBIT, HANBIT)
    assert len(report["records"]) == 1
    return report["records"][0], report["totals"]


def test_changing_the_current_method_later_does_not_change_past_carbon():
    app, _, _ = _setup()
    before, totals = _row(app)
    assert before["carbon"] is not None and before["carbon"]["value"] < 0
    assert before["currentMethod"] == "혼합 소각" and before["amount"]["value"] == 174000
    service.update_stream(
        app,
        HANBIT,
        "st_sprue",
        {"currentTreatment": {"method": "재활용", "unitCostKrwPerKg": 0}},
        HANBIT,
    )
    after, after_totals = _row(app)
    assert after["carbon"] is not None
    assert after["carbon"]["value"] == before["carbon"]["value"]
    assert after["currentMethod"] == "혼합 소각"
    assert after_totals["carbon"]["value"] == totals["carbon"]["value"]


def test_changing_the_vendor_process_later_does_not_change_past_carbon():
    app, _, _ = _setup()
    before, _ = _row(app)
    profile = app.store.get_vendor_profile(SAEGIL)
    app.store.put_vendor_profile({**profile, "process": "소각"})
    after, _ = _row(app)
    assert after["carbon"]["value"] == before["carbon"]["value"]
    assert after["process"] == before["process"]


def test_the_basis_keeps_factor_ids_and_versions_for_the_emitter_only():
    app, _, _ = _setup()
    record = feedback.outcomes(app)[0]
    basis = feedback.carbon_basis(app, record)
    assert basis["currentMethod"] == "혼합 소각" and basis["currentMethodAssumed"] is False
    assert basis["capturedOn"] == app.today and basis["version"] == 1
    assert len(basis["factors"]) == 2 and all(f["version"] >= 1 for f in basis["factors"])
    row, _ = _row(app)
    assert row["carbonBasis"]["evidenceId"] == basis["evidenceId"]
    assert basis["evidenceId"] in row["carbon"]["evidence"]
    # 현행 처리 방식이 담긴 기준은 업체에 열지 않고, 업체와 나누는 검수 기록에도 싣지 않는다
    evidence = app.registry.get(basis["evidenceId"])
    assert evidence.access == (HANBIT,)
    assert "carbonBasis" not in record and "currentMethod" not in str(record)


def test_resending_or_correcting_keeps_the_inspection_time_basis():
    """기준은 검수 ID마다 처음 등록할 때 고정한다. 재전송은 기준을 새로 만들지 않고, 정정은 무게만 바꾼다."""
    app, sid, body = _setup()
    before, _ = _row(app)
    service.update_stream(
        app,
        HANBIT,
        "st_sprue",
        {"currentTreatment": {"method": "매립", "unitCostKrwPerKg": 200}},
        HANBIT,
    )
    again = service.record_outcome(app, sid, body, SAEGIL)  # 같은 등록 요청의 재전송
    assert again["replayed"] is True
    same, _ = _row(app)
    assert same["carbonBasis"] == before["carbonBasis"] and same["currentMethod"] == "혼합 소각"
    inspection = again["outcome"]["inspectionId"]
    service.correct_outcome(
        app,
        sid,
        inspection,
        {
            **body,
            "requestId": "req-snapshot-fix1",
            "actualKg": 290,
            "baseRevision": 1,
            "reason": "계량 오기",
        },
        SAEGIL,
    )
    after, _ = _row(app)
    assert after["revision"] == 2 and after["carbonBasis"] == before["carbonBasis"]
    assert after["currentMethod"] == "혼합 소각"  # 지금 설정(매립)이 아니라 검수 때 기준
    assert after["carbon"]["value"] == round(before["carbon"]["value"] / 2, 6)  # 580 → 290kg


def test_old_records_without_a_basis_show_no_carbon_rather_than_a_guess():
    app, _, _ = _setup()
    record = feedback.outcomes(app)[0]
    found = app.registry.get(feedback.carbon_basis_id(record))
    # 기준을 남기기 전에 기록된 검수를 흉내 낸다(기준 근거를 숨긴다)
    app.registry.put(
        replace(found, meta={k: v for k, v in found.meta.items() if k != "carbonBasis"})
    )
    row, totals = _row(app)
    assert row["carbon"] is None and row["carbonBasis"] is None
    assert "남아 있지 않은" in row["carbonNote"]
    assert row["amount"]["value"] == 174000  # 금액은 업체가 적은 값 그대로 계산한다
    assert totals["carbon"] is None
