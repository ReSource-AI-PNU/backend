from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.core.config import settings
from app.v3 import httpio
from app.v3.accounts_api import router as v3_accounts_router
from app.v3.api import mcp_router as v3_mcp_router
from app.v3.api import router as v3_router

app = FastAPI(
    title="Re:Source AI API",
    version="0.1.0",
    description="산업 폐기물 등급 판정과 산업단지 묶음 수거 매칭 에이전트 API",
)

app.add_middleware(
    CORSMiddleware,
    # CORS_ORIGINS(JSON 목록)에 V3_CORS_ORIGINS(쉼표로 나눈 출처)를 더한다. 로컬 서버와 같은 이름이다
    allow_origins=list(
        dict.fromkeys([*settings.cors_origins, *httpio.extra_origins(settings.v3_cors_origins)])
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(v3_router)  # 설계안 v3: /api/v3
app.include_router(v3_accounts_router)  # 설계안 v3 계정·조직(V3_AUTH=bff): /api/v3/accounts 등
app.include_router(v3_mcp_router)  # 업체 연결 키로 붙는 MCP: /mcp/vendor
