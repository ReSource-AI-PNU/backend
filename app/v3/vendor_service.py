"""업체 측 에이전트를 별도 서비스로 띄우는 FastAPI 앱.

주의: 이 파일은 작성한 환경에서 실행해 보지 못했다(fastapi를 불러올 수 없었다).
tests/v3/test_vendor_service.py가 CI에서 확인한다.

    uvicorn app.v3.vendor_service:app --host 0.0.0.0 --port 8000

- POST /a2a/messages  본문 {"message": {...}, "context": {...}}, 답 {"message": {...}}
- GET  /healthz
- 환경 변수: VENDOR_ORG_ID(이 서비스가 대변하는 업체), VENDOR_SERVICE_KEY(Bearer 서비스 키),
  모델은 API와 같은 V3_LLM, FOUNDRY_*, OLLAMA_BASE_URL. 업체 프로필의 model 선택을 따른다.
- 서비스 키가 설정되지 않았으면 모든 메시지를 거절한다(열린 채로 돌지 않는다).
- 답 메시지는 받는 쪽(회사 측)이 다시 검증한다. 여기서 검증했다는 표시를 붙이지 않는다.
"""

import hmac
from typing import Any

from fastapi import FastAPI, Request

from app.core.config import settings
from app.v3 import service
from app.v3.api import ApiError, call, guarded, json_object

app = FastAPI(title="Re:Source AI 업체 측 에이전트", version="0.1.0")


def check_service_key(request: Request) -> None:
    expected = settings.vendor_service_key
    if not expected:
        raise ApiError("SERVICE_KEY_NOT_SET", "서비스 키가 설정되지 않았습니다.", 503)
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    given = token.strip()
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        given.encode("utf-8"), expected.encode("utf-8")
    ):
        raise ApiError("UNAUTHORIZED", "서비스 키가 맞지 않습니다.", 401)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "vendorOrgId": settings.vendor_org_id or None}


@app.post("/a2a/messages")
@guarded
async def messages(request: Request):
    check_service_key(request)
    vendor_org_id = settings.vendor_org_id
    if not vendor_org_id:
        raise ApiError("VENDOR_NOT_SET", "VENDOR_ORG_ID가 설정되지 않았습니다.", 503)
    body = await json_object(request)
    message = body.get("message")
    context = body.get("context")
    if context is None:
        context = {}
    if not isinstance(message, dict) or not isinstance(context, dict):
        raise ApiError("INVALID_BODY", "message와 context는 JSON 객체여야 합니다.", 422)
    if message.get("to") != vendor_org_id:
        raise ApiError(
            "WRONG_RECIPIENT", "이 서비스가 대변하는 업체에게 온 메시지가 아닙니다.", 403
        )
    reply = await call(service.handle_vendor_message, vendor_org_id, message, context)
    return {"message": reply}
