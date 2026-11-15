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

### 경제 효과 미리보기 API

`POST /api/v1/feasibility/preview`는 공통 `Stream` 목록과 선택적인 `overrides`를
받아 저장 없이 가상 경제 효과를 계산한다. 단가와 규칙은 서버의
`app.demo.synthetic.CONFIG`를 사용하며 요청으로 바꿀 수 없다. 탄소와 미계산
항목은 `null`, 묶음은 조건부로 반환한다. 수치는 synthetic/demo 가정이며 실제
거래 가능성이나 절감액을 확정하지 않는다.

직접 확인하려면 위 실행 명령으로 서버를 시작한 후:

1. http://localhost:8000/docs 에서 `POST /api/v1/feasibility/preview`를 연다.
2. **Try it out**을 누르고 제공된 가상 공장 네 흐름 예시를 확인한다.
3. **Execute**를 누르면 응답의 `totalNow`, `totalAfter`, `decisions`를 확인할 수 있다.
4. 경로 변경을 시험하려면 `overrides`에 예시 흐름의 `id`를 키로 넣는다. 예를 들어
   첫 흐름의 ID가 `st_d7ca23cd1c42`이면 `{"st_d7ca23cd1c42": "sell"}`을 전달한다.
   해당 결과의 `route`와 `aiRoute`를 비교한다.

흐름 ID 중복, 빈 목록 또는 입력에 없는 ID에 대한 override는 422를 반환한다.
0kg, 미확인 발생량 및 지원하지 않는 계산 조합은 정상 응답의 계산 보류 상태로
표시한다. 이 API는 세션을 만들거나 저장하지 않으며, 기존 계약 예시 조회 API와
별개다.

## 검사

```bash
uv run ruff check .
uv run pytest -q
```

### 가상 스크랩 손익 계산

`app.agents.cost_calculator`의 `CostInputs`와 `calculate_costs`는 다른 트랙과
독립적인 Python 계산 엔진이다. 발생량·매각량은 kg, 단가는 원/kg, 총 운송비와
손익은 원이다. 작업비는 투입량 기준이며 현재 운송비와 작업 후 잔재물 처리비는
포함하지 않는다. 회수율은 0~1이고 발생량은 양수, 비용과 매각 단가는 0 이상이다.
손익분기 작업비가 음수이면 그대로 반환한다.

계산은 Python `float`로 수행하며 중간 및 최종 결과를 반올림하지 않는다.
부동소수점 오차가 생길 수 있으므로 비교 시 허용 오차가 필요하다. 예시와 테스트는
가상값이며 실제 재활용 가능성, 업체 수용 여부 또는 절감액을 확정하지 않는다.
표준 라이브러리만으로 해당 엔진을 검사하려면 다음 명령을 사용한다.

```bash
python -m unittest discover -s tests -p 'test_cost_calculator.py' -v
```

### 가상 공장 경로·경제 효과 엔진

`app.agents.demo_routes.evaluate_factory(flows, config, overrides)`는 가상 규칙을
계산한다. `app.demo.synthetic`의 `FLOWS`와 `CONFIG`는 frontend #17·#18의 시연
fixture를 옮긴 **synthetic/demo** 데이터다. 수치와 경로는 실제 업체의 수용 조건,
법적 적합성, 현장 품질 또는 거래 가능성을 의미하지 않는다. 묶음 결과와 이를 포함한
공장 합계는 조건부다. 필수 정보나 해당 시연 단가가 없으면 추가 확인 상태를 반환하고
공장 합계는 계산하지 않는다. 사용자 경로 변경 시 원래 시연 경로를 `ai_route`에 둔다.

kg당 값은 현재 소각 −250, PP 재투입 순효과 +900, PP 매각 +300,
열분해 −80, 포장재 매각 +150원이며 묶음 운송비 50원/kg을 매각액에서 차감한다.
재투입 900원/kg은 분쇄·관리비를 이미 포함한 신재 구매 회피 순효과이며 현금
매출이 아니다. 월 인건비는 시간당 15,000원에 재투입·이물 제거 매각 2시간,
묶음 1시간을 곱해 별도 차감한다. 나머지 경로에 별도 작업비와 묶음 외 운송비는
프런트 시연에 맞춰 명시적으로 0으로 가정한다. 현행 운송비와 잔재물 처리비도
포함하지 않는다. 매각량 회수율이나 신재 대체율을 추정하지 않고 발생량 전체에
시연 kg당 효과를 적용한다. 현금 매각액, 재투입 구매 회피 순효과, 처리비,
운송비, 인건비를 따로 반환한다.

