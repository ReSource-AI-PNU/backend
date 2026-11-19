"""검수 기록의 탄소 기준 고정(outcome_carbon).

2026-11-19 재현: 580kg 검수를 기록한 뒤 현행 처리만 소각에서 매립으로 바꾸자 같은 검수의 탄소가 −1.968493에서
+0.003130 tCO2e로 바뀌었다. 기록한 뒤에 현행 처리, 업체 처리 방식, 배출계수 기본값을 바꿔도 지난 검수의 상태와
수치는 그대로이고, 새로 기록하는 검수에만 새 조건이 들어가야 한다.
"""

import copy
from dataclasses import replace

import pytest

from app.v3 import feedback, ids, outcome_carbon, service
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden
from app.v3.evidence.validator import validate_text
from app.v3.judge import basis
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, approve_all, make_app, start

INCINERATE = "ref:환경성적표지:혼합폐플라스틱-소각@2024-09"
# 580 kg × (재활용 0.0186 − 소각 3.412554039 kgCO2e/kg) ÷ 1000
FIRST = -1.968493


def _confirmed():
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    view = approve_all(app, view)
    sid = view["session"]["id"]
    session = app.store.get_session(sid)
    thread = next(t for t in app.store.list_threads(sid) if t["vendorOrgId"] == SAEGIL)
    stream_id = session["work"]["assigned"][thread["id"]][0]
    # 배출 기업이 자료 화면에서 현행 처리를 소각으로 적었다(사용자 재현과 같은 출발점)
    service.update_stream(
        app,
        HANBIT,
        stream_id,
        {"currentTreatment": {"method": "소각", "unitCostKrwPerKg": 250}},
        HANBIT,
    )
    return app, sid, thread, stream_id


def _record(app, sid, thread, stream_id, inspected_on, kg=580, **extra):
    return service.record_outcome(
        app,
        sid,
        {
            "requestId": "req-" + ids.digest([inspected_on, kg, extra]),
            "threadId": thread["id"],
            "streamId": stream_id,
            "inspectedOn": inspected_on,
            "actualUnitPriceKrwPerKg": 300,
            "actualKg": kg,
            **extra,
        },
        SAEGIL,
    )["outcome"]


def _report(app):
    return service.get_report(app, HANBIT, HANBIT)


def _row(app, inspected_on):
    return next(r for r in _report(app)["records"] if r["inspectedOn"] == inspected_on)


def _ok(app, claim):
    return validate_text(
        "{{c}}", [{**claim, "id": "c"}], app.registry, org_id=HANBIT, today=app.today
    ).ok


def _frozen(row):
    """지난 검수에서 바뀌면 안 되는 것: 탄소 값, 기준 상태와 내용, 처리 방식, 비교한 현행 처리."""
    return {
        "carbon": row["carbon"] and row["carbon"]["value"],
        "basis": row["carbonBasis"],
        "process": row["process"],
        "currentMethod": row["currentMethod"],
        "currentMethodAssumed": row["currentMethodAssumed"],
    }


def _change_method(app, stream_id):
    service.update_stream(
        app,
        HANBIT,
        stream_id,
        {"currentTreatment": {"method": "매립", "unitCostKrwPerKg": 250}},
        HANBIT,
    )


def _change_vendor_process(app, _stream_id):
    profile = app.store.get_vendor_profile(SAEGIL)
    app.store.put_vendor_profile({**profile, "process": "열분해"})


def _change_factor_value(app, _stream_id):
    # 같은 근거 ID의 값이 바뀌면 레지스트리는 판을 올려 덮어쓴다(이전 값은 남지 않는다)
    old = app.registry.get(INCINERATE)
    assert app.registry.put(replace(old, value=2.0)).version == old.version + 1


def _change_factor_default(app, _stream_id):
    # 기본값 후보 맨 앞에 다른 근거를 끼워 넣는다(계수 기본값 교체)
    app.registry.put(
        Evidence(
            id="assumption:시험-소각계수@v2",
            kind="assumption",
            value=2.5,
            unit="kgCO2e/kg",
            label="시험용 소각 배출계수",
            excerpt="시험용 소각 배출계수 2.5",
            source="시험",
            as_of="2026-11-18",
        )
    )
    basis.prefer("emission.incinerate.포장재", "assumption:시험-소각계수@v2")


