"""저장과 외부 호출 없이 자원 명세 Markdown 초안을 생성한다."""

from fastapi import APIRouter

from app.agents.pp_questions import ReviewState
from app.agents.pp_resource_spec import build_resource_spec_draft
from app.schemas import Contract

router = APIRouter(prefix="/c/resource-spec", tags=["C 자원 명세"])


class ResourceSpecRequest(Contract):
    state: ReviewState


class ResourceSpecResponse(Contract):
    markdown: str


@router.post("/draft", response_model=ResourceSpecResponse)
def draft(request: ResourceSpecRequest) -> ResourceSpecResponse:
    """입력 상태를 바꾸지 않고 초안을 반환한다."""
    return ResourceSpecResponse(markdown=build_resource_spec_draft(request.state))
