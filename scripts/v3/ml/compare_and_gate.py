"""미실행(Azure ML 필요). 재학습한 후보 모델을 지금 모델과 MLflow에서 비교하고 배포 게이트를 판정한다.

Azure ML 작업 안에서 돌린다(MLflow 추적 주소는 Azure ML이 넣어 준다). 로컬에서는 py_compile과
순수 함수(merge_rows, decide_grade, decide_detector) 테스트만 했고, mlflow를 부르는 부분은 돌려 보지
못했다.

    # 1) 실제 등급 행(GET /api/v3/admin/retrain/export 의 gradeLabels)을 합성 데이터와 합친다
    python scripts/v3/ml/compare_and_gate.py merge --synthetic grade_synthetic.v1.csv \
        --real grade_labels.json --out grade_retrain.csv

    # 2) 후보 실행과 지금 등록 판을 실제 등급 행으로만 재고 게이트를 판정한다
    PYTHONPATH=. python scripts/v3/ml/compare_and_gate.py gate-grade --candidate-run <run_id> \
        --real grade_labels.json --training grade_retrain.csv --baseline-version <등록 판> \
        --out gate.json

    # 3) 탐지 모델: 후보 실행과 기준 실행의 같은 지표를 비교한다
    python scripts/v3/ml/compare_and_gate.py gate-detector --candidate-run <run_id> \
        --baseline-run <run_id> --metric <지표 이름> --corrections corrections.json \
        --min-corrections <건수> --out gate.json

gate.json은 POST /api/v3/admin/retrain/runs 본문과 같은 모양이다. 관리자가 올려야 관리자 화면의
모델 버전 표에 나온다. 게이트를 통과하지 못한 후보는 등록·배포하지 않는다.

- 등급 게이트의 기준(실제 등급 최소 건수, 최소 정확도)은 app.v3.intake.grade_model.deploy_gate
  하나만 쓴다. 합성 행으로 잰 점수는 게이트에 넣지 않는다.
- 탐지 게이트의 기준(지표 이름, 최소 교정 건수)은 인자로 받는다. 이 파일에 숫자를 박지 않는다.
- 후보가 지금 모델보다 나쁘면 통과시키지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COLUMNS = (
    "material",
    "contamination_pct",
    "moisture_pct",
    "grade",
    "synthetic",
    "thresholds_version",
)
GRADE_MODEL = "resource-ai-grade"
DETECTOR_MODEL = "resource-ai-detector"


def load_rows(path: Path) -> list[dict[str, Any]]:
    """JSON(행 목록 또는 {"gradeLabels" | "detectionCorrections": [...]})이나 CSV를 읽는다."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        return list(csv.DictReader(text.splitlines()))
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("gradeLabels") or data.get("detectionCorrections") or []
    if not isinstance(data, list):
        raise TypeError(f"{path}: 행 목록이 아니다")
    return [row for row in data if isinstance(row, dict)]


def is_synthetic(row: dict[str, Any]) -> bool:
    return str(row.get("synthetic")).strip().lower() == "true"


