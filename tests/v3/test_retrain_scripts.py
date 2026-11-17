"""scripts/v3/ml/compare_and_gate.py의 순수 함수. mlflow를 부르는 부분은 로컬에서 돌리지 않는다."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from app.v3.intake.grade_model import deploy_gate, evaluate, make_synthetic_dataset

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "v3" / "ml" / "compare_and_gate.py"
ML_DIR = SCRIPT.parent


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("compare_and_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_rows(n):
    rows = make_synthetic_dataset(n, seed=7)
    return [{**row, "synthetic": False} for row in rows]  # 가상 행을 실제 행 자리에 쓴다(테스트용)


def test_merge_keeps_synthetic_and_real_apart(gate, tmp_path):
    synthetic = tmp_path / "synthetic.csv"
    with synthetic.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=gate.COLUMNS)
        writer.writeheader()
        for row in make_synthetic_dataset(5, seed=1):
            writer.writerow({**row, "synthetic": "true"})
    real = tmp_path / "labels.json"
    exported = [{**row, "stream_id": "st_x", "source": "human:x"} for row in real_rows(3)]
    real.write_text(json.dumps({"gradeLabels": exported}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out" / "merged.csv"
    assert (
        gate.main(["merge", "--synthetic", str(synthetic), "--real", str(real), "--out", str(out)])
        == 0
    )
    rows = gate.load_rows(out)
    assert len(rows) == 8 and set(rows[0]) == set(gate.COLUMNS)
    assert [gate.is_synthetic(r) for r in rows] == [True] * 5 + [False] * 3
    with pytest.raises(ValueError, match="synthetic=true"):
        gate.merge_rows([], [{"synthetic": "true"}])


def test_grade_gate_uses_only_real_labels_and_the_shared_rule(gate):
    few = evaluate(real_rows(5))
    blocked = gate.decide_grade(few, None, 5, deploy_gate)
    assert blocked["allowed"] is False and "5건" in blocked["reason"]
    enough = evaluate(real_rows(40))
    assert gate.decide_grade(enough, None, 40, deploy_gate)["allowed"] is True
    synthetic = evaluate(make_synthetic_dataset(40, seed=7))
    assert gate.decide_grade(synthetic, None, 40, deploy_gate)["allowed"] is False
    # 지금 모델보다 나쁘면 통과시키지 않는다
    worse = {**enough, "accuracy": 0.95}
    result = gate.decide_grade(worse, {"accuracy": 1.0}, 40, deploy_gate)
    assert result["allowed"] is False and "낮습니다" in result["reason"]


def test_detector_gate_needs_corrections_and_no_regression(gate):
    decide = gate.decide_detector
    assert decide(0.6, 0.5, 3, 10, "지표")["allowed"] is False
    assert decide(None, 0.5, 20, 10, "지표")["allowed"] is False
    assert decide(0.4, 0.5, 20, 10, "지표")["allowed"] is False
    passed = decide(0.6, 0.5, 20, 10, "지표")
    assert passed == {
        "allowed": True,
        "reason": "후보의 지표가 지금 모델 이상입니다.",
        "metric": "지표",
        "minCorrections": 10,
    }


def test_job_files_say_they_were_not_run():
    for name in ("retrain_grade_job.yml", "retrain_detector_job.yml", "compare_and_gate.py"):
        head = (ML_DIR / name).read_text(encoding="utf-8").splitlines()[0]
        assert "미실행(Azure ML 필요)" in head, name