# 바꾼 뒤 새로 기록한 580kg 검수의 탄소: 새 조건이 들어가야 한다
CHANGES = {
    # 매립 0.013203539 → 580 × (0.0186 − 0.013203539) ÷ 1000
    "current_method": (_change_method, 0.00313, "매립", "재활용"),
    # 열분해 1.239 → 580 × (1.239 − 3.412554039) ÷ 1000
    "vendor_process": (_change_vendor_process, -1.260661, "소각", "열분해(화학적 재활용)"),
    "factor_value": (_change_factor_value, -1.149212, "소각", "재활용"),
    "factor_default": (_change_factor_default, -1.439212, "소각", "재활용"),
}


@pytest.fixture
def restore_basis():
    saved = copy.deepcopy(basis._PREFERRED)
    yield
    basis._PREFERRED.clear()
    basis._PREFERRED.update(saved)


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_past_record_keeps_its_carbon_and_new_records_take_the_new_condition(change, restore_basis):
    app, sid, thread, stream_id = _confirmed()
    _record(app, sid, thread, stream_id, "2026-11-10")
    before = _row(app, "2026-11-10")
    assert before["carbon"]["value"] == FIRST and _ok(app, before["carbon"])
    assert before["carbonBasis"]["status"] == "recorded"
    assert before["currentMethod"] == "소각" and before["currentMethodAssumed"] is False
    assert before["process"] == "재활용"
    factors = before["carbonBasis"]["factors"]
    assert factors["baseline"]["value"] == 3.412554039 and factors["baseline"]["version"] == 1
    assert factors["applied"]["value"] == 0.0186 and factors["applied"]["unit"] == "kgCO2e/kg"
    totals = _report(app)["totals"]["carbon"]["value"]

    apply, expected, method, process = CHANGES[change]
    apply(app, stream_id)
    after = _row(app, "2026-11-10")
    assert _frozen(after) == _frozen(before)
    assert _ok(app, after["carbon"])
    assert _report(app)["totals"]["carbon"]["value"] == totals

    _record(app, sid, thread, stream_id, "2026-11-12")
    fresh = _row(app, "2026-11-12")
    assert fresh["carbonBasis"]["status"] == "recorded"
    assert fresh["carbon"]["value"] == pytest.approx(expected, abs=1e-6)
    assert fresh["currentMethod"] == method and fresh["process"] == process
    assert _frozen(_row(app, "2026-11-10")) == _frozen(before)
    report = _report(app)
    assert report["totals"]["carbon"]["value"] == pytest.approx(FIRST + expected, abs=2e-6)
    assert report["totals"]["carbonStatus"]["recorded"] == 2


def test_all_three_changes_together_leave_the_past_record_alone(restore_basis):
    app, sid, thread, stream_id = _confirmed()
    _record(app, sid, thread, stream_id, "2026-11-10")
    before = _frozen(_row(app, "2026-11-10"))
    for apply, *_ in CHANGES.values():
        apply(app, stream_id)
    assert _frozen(_row(app, "2026-11-10")) == before


def test_scope_says_only_treatment_emissions_are_in_the_number():
    app, sid, thread, stream_id = _confirmed()
    _record(app, sid, thread, stream_id, "2026-11-10")
    report = _report(app)
    scope = report["carbonScope"]
    assert scope["key"] == "process_only"
    assert "수거 운송 배출" in scope["excludes"] and "신재 대체 효과" in scope["excludes"]
    assert report["records"][0]["carbonBasis"]["scope"] == scope


