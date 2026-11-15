from fastapi import APIRouter

from app.api import c_evidence, c_questions, classify, contract, feasibility

router = APIRouter(prefix="/api/v1")
router.include_router(contract.router)
router.include_router(feasibility.router)
router.include_router(c_questions.router)
router.include_router(c_evidence.router)
router.include_router(classify.router)


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
