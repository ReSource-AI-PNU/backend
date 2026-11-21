"""사례 실행기. 고정 사례 50건을 모두 돌린다(CI에서 돈다). 수치는 모두 가상이다."""

import copy
from pathlib import Path

import pytest

from app.v3 import service
from app.v3.eval import foundry_eval, report
from app.v3.eval.loader import load_cases
from app.v3.eval.runner import run_case, run_cases
from tests.v3.runtime_fixtures import make_app

CASES = load_cases()
REPORT = Path(__file__).resolve().parents[2] / "docs" / "v3" / "eval-report.md"


@pytest.fixture(scope="module")
def outcome():
    return run_cases(None, CASES)


def test_all_cases_run_without_errors(outcome):
    summary = outcome["summary"]
    assert summary["caseCount"] == 50 and summary["errorCount"] == 0
    assert outcome["model"] == {"provider": "rule", "name": "규칙 정책(모델 미연결)"}
    assert len(outcome["results"]) == 50


def test_no_forbidden_violation_in_any_case(outcome):
    violated = {
        r["id"]: r["forbiddenViolations"] for r in outcome["results"] if r["forbiddenViolations"]
    }
    assert violated == {}
    assert outcome["summary"]["forbiddenClean"] is True


def test_every_outgoing_message_and_document_passes_the_validator(outcome):
    summary = outcome["summary"]
    assert summary["validatorChecked"] > 90
    assert summary["validatorPassed"] == summary["validatorChecked"]
    assert summary["validatorPassRate"] == 1.0
    failed = {
        r["id"]: r["validator"]["failed"] for r in outcome["results"] if r["validator"]["failed"]
    }
    assert failed == {}


def test_verdicts_match_except_for_cases_left_open(outcome):
    results = {r["id"]: r for r in outcome["results"]}
    mismatched = {
        cid: r["mismatches"]
        for cid, r in results.items()
        if not r["verdictMatch"] and not r["open"]
    }
    assert mismatched == {}
    summary = outcome["summary"]
    assert summary["closedMatched"] == summary["closedCount"]
    # open으로 남긴 사례는 실제로 기대와 다르다(같아졌으면 open 표시를 지워야 한다)
    for cid in summary["openCases"]:
        assert not results[cid]["verdictMatch"], cid
    assert summary["verdictMatched"] == 50 - len(summary["openCases"])
    assert summary["verdictMatchRate"] == summary["verdictMatched"] / 50


def test_running_twice_gives_the_same_result(outcome):
    again = run_cases(None, CASES[:12])
    assert again["results"] == outcome["results"][:12]


def test_runner_reports_a_wrong_expectation_and_a_forbidden_violation():
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "C04"))
    case["expect"]["result"]["status"] = "conditional"
    case["expect"]["assignments"]["st_a"]["unitPrice"] = 999
    case["forbidden"].append({"kind": "assigned", "stream": "st_a", "why": "시험용 금지"})
    case["forbidden"].append({"kind": "text", "pattern": "담당자께", "why": "시험용 금지"})
    result = run_case(case)
    assert result["verdictMatch"] is False
    assert {m["path"] for m in result["mismatches"]} == {
        "result.status",
        "assignments.st_a.unitPrice",
    }
    assert [v["kind"] for v in result["forbiddenViolations"]] == ["assigned", "text"]


def test_a_broken_case_is_reported_as_an_error_not_a_crash():
    case = copy.deepcopy(CASES[3])
    case["world"]["vendors"][0]["criteria"][0].pop("minMonthlyKg")
    result = run_case(case)
    assert result["error"] and result["verdictMatch"] is False


def test_runner_accepts_an_app_or_a_registry(outcome):
    app = make_app()
    picked = [c for c in CASES if c["id"] in ("C04", "C21")]
    from_app = run_cases(app, picked)
    from_registry = run_cases(app.registry, picked)
    expected = [r for r in outcome["results"] if r["id"] in ("C04", "C21")]
    assert from_app["results"] == expected and from_registry["results"] == expected
    assert app.store.list_sessions() == []  # 주어진 앱의 저장소는 건드리지 않는다


def test_service_run_eval_runs_the_case_set(outcome):
    app = make_app()
    evaluated = service.run_eval(app, "org_admin")
    cases = evaluated["cases"]
    assert cases["summary"] == outcome["summary"]
    assert len(cases["results"]) == 50 and "observed" not in cases["results"][0]
    # 시연 시드 그대로라 미소환경 경로는 필수 운반비 미정이다. 계약 검토 담당이 그 승인을 막아 진단은 승인
    # 대기로 남고, 나머지 업체만 확정된다
    assert evaluated["validatorPassRate"] == 1.0
    assert evaluated["sessionStatus"] == "awaiting_approval"
    status = {
        t["vendorOrgId"]: t["status"] for t in evaluated["threads"] if t["status"] != "not_opened"
    }
    assert status["org_miso"] == "agreed"
    assert status["org_saegil"] == status["org_namhae"] == "confirmed"
    assert app.store.list_sessions() == []


def test_groundedness_rows_come_from_the_execution_record():
    picked = [c for c in CASES if c["id"] == "C11"]
    rows = foundry_eval.rows_from_cases(None, picked)
    assert rows and all(r["query"] and r["response"] and r["context"] for r in rows)
    assert {r["kind"] for r in rows} == {"message", "document"}
    offer = next(r for r in rows if r["type"] == "offer")
    assert "260원/kg" in offer["response"] and "{{" not in offer["response"]
    # 메시지는 매입 기준표 근거를 그때 판으로 고정한 사본(doc:snapshot:…, meta.originalId)으로 인용한다(2026-11-21).
    # 원문 발췌가 그대로 context에 실린다
    assert "doc:snapshot:" in offer["context"] and "PE B등급 단가 kg당 260원" in offer["context"]
    assert "문의드립니다" in offer["query"]  # 받은 문의를 값으로 풀어 쓴 글


def test_groundedness_jsonl_is_written(tmp_path):
    rows = foundry_eval.rows_from_cases(None, [c for c in CASES if c["id"] == "C04"])
    path = tmp_path / "out" / "rows.jsonl"
    assert foundry_eval.write_jsonl(rows, path) == len(rows)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rows) and '"query"' in lines[0]


def test_the_saved_report_is_what_the_code_produces_now():
    produced = report.render(report.collect())
    assert REPORT.read_text(encoding="utf-8") == produced, (
        "docs/v3/eval-report.md가 지금 실행 결과와 다릅니다. "
        "python3 -m app.v3.eval.report --out docs/v3/eval-report.md 로 다시 만드세요."
    )
    assert "미측정" in produced and "50건" in produced


def test_report_includes_measured_results_only_when_given(tmp_path):
    measured = tmp_path / "ollama.json"
    measured.write_text(
        '{"model": {"provider": "ollama", "name": "시험용"}, "measured": true, '
        '"situationCount": 20, "roles": {"company": 14, "vendor": 6}, "firstPassed": 10, '
        '"firstPassRate": 0.5, "finalPassed": 15, "finalPassRate": 0.75, "rewritten": 10, '
        '"firstFailureCodes": {"SCHEMA": 10}, "byCheck": [], "results": []}',
        encoding="utf-8",
    )
    data = report.collect(compliance_files=[measured])
    text = report.render(data)
    assert "| ollama · 시험용 | 20 | 50.0% (10/20) | 75.0% (15/20) |" in text
    assert "| foundry | - | 미측정 | 미측정 |" in text
    assert "형식 준수 시험: ollama" not in text and "형식 준수 시험: foundry" in text