월 현재 효과 = 발생량 × 현재 kg당 효과; 월 변경 효과 = 발생량 × 변경 kg당
효과 − 별도 인건비다. `after_per_kg`는 묶음 운송비가 이미 차감된 값이고,
인건비는 포함하지 않는다. 계산에는 `float`를 사용하고 반올림하지 않는다.
경제 결과 계약은 유한한 소수를 보존하고 미계산 금액과 탄소는 null로 표현한다.
`app.agents.economics_adapter.evaluate_streams`는 공통 Stream을 변환하고, 0kg이나
미확인 발생량을 해당 흐름의 계산 보류로 처리한다. 세션 API 연결은 후속 작업이다.
프런트 표시의 반올림 및 표시 합계 차이 규칙은 별도로 정해야 한다.

시연 경로의 우선순위는 금속 매각 → 혼합 열분해 → PCR 물량별 묶음/매각 →
이물 2% 미만 단일 PIR 플라스틱 재투입 → 이물 12% 미만 매각 → 소각이다.
혼입 표시가 있는 경우 5% 초과일 때 혼합으로 본다. PCR 포장재는 LDPE·LLDPE
전체 이름만 필름 시연 규칙을 적용하며, 그 외 재질은 추가 확인한다. 이는 모든
PCR 포장재에 동일 규칙을 적용하는 프런트 mock과의 차이로, backend #10의
재질 구분 요구와 함께 공통 계약 담당자 확인이 필요하다.

혼입·혼합 탐지 중 제거되지 않은 항목 하나라도 혼합 임계값을 초과하면 혼합으로
처리하며 탐지 순서는 영향을 주지 않는다. 시연 계산을 지원하는 품목·재질·경로
조합은 `app.demo.synthetic.CONFIG.supported_routes`에 명시한다. 목록에 없는
조합은 계산값을 대체하지 않고 추가 확인 상태로 반환한다. 이는 실제 적합성 판단이
아니라 시연 계산 지원 범위다. 월 발생량은 0보다 커야 한다.

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

### Foundry 수동 연결 확인

`backend/.env`에 `.env.example`의 `OPENAI_ENDPOINT`, `CHAT_DEPLOYMENT`,
`FOUNDRY_KEY`를 추가하고 키를 직접 입력한다. 기존 `.env`를 덮어쓰지 않는다.
운영체제 환경변수에 같은 이름이 있으면 `.env`보다 우선한다.

```bash
uv sync --extra dev --extra foundry-check
uv run --extra foundry-check python scripts/check_foundry.py
```

이 명령은 실제 Foundry API에 한 번 요청하며 비용이 발생할 수 있다. 성공하면 모델
응답만 출력한다. 자동 재시도는 없고 타임아웃 설정은 20초다. 일반 테스트는 실제
서비스에 요청하지 않는다.

### PP 질문 선택 수동 확인

Foundry 연결 설정을 마친 뒤 아래 명령으로 가상 PP 입력의 모델 선택 결과를 확인한다.
실제 Azure 요청이 발생하며, `source`가 `azure`면 모델 선택, `rule_fallback`이면
고정 사유 코드와 규칙 기반 선택이다. 이 실행은 질문 표시나 묶음 횟수를 기록하지 않는다.

```bash
uv run --extra foundry-check python -m scripts.check_pp_selector
```

모델에는 입력 상태와 허용 후보가 전달되므로 수동 확인은 가상 입력만 사용한다.
질문 선택의 무결성은 코드가 검증하지만 모델이 작성한 문구의 사실성과 적절성까지
자동 보장하지는 않는다.

### C 질문 상태 전이 API (저장 없음)

`POST /api/v1/c/questions/evaluate`, `/select`, `/display`, `/answer`는 호출자가
제공한 C 상태를 검증하고 각각 평가, 선택, 표시 기록, 답변 전이를 수행한다.
표시·답변 성공 응답은 새 `state`와 다음 `evaluation`을 함께 반환한다. 선택은
표시 횟수를 늘리지 않는다. 상태 충돌은 409, 잘못된 입력·선택은 422다.
선택 호출은 서버의 `OPENAI_ENDPOINT`, `CHAT_DEPLOYMENT`, `FOUNDRY_KEY`를 사용한다.

서버는 이 API의 상태를 저장하지 않는다. 전달된 이력의 진실성, 여러 요청에 걸친
두 묶음 한도, 답변 재전송 멱등성 및 동시 저장은 보장하지 않는다. 같은 상태와
같은 내용의 표시 재처리 등 순수 함수가 제공하는 범위만 유효하다. `complete`는
질문 종료이며 사실 검증이 아니다. 아래 값은 실제 업체 정보가 아닌 가상 예시다.

