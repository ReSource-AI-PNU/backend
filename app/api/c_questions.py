"""저장 없는 C 질문 상태 전이 API. 상태의 진실성과 동시성은 호출자가 관리한다."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import Field, ValidationError

from app.agents.pp_question_selector import SelectionResult, select_questions
from app.agents.pp_questions import (
    ContaminationField,
    FieldId,
    FormField,
    InputField,
    QuantityField,
    QuestionConflictError,
    QuestionInputError,
    QuestionResult,
    ReviewState,
    Selection,
    evaluate,
    record_display,
    resolve_question,
)
from app.core.config import settings
from app.schemas import Contract

router = APIRouter(prefix="/c/questions", tags=["C 질문 정책"])


class StateRequest(Contract):
    state: ReviewState = Field(
        json_schema_extra={"example": {"material": {"status": "value", "value": "PP"}}}
    )


class DisplayRequest(StateRequest):
    batch_id: str = Field(min_length=1)
    selected: tuple[Selection, ...]
    snapshot_id: str = Field(min_length=1)


class AnswerRequest(StateRequest):
    batch_id: str = Field(min_length=1)
    question_id: FieldId
    answer: dict[str, Any] | None = None
    skip: bool = False


class TransitionResult(Contract):
    state: ReviewState
    evaluation: QuestionResult


@router.post("/evaluate", response_model=QuestionResult)
def evaluate_state(request: StateRequest) -> QuestionResult:
    """질문 후보와 종료 상태를 평가한다. 외부 호출이나 상태 변경은 없다."""
    return evaluate(request.state)


@router.post("/select", response_model=SelectionResult)
def select(request: StateRequest) -> SelectionResult:
    """Azure 선택 또는 규칙 기반 대체를 반환한다. 표시 횟수는 바꾸지 않는다."""
    return select_questions(
        request.state,
        endpoint=settings.openai_endpoint,
        deployment=settings.chat_deployment,
        key=settings.foundry_key,
    )


def _transition_error(exc: QuestionInputError | QuestionConflictError) -> HTTPException:
    status = 409 if isinstance(exc, QuestionConflictError) else 422
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/display", response_model=TransitionResult)
def display(request: DisplayRequest) -> TransitionResult:
    """동일 상태·동일 요청 재처리는 멱등이다. 호출자가 반환 상태를 보관해야 한다."""
    try:
        state = record_display(
            request.state, request.batch_id, request.selected, request.snapshot_id
        )
    except (QuestionInputError, QuestionConflictError) as exc:
        raise _transition_error(exc) from exc
    return TransitionResult(state=state, evaluation=evaluate(state))


@router.post("/answer", response_model=TransitionResult)
def answer(request: AnswerRequest) -> TransitionResult:
    """한 질문에 답하거나 건너뛴다. 상태 저장·재전송 멱등성은 제공하지 않는다."""
    answer_type = (
        QuantityField
        if request.question_id == "quantity"
        else ContaminationField
        if request.question_id == "contamination"
        else FormField
        if request.question_id == "form"
        else InputField
    )
    try:
        answer_value = (
            answer_type.model_validate(request.answer) if request.answer is not None else None
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="답변 입력 형식이 올바르지 않습니다") from exc
    try:
        state = resolve_question(
            request.state, request.batch_id, request.question_id, answer_value, skip=request.skip
        )
    except (QuestionInputError, QuestionConflictError) as exc:
        raise _transition_error(exc) from exc
    return TransitionResult(state=state, evaluation=evaluate(state))