def merge_rows(synthetic: list[dict[str, Any]], real: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """합성 행과 실제 행을 한 표로. 실제 행은 synthetic=false로 표시해 섞여도 구분되게 한다."""
    merged = []
    for row in synthetic:
        merged.append({**{c: row.get(c) for c in COLUMNS}, "synthetic": "true"})
    for row in real:
        if is_synthetic(row):
            raise ValueError("실제 등급 행에 synthetic=true가 섞여 있다")
        merged.append({**{c: row.get(c) for c in COLUMNS}, "synthetic": "false"})
    return merged


def write_csv(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def decide_grade(
    candidate: dict[str, Any], baseline: dict[str, Any] | None, real_count: int, gate_fn
) -> dict[str, Any]:
    """등급 모델 게이트. candidate와 baseline은 실제 등급 행으로만 잰 evaluate() 결과다."""
    gate = dict(gate_fn(candidate, real_count))
    known = bool(baseline) and baseline.get("accuracy") is not None
    if gate["allowed"] and known and candidate["accuracy"] < baseline["accuracy"]:
        gate.update(allowed=False, reason="후보의 실제 등급 기준 정확도가 지금 모델보다 낮습니다.")
    return gate


def decide_detector(
    candidate: float | None,
    baseline: float | None,
    correction_count: int,
    min_corrections: int,
    metric: str,
) -> dict[str, Any]:
    """탐지 모델 게이트. 같은 평가 세트에서 잰 같은 지표를 비교한다."""
    base = {"metric": metric, "minCorrections": min_corrections}
    if correction_count < min_corrections:
        return {
            "allowed": False,
            "reason": (
                f"사람이 고친 기록이 {correction_count}건뿐입니다. {min_corrections}건 이상 "
                "쌓인 뒤에 다시 학습합니다."
            ),
            **base,
        }
    if candidate is None or baseline is None:
        return {
            "allowed": False,
            "reason": f"후보나 기준 실행에 {metric} 지표가 없어 비교하지 못했습니다.",
            **base,
        }
    if candidate < baseline:
        return {"allowed": False, "reason": f"후보의 {metric}가 지금 모델보다 낮습니다.", **base}
    return {"allowed": True, "reason": f"후보의 {metric}가 지금 모델 이상입니다.", **base}


def _started_on(run: Any) -> str:
    """MLflow 실행이 시작된 날짜(UTC)."""
    started = getattr(run.info, "start_time", None)
    if not started:
        return ""
    return datetime.fromtimestamp(started / 1000, UTC).date().isoformat()


def _predictor(model: Any):
    import pandas as pd

    def predict(material, contamination, moisture):
        frame = pd.DataFrame(
            [
                {
                    "material": str(material or "").strip().upper(),
                    "contamination_pct": float(contamination),
                    "moisture_pct": float(moisture),
                }
            ]
        )
        return str(next(iter(model.predict(frame))))

    return predict


def gate_grade(args: argparse.Namespace) -> dict[str, Any]:
    import mlflow

    from app.v3.intake.grade_model import deploy_gate, evaluate

    rows = [r for r in load_rows(args.real) if not is_synthetic(r)]
    for row in rows:
        row["contamination_pct"] = float(row["contamination_pct"])
        row["moisture_pct"] = float(row["moisture_pct"])
        row["synthetic"] = False
    candidate_model = mlflow.pyfunc.load_model(f"runs:/{args.candidate_run}/model")
    candidate = evaluate(rows, _predictor(candidate_model))
    baseline = None
    if args.baseline_version:
        current = mlflow.pyfunc.load_model(f"models:/{GRADE_MODEL}/{args.baseline_version}")
        baseline = evaluate(rows, _predictor(current))
    else:
        baseline = evaluate(rows)  # 등록 판이 없으면 기준값 규칙과 견준다
    gate = decide_grade(candidate, baseline, len(rows), deploy_gate)
    run = mlflow.get_run(args.candidate_run)
    with mlflow.start_run(run_id=args.candidate_run):
        if candidate["accuracy"] is not None:
            mlflow.log_metric("real_accuracy", candidate["accuracy"])
        if baseline["accuracy"] is not None:
            mlflow.log_metric("baseline_real_accuracy", baseline["accuracy"])
        mlflow.log_metric("real_label_count", len(rows))
        mlflow.set_tags(
            {"gate_allowed": str(gate["allowed"]).lower(), "gate_reason": gate["reason"]}
        )
    version = ""
    if gate["allowed"] and args.register:
        registered = mlflow.register_model(f"runs:/{args.candidate_run}/model", GRADE_MODEL)
        version = str(registered.version)
    return {
        "model": GRADE_MODEL,
        "version": version or f"후보-{args.candidate_run}",
        "runId": args.candidate_run,
        "trainedOn": _started_on(run),
        "dataBasis": candidate["label"],
        "realLabelCount": len(rows),
        # 학습 CSV를 주지 않으면 합성 행 수는 모른다(지어내지 않는다)
        "syntheticCount": sum(1 for r in load_rows(args.training) if is_synthetic(r))
        if args.training
        else None,
        "metrics": {"accuracy": candidate["accuracy"], "perGrade": candidate["perGrade"]},
        "baseline": {
            "version": args.baseline_version or "기준값 규칙",
            "accuracy": baseline["accuracy"],
        },
        "gate": gate,
        "deployed": False,
    }


def gate_detector(args: argparse.Namespace) -> dict[str, Any]:
    import mlflow

    corrections = load_rows(args.corrections)
    with_photo = [c for c in corrections if c.get("hasPhoto")]
    candidate_run = mlflow.get_run(args.candidate_run)
    baseline_run = mlflow.get_run(args.baseline_run)
    candidate = candidate_run.data.metrics.get(args.metric)
    baseline = baseline_run.data.metrics.get(args.metric)
    gate = decide_detector(candidate, baseline, len(with_photo), args.min_corrections, args.metric)
    with mlflow.start_run(run_id=args.candidate_run):
        mlflow.log_metric("correction_count", len(with_photo))
        mlflow.set_tags(
            {"gate_allowed": str(gate["allowed"]).lower(), "gate_reason": gate["reason"]}
        )
    version = ""
    if gate["allowed"] and args.register:
        registered = mlflow.register_model(f"runs:/{args.candidate_run}/model", DETECTOR_MODEL)
        version = str(registered.version)
    return {
        "model": DETECTOR_MODEL,
        "version": version or f"후보-{args.candidate_run}",
        "runId": args.candidate_run,
        "trainedOn": _started_on(candidate_run),
        "dataBasis": "사람이 고친 기록 포함",
        "realLabelCount": len(with_photo),
        "syntheticCount": None,
        "metrics": {args.metric: candidate},
        "baseline": {"runId": args.baseline_run, args.metric: baseline},
        "gate": gate,
        "deployed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="재학습 후보 비교와 배포 게이트")
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("merge", help="합성 행과 실제 등급 행을 학습용 CSV로 합친다")
    merge.add_argument("--synthetic", type=Path, required=True)
    merge.add_argument("--real", type=Path, required=True)
    merge.add_argument("--out", type=Path, required=True)
    grade = commands.add_parser("gate-grade", help="등급 모델 후보를 실제 등급 행으로 판정한다")
    grade.add_argument("--candidate-run", required=True)
    grade.add_argument("--real", type=Path, required=True)
    grade.add_argument("--training", type=Path, default=None, help="학습에 쓴 CSV(합성 행 수 집계)")
    grade.add_argument("--baseline-version", default="")
    grade.add_argument("--register", action="store_true")
    grade.add_argument("--out", type=Path, required=True)
    detector = commands.add_parser("gate-detector", help="탐지 모델 후보를 기준 실행과 비교한다")
    detector.add_argument("--candidate-run", required=True)
    detector.add_argument("--baseline-run", required=True)
    detector.add_argument("--metric", required=True)
    detector.add_argument("--corrections", type=Path, required=True)
    detector.add_argument("--min-corrections", type=int, required=True)
    detector.add_argument("--register", action="store_true")
    detector.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "merge":
        rows = merge_rows(load_rows(args.synthetic), load_rows(args.real))
        count = write_csv(args.out, rows)
        real = sum(1 for row in rows if not is_synthetic(row))
        sys.stdout.write(f"{args.out}: {count}행 (실제 {real}행, 합성 {count - real}행)\n")
        return 0
    result = gate_grade(args) if args.command == "gate-grade" else gate_detector(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.write(
        f"게이트 {'통과' if result['gate']['allowed'] else '미통과'}: {result['gate']['reason']}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