Swagger(`/docs`)에서 각 요청의 **Try it out**을 사용한다.

1. `/evaluate` 또는 `/select`에 `{"state":{"material":{"status":"value","value":"PP"}}}`를 입력한다. `/select`는 서버 설정이 있으면 실제 모델을 호출한다.
2. `/select` 결과의 `selected`, `snapshotId`와 1의 `state`를 `/display`에 다음 형식으로 전달한다: `{"state": {...}, "batchId": "demo-1", "selected": [...], "snapshotId": "..."}`.
3. `/display`가 반환한 `state`를 사용해 `/answer`에 `{"state": {...}, "batchId": "demo-1", "questionId": "material_basis", "answer": {"status": "unknown"}, "skip": false}`를 보낸다. 각 질문에 대해 반환된 최신 `state`로 답변하거나 `answer` 없이 `"skip": true`로 건너뛴다.
4. 응답의 `evaluation`을 확인한다. 질문이 남으면 최신 `state`로 다시 `/select`하고, 종료 상태라면 `unconfirmed`를 확인한다.

#### 복사해서 실행하는 전체 흐름

아래 예시는 가상 PP 입력을 사용한다. 첫 번째 터미널에서 `backend`를 작업
디렉터리로 서버를 시작한다. 빈 환경변수를 명시해 이 예시에서는 `.env`의 Azure
설정을 사용하지 않으며 `/select`가 규칙 기반 결과를 반환한다.

```bash
uv sync --extra dev
OPENAI_ENDPOINT= CHAT_DEPLOYMENT= FOUNDRY_KEY= uv run uvicorn app.main:app --reload
```

서버의 `Settings`는 프로세스 시작 시 **현재 작업 디렉터리의 `.env`**를 읽는다.
운영체제 환경변수는 `.env` 값보다 우선한다. 위 빈 환경변수 없이 서버를 실행하고
세 설정이 준비돼 있으면 `/select`는 실제 Azure를 호출한다. `.env`를 편집한 뒤에는
서버를 재시작한다.

두 번째 터미널에서 아래를 실행한다. 각 전이는 바로 이전 응답의 `state`를 사용한다.
첫 묶음의 재질 근거에는 모름을 답하고 나머지는 건너뛴다. 두 번째 묶음의 질문도
건너뛰고 종료 상태와 미확인 항목을 출력한다.

```bash
python3 - <<'PY'
import json
import urllib.request

base = "http://127.0.0.1:8000/api/v1/c/questions"

def post(action, payload):
    request = urllib.request.Request(
        f"{base}/{action}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)

state = {"material": {"status": "value", "value": "PP"}}
for batch_id in ("demo-1", "demo-2"):
    selection = post("select", {"state": state})
    print(batch_id, "선택 출처:", selection["source"])
    if selection["evaluation"]["status"] != "questions":
        break
    displayed = post("display", {
        "state": state, "batchId": batch_id,
        "selected": selection["selected"], "snapshotId": selection["snapshotId"],
    })
    state = displayed["state"]
    for question in selection["selected"]:
        answer = {"state": state, "batchId": batch_id, "questionId": question["id"]}
        if batch_id == "demo-1" and question["id"] == "material_basis":
            answer["answer"] = {"status": "unknown"}
        else:
            answer["skip"] = True
        state = post("answer", answer)["state"]
result = post("evaluate", {"state": state})
print("종료 상태:", result["status"], "/ 미확인:", result["unconfirmed"])
PY
```

일반 pytest는 Azure 호출을 모의 처리하며 실제 연결 성공 여부를 뜻하지 않는다.
별도의 Foundry 수동 검증은 앞 절의 실행 명령을 사용한다.

### C PP 근거 비교 API (저장 없음)

`POST /api/v1/c/evidence/compare`는 기존 `ReviewState`를 받아 선정된 2026년 9월
공식 문서의 근거와 항목별 비교 결과를 반환한다. 상태 저장이나 Azure 호출은 없다.
`unsupported`와 `insufficient_information`도 정상 결과이며, 가격 적용·수익·법적 적합성
판정이 아니다. 아래는 가상의 요청이다.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/c/evidence/compare \
  -H 'Content-Type: application/json' \
  -d '{"state":{"material":{"status":"value","value":"PP"},"form":{"status":"value","value":"가공 전"},"region":{"status":"value","value":"부산"}}}'
```