def test_vendor_states_the_process_it_actually_applied():
    app, sid, thread, stream_id = _confirmed()
    record = _record(app, sid, thread, stream_id, "2026-11-10", actualProcess="매립")
    assert record["actualProcess"] == "매립"
    assert record["appliedProcess"] == {"value": "매립", "source": "vendor_input", "assumed": False}
    row = _row(app, "2026-11-10")
    assert row["process"] == "매립"
    assert row["carbonBasis"]["applied"]["sourceLabel"] == "업체가 검수 기록에 적음"
    # 소각 대신 매립: 580 × (0.013203539 − 3.412554039) ÷ 1000
    assert row["carbon"]["value"] == pytest.approx(-1.971623, abs=1e-6)
    with pytest.raises(service.ServiceError) as err:
        _record(app, sid, thread, stream_id, "2026-11-11", actualProcess="태움")
    assert err.value.code == "INVALID_VALUE"


def test_the_baseline_stays_with_the_emitter():
    """현행 처리는 업체에 열지 않는 값이다. 기준 근거는 배출 기업만 보고, 업체와 나누는 기록에는 싣지 않는다."""
    app, sid, thread, stream_id = _confirmed()
    record = _record(app, sid, thread, stream_id, "2026-11-10")
    with pytest.raises(EvidenceForbidden):
        app.registry.get(outcome_carbon.snapshot_id(record), SAEGIL)
    assert app.registry.get(outcome_carbon.snapshot_id(record), HANBIT).value is not None
    shared = service.get_view(app, sid, SAEGIL)["feedback"]["outcomes"]
    text = repr(shared)
    assert "소각" not in text and "baseline" not in text and "carbon" not in text


# 이 변경 전 기록에는 없던 항목(검수 ID 이전의 검수일 키 기록 모양)
NEW_KEYS = (
    "inspectionId",
    "revision",
    "supersedes",
    "requestId",
    "reason",
    "legacy",
    "weighTicket",
    "documentName",
    "createdOn",
    "createdAt",
    "recordedAt",
    "actualProcess",
    "appliedProcess",
)


def _make_legacy(app, record):
    """이 변경 전에 기록된 것처럼: 기준 근거를 지우고 기록을 예전 모양(처리 방식·검수 ID 없음)으로 되돌린다."""
    app.registry.delete([outcome_carbon.snapshot_id(record)])
    for item in app.registry.list(org_id=None):
        found = item.meta.get("feedback")
        if isinstance(found, dict) and found.get("type") == "outcome":
            meta = {
                **item.meta,
                "feedback": {k: v for k, v in found.items() if k not in NEW_KEYS},
            }
            app.registry.put(replace(item, meta=meta))
    legacy = feedback.outcomes(app)[0]
    assert legacy["legacy"] is True and legacy.get("appliedProcess") is None


def test_legacy_record_is_rebuilt_from_the_agreement_and_then_frozen(restore_basis):
    app, sid, thread, stream_id = _confirmed()
    _make_legacy(app, _record(app, sid, thread, stream_id, "2026-11-10"))
    row = _row(app, "2026-11-10")
    shown = row["carbonBasis"]
    assert shown["status"] == "reconstructed"
    assert shown["statusLabel"] == "합의 당시 기준으로 재구성"
    # 합의 당시에는 현행 처리를 적지 않아 혼합 소각으로 가정했다(지금 자료의 소각이 아니다)
    assert row["currentMethod"] == "혼합 소각" and row["currentMethodAssumed"] is True
    assert shown["baseline"]["source"] == "deal" and shown["deal"]["monthlyKg"] == 300
    assert row["carbon"]["value"] == FIRST and _ok(app, row["carbon"])
    # 재구성한 기준도 처음 읽을 때 고정된다: 그 뒤 계수가 바뀌어도 같다
    _change_factor_value(app, stream_id)
    _change_method(app, stream_id)
    assert _frozen(_row(app, "2026-11-10")) == _frozen(row)


