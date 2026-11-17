"""미실행(Azure ML 필요). rsai-grade 온라인 엔드포인트의 채점 스크립트.

요청:  {"input_data": {"columns": ["material", "contamination_pct", "moisture_pct"], "data": [["PP", 1.0, 0.3]]}}
응답:  ["A"]   (등급 문자열 목록. app.v3.intake.grade_model.GradeModelClient가 이 모양을 읽는다)
"""

from __future__ import annotations

import json
import os

MODEL = None
COLUMNS = ["material", "contamination_pct", "moisture_pct"]


def init() -> None:
    global MODEL  # Azure ML 채점 스크립트의 정해진 꼴
    import mlflow

    root = os.environ["AZUREML_MODEL_DIR"]
    candidates = [root, *[os.path.join(root, name) for name in sorted(os.listdir(root))]]
    path = next(p for p in candidates if os.path.exists(os.path.join(p, "MLmodel")))
    MODEL = mlflow.pyfunc.load_model(path)


def run(raw_data: str) -> list[str]:
    import pandas as pd

    payload = json.loads(raw_data)["input_data"]
    frame = pd.DataFrame(payload["data"], columns=payload.get("columns") or COLUMNS)[COLUMNS]
    frame["material"] = frame["material"].astype(str).str.strip().str.upper()
    return [str(grade) for grade in MODEL.predict(frame)]
