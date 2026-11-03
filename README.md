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

## 배포

`Dockerfile`로 컨테이너를 빌드해 Azure Container Apps에 올린다. GitHub Actions 워크플로와 Bicep은 이후 추가한다.
