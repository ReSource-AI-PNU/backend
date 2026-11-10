from fastapi import APIRouter

from app.api import classify, contract

router = APIRouter(prefix="/api/v1")
router.include_router(contract.router)
router.include_router(classify.router)


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
