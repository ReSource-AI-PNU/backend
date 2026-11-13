"""독립적인 PP 질문 정책. 저장과 표시 시점의 동시성 통제는 호출자가 맡는다."""

import hashlib
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.schemas import Contract

FieldId = Literal[
    "material",
    "material_basis",
    "form",
    "contamination",
    "separation",
    "process",
    "sorting",
    "color",
    "trade_stage",
    "region",
    "quantity",
]
Status = Literal["unanswered", "unknown", "value", "conflict"]


class InputField(Contract):
    status: Status = "unanswered"
    value: str | None = None
    # 호출자는 해당 항목에 관련된 새 정보가 생겼을 때만 revision을 증가시킨다.
    revision: int = Field(default=0, ge=0)
    answered_revision: int | None = Field(default=None, ge=0)
    reask_reason: str | None = None

    @model_validator(mode="after")
    def check_state(self):
        if self.status == "unknown" and self.answered_revision is None:
            self.answered_revision = self.revision
        if self.status == "value" and (self.value is None or not self.value.strip()):
            raise ValueError("value 상태에는 값이 필요합니다")
        if self.status in ("unknown", "unanswered") and self.value is not None:
            raise ValueError("미응답과 모름에는 값을 지정할 수 없습니다")
        if self.answered_revision is not None and self.answered_revision > self.revision:
            raise ValueError("답변 버전이 현재 정보 버전보다 클 수 없습니다")
        if (
            self.answered_revision is not None
            and self.revision > self.answered_revision
            and (not self.reask_reason or not self.reask_reason.strip())
        ):
            raise ValueError("새 정보에 의한 재질문에는 근거가 필요합니다")
        return self


class FormField(InputField):
    @model_validator(mode="before")
    @classmethod
    def normalize_form(cls, data):
        if isinstance(data, InputField):
            data = data.model_dump()
        if isinstance(data, dict):
            data = dict(data)
            value = data.get("value")
            if isinstance(value, str):
                value = value.strip()
                if value == "모름" and data.get("status") == "value":
                    data["status"], value = "unknown", None
                elif value not in ("가공 전", "압축", "세척·파쇄", "펠렛"):
                    raise ValueError("지원하지 않는 형태입니다")
                data["value"] = value
        return data


class Quantity(Contract):
    amount: float = Field(ge=0, allow_inf_nan=False)
    unit: str = Field(min_length=1)
    period: str = Field(min_length=1)
    basis: Literal["measured", "estimated", "unknown"]

    @field_validator("unit", "period", mode="before")
    @classmethod
    def trim_dimension(cls, value):
        return value.strip() if isinstance(value, str) else value


class QuantityField(Contract):
    status: Status = "unanswered"
    value: Quantity | None = None
    revision: int = Field(default=0, ge=0)
    answered_revision: int | None = Field(default=None, ge=0)
    reask_reason: str | None = None

    @model_validator(mode="after")
    def check_state(self):
        if self.status == "unknown" and self.answered_revision is None:
            self.answered_revision = self.revision
        if self.status == "value" and self.value is None:
            raise ValueError("value 상태에는 물량이 필요합니다")
        if self.status in ("unknown", "unanswered") and self.value is not None:
            raise ValueError("미응답과 모름에는 물량을 지정할 수 없습니다")
        if self.answered_revision is not None and self.answered_revision > self.revision:
            raise ValueError("답변 버전이 현재 정보 버전보다 클 수 없습니다")
        if (
            self.answered_revision is not None
            and self.revision > self.answered_revision
            and (not self.reask_reason or not self.reask_reason.strip())
        ):
            raise ValueError("새 정보에 의한 재질문에는 근거가 필요합니다")
        return self


