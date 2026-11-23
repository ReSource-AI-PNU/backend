# BYPRO — Backend

배출 기업과 처리 업체의 AI가 폐플라스틱 거래 조건을 조율하는 서비스의 FastAPI 백엔드다. 현재 서비스는 `app/v3`와 `/api/v3`가 중심이며, 기존 `/api/v1` API도 별도로 남아 있다.

이전 서비스명은 Re:Source AI다. GitHub 주소·코드 경로·인프라 리소스 이름은 유지한다. [설계·구현 상태](https://github.com/ReSource-AI-PNU/ReSource-AI)에서 전체 구성을 확인할 수 있다.

## 현재 서비스

1. 사진·영상, 측정값 CSV, 계량 기록과 문서에서 폐기물·사업장 자료를 정리한다.
2. 판단 도구로 재활용 경로, 허가·법령 요건, 업체 후보, 수거 조건과 비용·탄소를 검토한다.
3. 회사 측 AI와 업체 측 AI가 조건을 조율한다. 모델의 초안은 근거 검증기와 가드레일을 통과해야 한다.
4. 규칙 기반 계약 검토 담당이 합의를 대조하고, 양쪽 담당자가 승인한 뒤 요청서·이행 계획서·위탁 계약서 초안을 만든다.
5. 반입 검수와 실제 결과를 기록해 실적과 다음 판단에 활용한다.

계산값에는 출처·계산 근거를 붙이고 팀 가정값은 구분한다. 묶음 수거는 업체의 기존 노선 안에서 검토한다. 실제 계약 체결·메일 발송·신고서 제출은 자동 수행하지 않는다.

## 구조

| 경로 | 역할 |
|---|---|
| `app/main.py` | FastAPI 앱과 v1·v3·계정·업체 MCP 라우터 등록 |
| `app/v3/api.py`, `accounts_api.py` | 현재 서비스·계정·조직 API |
| `app/v3/agents/` | 두 AI, 모델 선택, 대화 진행, 규칙 기반 계약 검토 |
| `app/v3/judge/` | 식별·경로·규정·업체·비용·탄소·배정 판단 도구 |
| `app/v3/mcp_server.py` | 판단 도구를 외부에 제공하는 별도 MCP 서버 |
| `app/v3/devserver.py` | 로컬 개발용 표준 라이브러리 HTTP 서버 |
| `app/api/`, `app/agents/` | 기존 v1 API와 계산·시연 코드 |
| `infra/vm/` | 앱 VM의 Docker Compose와 배포 스크립트 |
| `tests/v3/`, `docs/v3/` | 현재 서비스 시험과 계약·실행·평가 문서 |

## 로컬 실행

```bash
cp .env.example .env
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8000
```

- API 문서: http://localhost:8000/docs
- 헬스 체크: http://localhost:8000/api/v1/health
- 현재 API: `/api/v3`
- 개발용 모델 미연결 실행은 규칙 정책(`V3_LLM=rule`)을 쓴다. 실제 모델 실행과 구분한다.

프런트 개발·가상 시연에는 다음 서버도 사용할 수 있다.

```bash
python3 -m app.v3.devserver --port 8000
```

`V3_AUTH=header`는 로컬 시연용이다. 운영은 프런트 BFF와 백엔드가 `V3_AUTH=bff`로 계정·회사 소속을 확인한다. 운영 키나 실제 회사 자료를 로컬 시연 설정에 넣지 않는다. SQL 저장을 사용할 때는 `DATABASE_URL`을 설정하고 마이그레이션을 먼저 적용한다.

```bash
uv run alembic upgrade head
```

## 모델과 외부 서비스

- 회사 측은 Microsoft Foundry, 업체 측은 기본 구성에서 Ollama를 사용한다. 모델 선택과 장애 시 대체 정책은 `app/v3/agents/factory.py`의 환경 변수 설명을 따른다.
- 근거 저장·검증, Azure AI Search 검색, Document Intelligence 문서 추출, 탐지·3D 재구성, 카카오 도로 거리 등을 설정에 따라 연결한다. 자원이 없거나 호출에 실패한 경우의 보류·대체 처리를 실제 모델 성공으로 설명하지 않는다.
- `.env.example`과 `app/core/config.py`가 환경 변수 이름의 기준이다. 키는 로컬 환경이나 Key Vault로 주입하며 커밋하지 않는다.

## 검사

```bash
uv run ruff check .
uv run pytest -q
```

외부 모델·Azure·GPU 연동과 배포 검증은 단위 시험과 구분한다. 날짜·환경·남은 제한은 아래 검증 문서에 기록되어 있다.

## 배포와 문서

앱의 현재 배포 경로는 Azure VM의 Docker Compose(`infra/vm/`)다. Container Apps·App Service용 기존 파일도 남아 있으므로, 그 파일이 있다는 사실을 현재 운영 경로로 해석하지 않는다.

- [API·데이터 계약](docs/v3/CONTRACT.md)
- [대화 진행과 모델·근거 처리](docs/v3/RUNTIME.md)
- [2026-11-22 Azure 로컬 연동 검증](docs/v3/azure-integration-2026-11-22.md)
- [설문 후속 구현 검증](docs/verification.md)
- [인프라 설명](infra/README.md)

`/api/v1/feasibility/preview`와 `app.demo.synthetic`의 고정 단가·가상 공장 엔진은 이전 시연용이다. 현재 v3 거래의 실제 단가·업체 수용 여부·현장 품질·거래 가능성을 확정하는 근거로 쓰지 않는다. 이전 실행 예시와 상세 설명은 Git 이력에서 확인할 수 있다.
