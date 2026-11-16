"""저장과 외부 호출 없이 업체 문의 Markdown 초안을 생성한다."""

from fastapi import APIRouter

from app.agents.pp_questions import ReviewState
from app.agents.vendor_inquiry import build_vendor_inquiry_draft
from app.schemas import Contract

router = APIRouter(prefix="/c/vendor-inquiry", tags=["C 업체 문의"])


class VendorInquiryRequest(Contract):
    state: ReviewState


class VendorInquiryResponse(Contract):
    markdown: str


@router.post("/draft", response_model=VendorInquiryResponse)
def draft(request: VendorInquiryRequest) -> VendorInquiryResponse:
    """입력 상태를 바꾸지 않고 문의 초안을 반환한다."""
    return VendorInquiryResponse(markdown=build_vendor_inquiry_draft(request.state))
