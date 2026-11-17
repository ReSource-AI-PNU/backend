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
- 같은 일을 하는 표준 라이브러리 서버가 app/v3/vendor_devserver.py에 있다(로컬에서 실제로 띄워 본 것은
  그쪽이다). 서비스 키와 본문 확인은 두 서버가 httpio의 같은 함수를 쓴다.
"""

from typing import Any

from fastapi import FastAPI, Request

from app.core.config import settings
from app.v3 import httpio, service
from app.v3.api import call, guarded, json_object

app = FastAPI(title="Re:Source AI 업체 측 에이전트", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "vendorOrgId": settings.vendor_org_id or None}


@app.post("/a2a/messages")
@guarded
async def messages(request: Request):
    # 서비스 키와 본문 확인은 표준 라이브러리 서버(vendor_devserver.py)와 같은 함수로 한다
    httpio.check_service_key(request.headers, settings.vendor_service_key)
    vendor_org_id = httpio.vendor_org_required(settings.vendor_org_id)
    message, context = httpio.vendor_request(await json_object(request), vendor_org_id)
    reply = await call(service.handle_vendor_message, vendor_org_id, message, context)
    return {"message": reply}
