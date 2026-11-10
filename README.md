# Re:Source AI — Backend

FastAPI 기반 에이전트 API. 설계 문서는 [ReSource-AI](https://github.com/ReSource-AI-PNU/ReSource-AI) 저장소의 `docs/`를 참고한다.

## 구조

```
app/
├── main.py          # FastAPI 앱, CORS, 라우터 등록
├── api/router.py    # /api/v1 엔드포인트
├── agents/          # 대회 6단계 에이전트 (README 참고)
├── ml/              # ① 식별 모델 추론 코드만 (학습 코드는 별도 저장소)
└── core/config.py   # 환경 변수 설정
```

## 실행

```bash
cp .env.example .env        # 키는 각자 로컬에만 둔다 (커밋 금지)
uv sync --extra dev
uv run uvicorn app.main:app --reload
```

- API 문서: http://localhost:8000/docs
- 헬스 체크: http://localhost:8000/api/v1/health

## 검사

```bash
uv run ruff check .
uv run pytest -q
```

## ① 식별 모델 (backend#8)

백엔드는 `app.ml.detector.get_detector()` 로만 모델을 부른다. `CLASSIFIER_BACKEND` 로 경로를 고른다.

| 값 | 동작 |
|---|---|
| `auto` (기본) | `MATERIAL_MODEL_DIR` 에 가중치가 있으면 trained, 없으면 zeroshot |
| `trained` | RT-DETRv2 재질 모델 + 상태(이물) 모델 + 도메인 게이트. `pip install '.[ml]'` 필요 |
| `zeroshot` | Azure OpenAI 멀티모달 (backend#4). 학습 모델 없이 동작 |

- 가중치·`mlflow.db` 는 git 에 넣지 않는다. 컨테이너 시작 시 `scripts/fetch_models.py` 가 `MODEL_BLOB_URL`(Azure Blob SAS, tar.gz) 에서 `models/` 로 받아 온다.
- 학습 데이터(AI허브)는 재배포 금지라 이 저장소에 없다. 학습·평가는 별도 학습 저장소에서 하고 여기에는 추론 코드만 둔다.
- `POST /api/v1/classify` (multipart: files, stream_id, name, process, material_doc_text, material_hint) 가 `Stream.classify` + 출처 추정(originSource=ai, 확인 질문 포함) + 되묻기 `Question[]` 를 돌려준다. `GET /api/v1/models` 로 현재 모델을 확인한다.
- 로컬에서 학습 모델로 돌리려면:

```bash
uv sync --extra dev --extra ml
MATERIAL_MODEL_DIR=/path/v3_material CONDITION_MODEL_DIR=/path/v4_condition DOMAIN_CLF_DIR=/path/domain_clf uv run uvicorn app.main:app --reload
```

## 배포

`Dockerfile`로 컨테이너를 빌드해 Azure Container Apps에 올린다. GitHub Actions 워크플로와 Bicep은 이후 추가한다.
