# Re:Source AI — Backend

FastAPI 기반 에이전트 API. 설계 문서는 [ReSource-AI](https://github.com/ReSource-AI-PNU/ReSource-AI) 저장소의 `docs/`를 참고한다.

## 구조

```
app/
├── main.py          # FastAPI 앱, CORS, 라우터 등록
├── api/router.py    # /api/v1 엔드포인트
├── agents/          # 대회 6단계 에이전트 (README 참고)
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
