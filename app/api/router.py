from fastapi import APIRouter

from app.api import c_questions, classify, contract, feasibility
from app.platform.api import router as platform_router
from app.platform.delivery import router as delivery_router
from app.platform.identity import router as identity_router

router = APIRouter(prefix="/api/v1")
router.include_router(contract.router)
router.include_router(feasibility.router)
router.include_router(c_questions.router)
router.include_router(classify.router)
router.include_router(platform_router)
router.include_router(identity_router)
router.include_router(delivery_router)


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
