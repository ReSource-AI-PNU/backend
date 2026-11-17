from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.core.config import settings
from app.v3.api import router as v3_router

app = FastAPI(
    title="Re:Source AI API",
    version="0.1.0",
    description="산업 폐기물 등급 판정과 산업단지 묶음 수거 매칭 에이전트 API",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(v3_router)  # 설계안 v3: /api/v3
