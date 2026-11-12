from fastapi import APIRouter

from app.api import contract

router = APIRouter(prefix="/api/v1")
router.include_router(contract.router)


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