class ContaminationField(Contract):
    status: Status = "unanswered"
    value: Literal["absent", "present"] | None = None
    description: str | None = None
    revision: int = Field(default=0, ge=0)
    answered_revision: int | None = Field(default=None, ge=0)
    reask_reason: str | None = None

    @model_validator(mode="after")
    def check_state(self):
        if self.status == "unknown" and self.answered_revision is None:
            self.answered_revision = self.revision
        if (self.status == "value") != (self.value is not None):
            raise ValueError("혼입 값은 value 상태에서만 필요합니다")
        if self.answered_revision is not None and self.answered_revision > self.revision:
            raise ValueError("답변 버전이 현재 정보 버전보다 클 수 없습니다")
        if (
            self.answered_revision is not None
            and self.revision > self.answered_revision
            and (not self.reask_reason or not self.reask_reason.strip())
        ):
            raise ValueError("새 정보에 의한 재질문에는 근거가 필요합니다")
        return self


class DisplayedQuestion(Contract):
    selection: "Selection"
    revision: int = Field(ge=0)
    outcome: Literal["pending", "answered", "skipped"] = "pending"


class DisplayedBatch(Contract):
    id: str = Field(min_length=1)
    questions: tuple[DisplayedQuestion, ...] = Field(min_length=1, max_length=3)
    snapshot_id: str = Field(min_length=1)


class ReviewState(Contract):
    material: InputField = Field(default_factory=InputField)
    material_basis: InputField = Field(default_factory=InputField)
    form: FormField = Field(default_factory=FormField)
    contamination: ContaminationField = Field(default_factory=ContaminationField)
    separation: InputField = Field(default_factory=InputField)
    process: InputField = Field(default_factory=InputField)
    sorting: InputField = Field(default_factory=InputField)
    color: InputField = Field(default_factory=InputField)
    trade_stage: InputField = Field(default_factory=InputField)
    region: InputField = Field(default_factory=InputField)
    quantity: QuantityField = Field(default_factory=QuantityField)
    batches: tuple[DisplayedBatch, ...] = ()

    @model_validator(mode="after")
    def check_batches(self):
        ids = [batch.id for batch in self.batches]
        if len(ids) > 2 or len(set(ids)) != len(ids) or any(not x.strip() for x in ids):
            raise ValueError("표시 묶음 ID는 중복 없이 최대 2개여야 합니다")
        for batch in self.batches:
            question_ids = [q.selection.id for q in batch.questions]
            if len(question_ids) != len(set(question_ids)):
                raise ValueError("묶음 안의 질문 ID는 중복될 수 없습니다")
        if any(q.outcome == "pending" for batch in self.batches[:-1] for q in batch.questions):
            raise ValueError("이전 묶음에 대기 중인 질문이 있습니다")
        return self


class Candidate(Contract):
    id: FieldId
    text: str
    why: str
    kind: Literal["choice", "text", "number"]
    options: tuple[str, ...] = ()
    reask_reason: str | None = None
    revision: int = Field(ge=0)


