"""평가 실행 CLI(scripts/v3/eval/run_all.py)와 리포트·latest.json 모양(평가 설계 15장, 17장)."""

import json

from app.v3.eval import report, stats, suite
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
    assert run["aiVersion"].startswith("모델을 연결하지 않은 기본 규칙") and run["isModel"] is False
    # 공개 이름은 쉬운 말이고 기술 이름과 키는 따로 있다
    pass1 = run["headline"]["pass1"]
    assert pass1["label"] == "찾기를 끝까지 맞게 마친 비율" and pass1["key"] == "pass^1"
    assert pass1["technicalLabel"].startswith("pass^1") and pass1["ciMethod"] == "wilson"
    assert run["layers"]["L4"]["title"] == "찾기를 끝까지 해내기"
    assert run["layers"]["L4"]["technicalTitle"] == "에이전트 과업 성공(끝 상태)"
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


JARGON = (
    "ASR",
    "카나리아",
    "탐침",
    "spotlighting",
    "Shields",
    "pass^",
    "L4",
    "nDCG",
    "Recall",
    "규칙 정책",
)


def test_the_committed_latest_json_speaks_plain_korean():
    """신뢰 페이지가 읽는 latest.json: 이름·풀이·재지 못한 것에 기술 용어가 없다(기술 이름은 따로)."""
    latest = json.loads((run_all.OUT_DIR / "latest.json").read_text(encoding="utf-8"))
    for run in latest["runs"].values():
        texts = [run["aiVersion"]]
        for layer in run["layers"].values():
            texts += [layer["title"], layer["explain"], *layer["notMeasured"]]
            for m in layer["metrics"]:
                texts += [m["label"], m["explain"] or ""]
                if m["ci"] is not None:
                    assert m["ciMethod"] in ("wilson", "bootstrap")
                    # 비율 지표는 Wilson 구간이라 n이 있으면 폭이 0이 아니다
                    if m["ciMethod"] == "wilson":
                        assert m["ci"][1] - m["ci"][0] > 0, m["key"]
        texts += [h["label"] for h in run["headline"].values() if h]
        for text in texts:
            assert not any(word in text for word in JARGON), text
    # 지난 결과는 지금 결과가 아니라고 적혀 있다
    for old in latest.get("history") or []:
        assert old["current"] is False and old["supersededBy"] and old["reason"]


def test_wilson_interval_for_proportions_and_bootstrap_for_scores():
    low, high = stats.wilson_ci(1.0, 10)
    assert high == 1.0 and abs(low - 0.7225) < 1e-3  # 10/10이어도 폭이 0이 아니다
    assert stats.wilson_ci(0.0, 29)[0] == 0.0 and stats.wilson_ci(0.0, 29)[1] > 0.1
    assert stats.wilson_ci(0.5, 0) is None
    rate = stats.rate_metric("firstTry", "x", [True] * 12)
    assert rate["ciMethod"] == "wilson" and rate["ci"][0] < 1.0 == rate["ci"][1]
    # 묶음이 있으면 n은 묶음 수다
    grouped = stats.rate_metric(
        "firstTry", "x", [True] * 6, clusters=["a", "a", "b", "b", "c", "c"]
    )
    assert grouped["n"] == 3 and grouped["ci"] == list(stats.wilson_ci(1.0, 3))
    score = stats.rate_metric("ndcg@10", "nDCG@10", [0.5, 0.7, 0.9], resamples=200)
    assert score["ciMethod"] == "bootstrap"


def test_reports_are_rebuilt_from_saved_raw_outputs_without_models(tmp_path, monkeypatch):
    first = run_all.main(
        ["--split", "dev", "--layers", "L4", "--resamples", "200", "--date", "2026-01-03",
         "--out-dir", str(tmp_path)]
    )  # fmt: skip
    assert first == 0
    json_path = tmp_path / "2026-01-03-dev-rule.json"
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    # 예전 리포트처럼 비율 지표를 부트스트랩의 폭 0 구간으로 바꿔 둔다
    pass1 = next(m for m in saved["layers"][0]["metrics"] if m["key"] == "pass^1")
    pass1["ci"] = [1.0, 1.0]
    pass1.pop("ciMethod", None)
    json_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    raw = tmp_path / "2026-01-03-dev-rule.raw.jsonl"
    before = raw.read_text(encoding="utf-8")
    calls = []
    monkeypatch.setattr(suite, "run_suite", lambda **kw: calls.append(kw))  # 실행하지 않는다
    assert run_all.main(["--from-raw", str(raw), "--out-dir", str(tmp_path)]) == 0
    assert calls == [] and raw.read_text(encoding="utf-8") == before
    rebuilt = json.loads(json_path.read_text(encoding="utf-8"))
    pass1 = next(m for m in rebuilt["layers"][0]["metrics"] if m["key"] == "pass^1")
    assert pass1["ciMethod"] == "wilson" and pass1["ci"] == list(stats.wilson_ci(1.0, pass1["n"]))
    assert rebuilt["header"]["rebuiltFrom"]["rows"] == 50
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert latest["runs"]["dev"]["rebuiltFrom"]["rows"] == 50
    assert latest["runs"]["dev"]["headline"]["pass1"]["ci"][0] < 1.0
    # 원 출력과 리포트가 맞지 않으면 멈춘다
    raw.write_text(before.splitlines()[0] + "\n", encoding="utf-8")
    assert run_all.main(["--from-raw", str(raw), "--out-dir", str(tmp_path)]) == 2


def test_superseded_results_are_kept_apart_from_the_current_one(tmp_path):
    path = tmp_path / "latest.json"

    def result(value, commit):
        return {
            "header": {
                "date": "2026-01-02", "isModel": False, "policy": "rule", "split": "test",
                "setVersion": "test-v1", "manifestHash": None, "commit": commit, "repeats": 1,
                "reviewState": "초안",
            },
            "layers": [
                {
                    "layer": "L6c", "title": "안전: 조직 간 노출(카나리아·탐침)", "status": "measured",
                    "n": 31, "unit": "실행", "notMeasured": [],
                    "metrics": [stats.count_metric("exposures", "조직 간 노출 건수(카나리아·값)", value, n=31)],
                }
            ],
        }  # fmt: skip

    suite.update_latest(path, result(186, "aaa"), [])
    latest = suite.update_latest(path, result(0, "bbb"), [])
    assert latest["runs"]["test"]["headline"]["exposures"]["value"] == 0
    assert latest["runs"]["test"]["headline"]["exposures"]["label"] == "다른 회사에 보인 정보 건수"
    old = latest["history"][0]
    assert old["current"] is False and old["commit"] == "aaa"
    assert old["headline"]["exposures"]["value"] == 186 and old["supersededBy"]["commit"] == "bbb"
    # 같은 값으로 다시 재면 지난 결과를 더 쌓지 않는다
    assert len(suite.update_latest(path, result(0, "ccc"), [])["history"]) == 1