def test_legacy_record_whose_factors_changed_is_not_given_todays_number(restore_basis):
    app, sid, thread, stream_id = _confirmed()
    _make_legacy(app, _record(app, sid, thread, stream_id, "2026-11-10"))
    _change_factor_value(app, stream_id)  # 기준을 읽기 전에 계수가 바뀌었다
    report = _report(app)
    row = report["records"][0]
    assert row["carbonBasis"]["status"] == "unverified"
    assert row["carbon"] is None and "당시 기준을 확인할 수 없어" in row["carbonBasis"]["reason"]
    assert row["carbonNote"] == row["carbonBasis"]["reason"]
    assert report["totals"]["carbon"] is None
    assert report["totals"]["carbonStatus"]["unverified"] == 1
    # 무게·단가·금액(실제 기록)은 그대로 보인다
    assert row["kg"]["value"] == 580 and row["amount"]["value"] == 174000


def test_legacy_record_without_its_agreement_is_unverified():
    app, sid, thread, stream_id = _confirmed()
    _make_legacy(app, _record(app, sid, thread, stream_id, "2026-11-10"))
    session = app.store.get_session(sid)
    session["work"]["result"]["assignments"] = []
    app.store.put_session(session)
    row = _row(app, "2026-11-10")
    assert row["carbonBasis"]["status"] == "unverified" and row["carbon"] is None
    assert row["currentMethod"] is None


def test_backfill_previews_then_freezes_legacy_records_once(capsys, restore_basis):
    from scripts.v3 import backfill_outcome_carbon as backfill

    app, sid, thread, stream_id = _confirmed()
    record = _record(app, sid, thread, stream_id, "2026-11-10")
    _make_legacy(app, record)
    legacy = feedback.outcomes(app)[0]
    assert backfill.main([], app) == 0
    assert '"would_freeze:reconstructed": 1' in capsys.readouterr().out
    assert outcome_carbon.latest(app.registry, legacy) is None  # 미리 보기는 쓰지 않는다
    assert backfill.main(["--apply"], app) == 0
    assert '"frozen:reconstructed": 1' in capsys.readouterr().out
    assert outcome_carbon.latest(app.registry, legacy)["status"] == "reconstructed"
    _change_factor_value(app, stream_id)  # 고정한 뒤에는 계수가 바뀌어도 재구성 결과가 그대로다
    assert backfill.main(["--apply"], app) == 0
    assert '"already:reconstructed": 1' in capsys.readouterr().out
    assert _row(app, "2026-11-10")["carbon"]["value"] == FIRST


def _correct(app, sid, record, reason, **values):
    body = {
        "threadId": record["threadId"],
        "streamId": record["streamId"],
        "inspectedOn": record["inspectedOn"],
        "actualUnitPriceKrwPerKg": record["actualUnitPriceKrwPerKg"],
        "actualKg": record["actualKg"],
        **values,
        "baseRevision": record["revision"],
        "reason": reason,
        "requestId": "fix-" + ids.digest([record["revision"], reason, values]),
    }
    return service.correct_outcome(app, sid, record["id"], body, SAEGIL)["outcome"]


def test_correcting_the_weight_keeps_the_basis_and_leaves_history(restore_basis):
    """정정은 검수값만 바꾼다. 비교 기준선과 계수는 처음 기록한 것이고, 그 사이 자료를 고쳤어도 따라가지 않는다."""
    app, sid, thread, stream_id = _confirmed()
    record = _record(app, sid, thread, stream_id, "2026-11-10")
    before = _row(app, "2026-11-10")
    _change_method(app, stream_id)
    _change_factor_value(app, stream_id)
    fixed = _correct(app, sid, record, "계량표 다시 확인", actualKg=600)
    assert fixed["revision"] == 2 and fixed["history"][0]["actualKg"] == 580
    row = _row(app, "2026-11-10")
    assert row["revision"] == 2 and row["correctionReason"] == "계량표 다시 확인"
    assert row["carbonBasis"] == before["carbonBasis"]  # 판 1에 고정한 기준 그대로
    # 600 × (0.0186 − 3.412554039) ÷ 1000: 처음 기록한 계수(소각 3.412554039)로
    assert row["carbon"]["value"] == pytest.approx(-2.036372, abs=1e-6) and _ok(app, row["carbon"])
    assert row["currentMethod"] == "소각"
    assert _report(app)["totals"]["count"] == 1


