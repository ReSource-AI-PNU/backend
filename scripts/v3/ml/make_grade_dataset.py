"""등급 모델 합성 학습 데이터를 CSV로 내보낸다.

    python -m scripts.v3.ml.make_grade_dataset --n 5000 --seed 20261117 --out data/grade_synthetic.v1.csv

모든 행에 synthetic=true가 붙는다. 기준값은 app/v3/intake/grade_thresholds.v2.json(공개 규격을 참고한 가정값)이다.
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from app.v3.intake.grade_model import make_synthetic_dataset

COLUMNS = (
    "material",
    "contamination_pct",
    "moisture_pct",
    "grade",
    "synthetic",
    "thresholds_version",
)


def write_csv(path: Path, n: int, seed: int) -> int:
    rows = make_synthetic_dataset(n, seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "synthetic": "true"})
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="등급 모델 합성 학습 데이터 생성")
    parser.add_argument("--n", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--out", type=Path, default=Path("data/grade_synthetic.v1.csv"))
    args = parser.parse_args(argv)
    count = write_csv(args.out, args.n, args.seed)
    sys.stdout.write(f"{args.out}: {count}행 (synthetic=true, seed={args.seed})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
