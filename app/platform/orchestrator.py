"""Ordered stage runner. Missing A/B/C handlers are explicit handoff boundaries."""

from collections.abc import Callable

from pydantic import ValidationError

from app.platform.adapters import AdapterError
from app.platform.models import PlatformState
from app.schemas import (
    CostCarbonResult,
    FeasibilityResult,
    MatchingResult,
    ProposalResult,
    RegulationResult,
)

STEPS = ("classify", "feasibility", "regulation", "matching", "cost-carbon", "proposal")
FIELDS = {
    "feasibility": "feasibility",
    "regulation": "regulation",
    "matching": "matching",
    "cost-carbon": "cost_carbon",
    "proposal": "proposal",
}
RESULTS = {
    "matching": MatchingResult,
    "feasibility": FeasibilityResult,
    "regulation": RegulationResult,
    "cost-carbon": CostCarbonResult,
    "proposal": ProposalResult,
}


def advance(state: PlatformState, target: str, handlers: dict[str, Callable]):
    """Return first blocked step; never manufacture an unavailable result."""
    for step in STEPS[: STEPS.index(target) + 1]:
        if step == "classify":
            if any(not s.monthly_kg_confirmed for s in state.session.streams):
                return step, "월 발생량 확인 질문에 답하세요"
        elif getattr(state.session, FIELDS[step]) is None:
            handler = handlers.get(step)
            if handler is None:
                return step, f"{step} 담당 트랙의 실행 어댑터 연결 대기"
            try:
                result = RESULTS[step].model_validate(handler(state.session.model_copy(deep=True)))
            except (AdapterError, ValidationError, KeyError) as exc:
                return step, str(exc) if isinstance(
                    exc, AdapterError
                ) else "단계 응답이 데이터 계약과 다릅니다"
            setattr(state.session, FIELDS[step], result)
        if step == "matching" and any(not b.ready for b in state.bundles):
            return step, "묶음 참여 동의와 최소 수거 물량을 확인하세요"
        if any(q.step == step and q.answer is None for q in state.session.questions):
            return step, "이 단계의 확인 질문에 답하세요"
        if step == "feasibility" and state.session.feasibility.status == "needs_information":
            return step, "경로 판정에 필요한 정보가 부족합니다"
        if step == "regulation" and any(i.status != "ok" for i in state.session.regulation.items):
            return step, "규정 검토의 미확인·불가 항목을 해결하세요"
        state.session.step = step
    if target == "proposal":
        state.session.status = "awaiting-approval"
    return None, None