class Selection(Contract):
    id: FieldId
    text: str = Field(min_length=1)
    why: str = Field(min_length=1)
    kind: Literal["choice", "text", "number"]
    options: tuple[str, ...] = ()

    @field_validator("text", "why", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class QuestionResult(Contract):
    status: Literal["questions", "awaiting_answers", "complete", "limit_reached", "unsupported"]
    snapshot_id: str | None = None
    candidates: tuple[Candidate, ...] = ()
    unconfirmed: tuple[FieldId, ...] = ()


# 순서가 정책 우선순위다. 선택지는 코드가 소유한다.
_SPECS = {
    "material": ("재질은 무엇인가요?", "PP 지원 범위를 확인합니다.", "text", ()),
    "material_basis": (
        "재질을 판단한 근거는 무엇인가요?",
        "재질 진술의 근거를 확인합니다.",
        "text",
        (),
    ),
    "form": (
        "현재 형태는 무엇인가요?",
        "관련 조사 품목을 구분합니다.",
        "choice",
        ("가공 전", "압축", "세척·파쇄", "펠렛", "모름"),
    ),
    "contamination": (
        "다른 재질이나 금속이 섞여 있나요?",
        "혼입 상태를 확인합니다.",
        "choice",
        ("없음으로 파악", "있음", "모름"),
    ),
    "separation": (
        "혼입물을 분리하거나 구분 보관할 수 있나요?",
        "업체에 확인할 분리 상태를 파악합니다.",
        "text",
        (),
    ),
    "process": ("어떤 과정에서 발생하나요?", "발생 과정을 확인합니다.", "text", ()),
    "sorting": ("알고 있는 선별 상태는 무엇인가요?", "조사 조건과 비교합니다.", "text", ()),
    "color": ("색상은 어떤 상태인가요?", "조사 조건과 비교합니다.", "text", ()),
    "trade_stage": (
        "현재 판매 또는 인도 단계는 무엇인가요?",
        "조사 거래 단계와 비교합니다.",
        "text",
        (),
    ),
    "region": ("발생 지역은 어디인가요?", "관련 지역을 확인합니다.", "text", ()),
    "quantity": (
        "발생량과 단위, 기간 및 측정·추정 여부는 무엇인가요?",
        "물량 정보를 보존합니다.",
        "text",
        (),
    ),
}


def _eligible(field: InputField | QuantityField | ContaminationField) -> bool:
    if field.status in ("unanswered", "conflict"):
        return True
    return (
        field.status == "unknown"
        and field.answered_revision is not None
        and field.revision > field.answered_revision
    )


def _snapshot_id(state: ReviewState) -> str:
    payload = json.dumps(
        state.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate(state: ReviewState) -> QuestionResult:
    """현재 상태를 읽기만 한다. 관련 후보 전체를 우선순위 순으로 반환한다."""
    state = ReviewState.model_validate(state.model_dump())
    active: list[FieldId] = ["material", "material_basis", "form", "contamination"]
    if state.contamination.status == "value" and state.contamination.value == "present":
        active.append("separation")
    active.append("process")
    if state.form.status == "value" and state.form.value in ("압축", "세척·파쇄", "펠렛"):
        active.extend(["sorting", "color", "trade_stage"])
    active.extend(["region", "quantity"])
    remaining = tuple(key for key in active if getattr(state, key).status != "value")
    material = state.material
    if any(q.outcome == "pending" for batch in state.batches for q in batch.questions):
        return QuestionResult(status="awaiting_answers", unconfirmed=remaining)
    if material.status == "value" and material.value.strip().upper() != "PP":
        return QuestionResult(status="unsupported", unconfirmed=remaining)
    candidates = []
    for key in active:
        field = getattr(state, key)
        skipped = any(
            q.selection.id == key and q.revision == field.revision and q.outcome == "skipped"
            for batch in state.batches
            for q in batch.questions
        )
        if _eligible(field) and not skipped:
            text, why, kind, options = _SPECS[key]
            candidates.append(
                Candidate(
                    id=key,
                    text=text,
                    why=why,
                    kind=kind,
                    options=options,
                    reask_reason=field.reask_reason,
                    revision=field.revision,
                )
            )
    if not candidates:
        return QuestionResult(status="complete", unconfirmed=remaining)
    if len(state.batches) >= 2:
        return QuestionResult(status="limit_reached", unconfirmed=remaining)
    return QuestionResult(
        status="questions",
        candidates=tuple(candidates),
        unconfirmed=remaining,
        snapshot_id=_snapshot_id(state),
    )


def validate_selection(allowed: tuple[Candidate, ...], selected: tuple[Selection, ...]) -> None:
    """모델 결과를 검증한다. 오류는 호출자에게 전달하며 폴백하지 않는다."""
    selected = tuple(Selection.model_validate(item.model_dump()) for item in selected)
    if len({c.id for c in allowed}) != len(allowed):
        raise ValueError("허용 후보 ID는 중복될 수 없습니다")
    by_id = {c.id: c for c in allowed}
    ids = [s.id for s in selected]
    if not ids or len(ids) > 3 or len(ids) != len(set(ids)):
        raise ValueError("선택은 중복 없이 1~3개여야 합니다")
    # 미해결 재질은 지원 범위 확인의 선행 조건이다. 근거도 후보이면 함께 선택한다.
    if "material" in by_id and "material" not in ids:
        raise ValueError("재질 확인을 먼저 선택해야 합니다")
    if "material_basis" in by_id and "material_basis" not in ids:
        raise ValueError("재질 판단 근거를 먼저 선택해야 합니다")
    for item in selected:
        candidate = by_id.get(item.id)
        if candidate is None or item.kind != candidate.kind or item.options != candidate.options:
            raise ValueError("허용되지 않은 ID 또는 변조된 응답 형식입니다")


def record_display(
    state: ReviewState, batch_id: str, selected: tuple[Selection, ...], snapshot_id: str
) -> ReviewState:
    """같은 요청의 재시도만 멱등 처리하며 대기 중 새 묶음을 금지한다."""
    state = ReviewState.model_validate(state.model_dump())
    selected = tuple(Selection.model_validate(item.model_dump()) for item in selected)
    if not batch_id.strip():
        raise ValueError("묶음 ID가 필요합니다")
    for batch in state.batches:
        if batch.id == batch_id:
            if (
                batch.snapshot_id != snapshot_id
                or tuple(q.selection for q in batch.questions) != selected
            ):
                raise ValueError("같은 묶음 ID에 다른 요청을 사용할 수 없습니다")
            return state
    result = evaluate(state)
    if result.status != "questions":
        raise ValueError("표시할 질문 묶음이 없습니다")
    if snapshot_id != result.snapshot_id:
        raise ValueError("오래된 후보 스냅샷입니다")
    validate_selection(result.candidates, selected)
    by_id = {c.id: c for c in result.candidates}
    batch = DisplayedBatch(
        id=batch_id,
        snapshot_id=snapshot_id,
        questions=tuple(
            DisplayedQuestion(selection=item, revision=by_id[item.id].revision) for item in selected
        ),
    )
    return state.model_copy(update={"batches": (*state.batches, batch)})


def resolve_question(
    state: ReviewState,
    batch_id: str,
    question_id: FieldId,
    answer: InputField | QuantityField | ContaminationField | None = None,
    *,
    skip: bool = False,
) -> ReviewState:
    """질문별 답변 또는 건너뛰기를 반영한다. 모든 질문 처리 후 다음 묶음으로 간다."""
    state = ReviewState.model_validate(state.model_dump())
    if not state.batches or state.batches[-1].id != batch_id:
        raise ValueError("현재 대기 묶음이 아닙니다")
    batch = state.batches[-1]
    matches = [q for q in batch.questions if q.selection.id == question_id]
    if len(matches) != 1 or matches[0].outcome != "pending":
        raise ValueError("대기 중인 질문이 아닙니다")
    question = matches[0]
    current = getattr(state, question_id)
    if not skip and current.revision != question.revision:
        raise ValueError("표시 후 대상 항목이 변경됐습니다")
    if skip:
        if answer is not None:
            raise ValueError("건너뛰기에는 답변을 지정할 수 없습니다")
        answer = current
    else:
        expected = (
            QuantityField
            if question_id == "quantity"
            else ContaminationField
            if question_id == "contamination"
            else InputField
        )
        if not isinstance(answer, expected) or answer.status not in (
            "value",
            "unknown",
            "conflict",
        ):
            raise ValueError("해당 항목에 맞는 답변이 필요합니다")
        answer = (FormField if question_id == "form" else expected).model_validate(
            answer.model_dump()
        )
        if answer.revision != question.revision:
            raise ValueError("답변의 대상 버전이 다릅니다")
        answer = answer.model_copy(update={"answered_revision": question.revision})
    questions = tuple(
        q.model_copy(update={"outcome": "skipped" if skip else "answered"})
        if q.selection.id == question_id
        else q
        for q in batch.questions
    )
    updated_batch = batch.model_copy(update={"questions": questions})
    return state.model_copy(
        update={question_id: answer, "batches": (*state.batches[:-1], updated_batch)}
    )
