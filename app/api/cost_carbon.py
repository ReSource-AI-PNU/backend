"""Stateless synthetic cost·carbon preview; no session is created or modified."""

from typing import Annotated

from fastapi import APIRouter, Body

from app.agents.cost_carbon import evaluate_cost_carbon
from app.agents.economics_adapter import evaluate_streams
from app.api.feasibility import DEMO_REQUEST, FeasibilityPreviewRequest
from app.demo.synthetic import CARBON_CONFIG, CONFIG
from app.schemas import CarbonBaseline, CostCarbonResult

router = APIRouter(prefix="/cost-carbon", tags=["cost-carbon"])


class CostCarbonPreviewRequest(FeasibilityPreviewRequest):
    """경로 판정 미리보기와 같은 입력에 탄소 기준을 더한다. 단가·계수는 서버의 시연값이다."""

    baseline: CarbonBaseline = "신재 대비"


@router.post("/preview", response_model=CostCarbonResult)
def preview(
    request: Annotated[
        CostCarbonPreviewRequest,
        Body(openapi_examples={
            "synthetic_factory": {
                "summary": "가상 PP 사출 공장 네 흐름",
                "description": "경로 판정 후 시나리오 A·B·C·D를 서버의 synthetic/demo 값으로 계산합니다.",
                "value": {**DEMO_REQUEST, "baseline": "신재 대비"},
            }
        }),
    ],
) -> CostCarbonResult:
    """경로 판정을 거쳐 시나리오 A·B·C·D의 월 손익·순 탄소·KAU를 계산한다. 저장하지 않는다."""
    feasibility = evaluate_streams(request.streams, CONFIG, request.overrides)
    return evaluate_cost_carbon(request.streams, feasibility, CARBON_CONFIG, request.baseline)
