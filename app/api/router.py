from fastapi import APIRouter

from app.api import c_questions, contract, feasibility

router = APIRouter(prefix="/api/v1")
router.include_router(contract.router)
router.include_router(feasibility.router)
router.include_router(c_questions.router)


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
