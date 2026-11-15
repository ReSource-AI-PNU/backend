"""저장과 외부 호출 없이 PP 근거를 비교하는 API."""

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter

from app.agents.pp_evidence import compare_pp_evidence
from app.agents.pp_questions import (
    ContaminationField,
    FormField,
    InputField,
    QuantityField,
    ReviewState,
)
from app.schemas import Contract

router = APIRouter(prefix="/c/evidence", tags=["C 근거 비교"])


class EvidenceRequest(Contract):
    state: ReviewState


class InputSummary(Contract):
    material: InputField
    material_basis: InputField
    form: FormField
    contamination: ContaminationField
    separation: InputField
    process: InputField
    sorting: InputField
    color: InputField
    trade_stage: InputField
    region: InputField
    quantity: QuantityField


class SourceResponse(Contract):
    document: str
    url: str
    pdf_page: int
    printed_page: int


class EvidenceResponse(Contract):
    id: str
    reference_month: str
    material: str
    form: str
    quality: str
    processing: str
    trade_stage: str
    region: str
    vat: str
    logistics: str
    criteria: SourceResponse
    price_table: SourceResponse
    general_criteria: SourceResponse
    detailed_criteria: SourceResponse
    faq: SourceResponse


class FindingResponse(Contract):
    status: Literal["match", "different", "unconfirmed"]
    reason: str


class ComparisonResponse(Contract):
    evidence: EvidenceResponse
    material: FindingResponse
    form: FindingResponse
    region: FindingResponse
    quality: FindingResponse
    trade_stage: FindingResponse


class EvidenceComparisonResponse(Contract):
    status: Literal["compared", "insufficient_information", "unsupported"]
    input_summary: InputSummary
    comparisons: tuple[ComparisonResponse, ...]
    limitations: tuple[str, ...]
    follow_up: tuple[str, ...]


@router.post("/compare", response_model=EvidenceComparisonResponse)
def compare(request: EvidenceRequest) -> EvidenceComparisonResponse:
    """선정 문서와 입력을 비교하며 상태를 저장하거나 변경하지 않는다."""
    return EvidenceComparisonResponse.model_validate(asdict(compare_pp_evidence(request.state)))
