"""미실행(Azure ML 필요). 비교용 scikit-learn 등급 모델을 학습해 MLflow에 기록한다.

Azure ML 작업(rsai-cpu) 안에서 돌린다. MLflow 추적 주소는 Azure ML이 넣어 준다.

    python train_grade_sklearn.py --data grade_synthetic.v1.csv --register resource-ai-grade-sklearn

합성 데이터로 낸 점수에는 data_basis=합성 데이터 기준 태그를 붙인다. 이 점수는 기준값 규칙을
얼마나 따라 하는지만 보여 주며, 실제 등급 정확도가 아니다. 배포 게이트는
app.v3.intake.grade_model.deploy_gate가 실제 등급 건수로 판단한다.
"""

from __future__ import annotations

import argparse


def main() -> None:
    import mlflow
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score, f1_score
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--register", default="")
    parser.add_argument("--seed", type=int, default=20261117)
    args = parser.parse_args()

    frame = pd.read_csv(args.data)
    features = frame[["material", "contamination_pct", "moisture_pct"]]
    labels = frame["grade"]
    synthetic = bool(frame["synthetic"].astype(str).str.lower().eq("true").any())
    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.2, random_state=args.seed, stratify=labels
    )
    model = Pipeline(
        [
            (
                "encode",
                ColumnTransformer(
                    [("material", OneHotEncoder(handle_unknown="ignore"), ["material"])],
                    remainder="passthrough",
                ),
            ),
            ("forest", RandomForestClassifier(n_estimators=200, random_state=args.seed)),
        ]
    )
    with mlflow.start_run(run_name="grade-sklearn-baseline"):
        mlflow.set_tags(
            {
                "data_basis": "합성 데이터 기준" if synthetic else "실제 등급 기준",
                "synthetic": str(synthetic).lower(),
                "thresholds_version": str(
                    frame.get("thresholds_version", pd.Series(["?"])).iloc[0]
                ),
            }
        )
        mlflow.log_params(
            {"n_rows": len(frame), "seed": args.seed, "model": "RandomForestClassifier"}
        )
        model.fit(x_train, y_train)
        predicted = model.predict(x_test)
        mlflow.log_metric("accuracy", accuracy_score(y_test, predicted))
        mlflow.log_metric("f1_macro", f1_score(y_test, predicted, average="macro"))
        mlflow.sklearn.log_model(
            model,
            artifact_path="model",
            registered_model_name=args.register or None,
            input_example=x_test.head(2),
        )


if __name__ == "__main__":
    main()
