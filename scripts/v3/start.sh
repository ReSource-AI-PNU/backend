#!/bin/sh
# 미실행·미검증: 컨테이너 안에서 돌려 보지 못했다(sh -n 문법 확인만 했다).
# 같은 이미지에서 APP_ROLE로 띄울 서비스를 고른다. 값이 없으면 API다.
#   api          : 모델 가중치를 받고(있으면) FastAPI를 띄운다 (이전 CMD와 같다)
#   vendor-agent : 업체 측 에이전트 서비스 (VENDOR_ORG_ID, VENDOR_SERVICE_KEY 필요)
#   mcp          : 판단 도구 MCP 서버 (MCP_SERVICE_KEY 필요)
#   migrate      : alembic upgrade head 를 돌리고 끝난다
set -e
case "${APP_ROLE:-api}" in
  api)
    python scripts/fetch_models.py
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  vendor-agent)
    exec uvicorn app.v3.vendor_service:app --host 0.0.0.0 --port 8000
    ;;
  mcp)
    exec python -m app.v3.mcp_server --host 0.0.0.0 --port 8000
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    echo "알 수 없는 APP_ROLE: ${APP_ROLE}" >&2
    exit 2
    ;;
esac
