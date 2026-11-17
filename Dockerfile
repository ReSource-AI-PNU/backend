FROM python:3.12-slim

# 함께 넣을 선택 의존성. 기본은 추론 extra(ml: torch CPU 휠, 학습 코드 없음)다.
# 추론 없이 가볍게 만들려면 --build-arg EXTRAS= 로 비운다. mcp-server extra(MCP 서버)는 항상 들어간다.
ARG EXTRAS=ml

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu ".[mcp-server${EXTRAS:+,${EXTRAS}}]"
COPY app ./app
COPY scripts ./scripts
# 배포 때마다 같은 이미지로 마이그레이션을 돌린다: alembic upgrade head
COPY alembic.ini ./
COPY migrations ./migrations

# 가중치는 이미지에 넣지 않는다. 시작 시 MODEL_BLOB_URL 이 있으면 models/ 로 내려받고, 없으면 제로샷(Azure OpenAI)으로 동작.
# 같은 이미지에서 환경 변수 APP_ROLE(api | vendor-agent | mcp | migrate)이나 실행 명령만 바꿔 띄운다.
# 기본(api)은 이전 CMD와 같은 순서다. v3 부분은 미실행·미검증이다. 직접 띄우는 명령:
#   업체 측 에이전트 : uvicorn app.v3.vendor_service:app --host 0.0.0.0 --port 8000
#   판단 도구 MCP    : python -m app.v3.mcp_server --host 0.0.0.0 --port 8000
EXPOSE 8000
CMD ["sh", "scripts/v3/start.sh"]
