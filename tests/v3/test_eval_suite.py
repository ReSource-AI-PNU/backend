"""평가 실행 CLI(scripts/v3/eval/run_all.py)와 리포트·latest.json 모양(평가 설계 15장, 17장)."""

import json

from app.v3.eval import report, suite
from scripts.v3.eval import run_all


def test_run_all_writes_reports_and_latest_without_touching_the_repo_report(tmp_path):
    before = run_all.EVAL_REPORT.read_text(encoding="utf-8")
    code = run_all.main(
        [
            "--split",
            "dev",
            "--layers",
            "L1,L3,L4,L7",
            "--resamples",
            "200",
            "--date",
            "2026-01-02",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert run_all.EVAL_REPORT.read_text(encoding="utf-8") == before
    data = json.loads((tmp_path / "2026-01-02-dev-rule.json").read_text(encoding="utf-8"))
    assert [layer["layer"] for layer in data["layers"]] == ["L1", "L3", "L4", "L7"]
    assert data["header"]["isModel"] is False and data["header"]["modelCalls"] == 0
    markdown = (tmp_path / "2026-01-02-dev-rule.md").read_text(encoding="utf-8")
    assert "규칙 정책(모델 미연결) 실행이다" in markdown and "결과가 아니다" in markdown
    raw = (tmp_path / "2026-01-02-dev-rule.raw.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 50 and "threads" in json.loads(raw[0])
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    run = latest["runs"]["dev"]
    assert run["aiVersion"].startswith("규칙 정책") and run["isModel"] is False
    assert run["nPerLayer"]["L4"] == 50 and run["layers"]["L1"]["status"] == "not_measured"
    assert run["headline"]["pass1"]["target"]["isTarget"] is True
    assert {s["path"].rsplit("/", 1)[-1] for s in run["sources"]} == {
        "2026-01-02-dev-rule.json",
        "2026-01-02-dev-rule.md",
        "2026-01-02-dev-rule.raw.jsonl",
    }


def test_latest_keeps_one_entry_per_split(tmp_path):
    path = tmp_path / "latest.json"
    for split in ("dev", "test"):
        result = {
            "header": {
                "date": "2026-01-02",
                "isModel": True,
                "policy": "foundry",
                "split": split,
                "setVersion": split,
                "manifestHash": None,
                "commit": None,
                "repeats": 4,
                "reviewState": "초안",
            },
            "layers": [],
        }
        latest = suite.update_latest(path, result, [])
    assert sorted(latest["runs"]) == ["dev", "test"] and latest["primary"] == "test"
    # 공개 신뢰 페이지에는 모델 이름 대신 AI 판(평가일)을 쓴다
    assert latest["runs"]["test"]["aiVersion"] == "AI 판(2026-01-02)"


def test_the_repo_report_shows_the_latest_layer_summary_and_how_to_read_it():
    text = report.render(report.collect())
    assert "## 9. 평가 설계 층별 결과" in text
    if report.load_latest():
        assert "규칙 정책 실행은 AI 결과가 아니다" in text and "목표는 결과가 아니다" in text
