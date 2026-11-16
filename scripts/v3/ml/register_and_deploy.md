# 등급 모델 학습, 등록, 배포 순서

미실행(Azure ML 필요). 이 폴더의 파일은 `make_grade_dataset.py`만 로컬에서 실행해 봤다. 나머지 파이썬 파일은 `py_compile`만 통과했고, YAML은 로컬에 파서가 없어 문법 검사도 하지 못했다(눈으로만 확인).
처음 돌릴 때 `az ml` 스키마 오류가 나면 이 문서와 YAML을 함께 고친다.

대상: 리소스 그룹 `team_AIBIC2026_03_RG_EUS2`, 작업 영역 `team-workspace`, 컴퓨팅 `rsai-cpu`.

```bash
export RG=team_AIBIC2026_03_RG_EUS2 WS=team-workspace

# 1) 합성 학습 데이터 (모든 행 synthetic=true, 저장소에는 넣지 않는다)
python -m scripts.v3.ml.make_grade_dataset --n 5000 --seed 20261117 \
  --out scripts/v3/ml/grade_training_mltable/grade_synthetic.v1.csv

# 2) AutoML 분류
az ml job create -f scripts/v3/ml/automl_grade_job.yml -g $RG -w $WS --stream

# 3) 비교용 scikit-learn 모델 (MLflow에 같이 기록)
az ml job create -g $RG -w $WS --stream \
  --name rsai-grade-sklearn-$(date +%m%d%H%M) \
  --set experiment_name=rsai-grade compute=azureml:rsai-cpu \
  code=scripts/v3/ml \
  command='python train_grade_sklearn.py --data grade_training_mltable/grade_synthetic.v1.csv --register resource-ai-grade-sklearn' \
  environment=azureml://registries/azureml/environments/sklearn-1.5/labels/latest

# 4) 가장 좋은 AutoML 모델을 resource-ai-grade로 등록 (<job>은 2)의 작업 이름)
az ml model create -g $RG -w $WS --name resource-ai-grade --type mlflow_model \
  --path azureml://jobs/<job>/outputs/artifacts/paths/outputs/mlflow-model \
  --tags data_basis=synthetic thresholds_version=v1

# 5) 배포 게이트 확인: 실제 등급이 쌓이기 전에는 allowed=false가 나온다
python -c "from app.v3.intake.grade_model import *; print(deploy_gate(evaluate(make_synthetic_dataset(1000, 1)), 0))"

# 6) 엔드포인트와 배포 (deployment.yml의 모델 버전을 4)에서 나온 번호로 바꾼다)
az ml online-endpoint create -f scripts/v3/ml/endpoint.yml -g $RG -w $WS
az ml online-deployment create -f scripts/v3/ml/deployment.yml --all-traffic -g $RG -w $WS

# 7) 호출 확인
az ml online-endpoint invoke -n rsai-grade -g $RG -w $WS --request-file <(echo \
  '{"input_data": {"columns": ["material", "contamination_pct", "moisture_pct"], "data": [["PP", 1.0, 0.3]]}}')
```

## 배포 게이트와 시연

게이트는 실제 등급 기록이 30건 이상이고, 그 기록만으로 잰 정확도가 90% 이상일 때만 통과한다.
합성 데이터만 있는 지금은 통과하지 못한다. 시연에서 엔드포인트를 실제로 호출하려면 6)을
"게이트 미통과, 시연용"이라고 밝히고 올려야 하며, 이 결정은 사용자에게 받는다. 화면에는
"합성 데이터 기준"을 그대로 표시한다.

## 백엔드 연결

`GradeModelClient(endpoint_url, api_key)`에 엔드포인트의 채점 주소와 키를 준다. 키는 Key Vault 비밀로만 둔다.
주소가 비어 있으면 기준값 규칙으로 답하고 결과에 `"modelBasis": "threshold_rule"`이 붙는다.