def test_correcting_the_process_fixes_a_new_basis_for_that_revision(restore_basis):
    app, sid, thread, stream_id = _confirmed()
    record = _record(app, sid, thread, stream_id, "2026-11-10")
    _change_method(app, stream_id)  # 정정 전에 현행 처리를 바꿔도 기준선은 처음 기록의 소각
    fixed = _correct(app, sid, record, "실제로는 매립 처리", actualProcess="매립")
    assert fixed["appliedProcess"]["source"] == "vendor_input"
    row = _row(app, "2026-11-10")
    shown = row["carbonBasis"]
    assert shown["revision"] == 2 and shown["capturedBy"] == "correction"
    assert row["currentMethod"] == "소각" and row["process"] == "매립"
    assert shown["factors"]["baseline"]["value"] == 3.412554039
    assert any("정정 판 2에서 처리 방식을 바꿔" in note for note in shown["notes"])
    # 580 × (매립 0.013203539 − 소각 3.412554039) ÷ 1000
    assert row["carbon"]["value"] == pytest.approx(-1.971623, abs=1e-6)
    # 앞 판(판 1)의 기준 근거는 그대로 남는다
    first = outcome_carbon.latest(app.registry, {**fixed, "revision": 1})
    assert first["applied"]["process"] == "재활용" and first["revision"] == 1


def test_correction_without_a_process_keeps_the_previous_one_not_todays_profile():
    app, sid, thread, stream_id = _confirmed()
    record = _record(app, sid, thread, stream_id, "2026-11-10")
    _change_vendor_process(app, stream_id)  # 업체 정보를 열분해로 바꾼 뒤
    fixed = _correct(app, sid, record, "등급 기록", actualGrade="B")
    assert fixed["appliedProcess"] == record["appliedProcess"]
    assert _row(app, "2026-11-10")["process"] == "재활용"


def test_a_basis_saved_in_the_earlier_format_is_carried_over_as_recorded():
    """한때 쓴 형식(calc:outcome_carbon_basis:…)으로 기록할 때 남긴 기준은 재구성하지 않고 그대로 옮긴다."""
    app, sid, thread, stream_id = _confirmed()
    _make_legacy(app, _record(app, sid, thread, stream_id, "2026-11-10"))
    legacy = feedback.outcomes(app)[0]
    factor = {"version": 1, "unit": "kgCO2e/kg", "asOf": "2024-09-06"}
    app.registry.put(
        Evidence(
            id=outcome_carbon.earlier_basis_id(legacy),
            kind="calc",
            label="포장 필름 검수 2026-11-10 탄소 계산 기준",
            excerpt="검수 기록 당시 기준",
            source="검수 기록 당시의 탄소 계산 기준",
            as_of="2026-11-12",
            access=(HANBIT,),
            meta={
                "carbonBasis": {
                    "capturedOn": "2026-11-12",
                    "currentMethod": "매립",
                    "currentMethodAssumed": False,
                    "vendorProcess": "재활용",
                    "wasteItem": "포장재",
                    "unitPrice": 300.0,
                    "perKg": 0.000005396461,
                    "reason": None,
                    "factors": [
                        {
                            "id": "ref:환경성적표지:혼합폐플라스틱-매립@2024-09",
                            "value": 0.013203539,
                            **factor,
                        },
                        {
                            "id": "ref:환경성적표지:혼합폐플라스틱-재활용@2024-09",
                            "value": 0.0186,
                            **factor,
                        },
                    ],
                }
            },
        )
    )
    row = _row(app, "2026-11-10")
    shown = row["carbonBasis"]
    assert shown["status"] == "recorded" and shown["capturedOn"] == "2026-11-12"
    assert row["currentMethod"] == "매립" and row["process"] == "재활용"
    assert row["carbon"]["value"] == pytest.approx(0.00313, abs=1e-6)
    assert any("이전 형식" in note for note in shown["notes"])
    assert shown["evidenceId"] == outcome_carbon.snapshot_id(legacy)
