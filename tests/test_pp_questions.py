"""가상 입력으로 질문 정책을 검증한다."""

import pytest
from pydantic import ValidationError

from app.agents.pp_questions import (
    ContaminationField,
    InputField,
    Quantity,
    QuantityField,
    ReviewState,
    Selection,
    evaluate,
    resolve_question,
    validate_selection,
)
from app.agents.pp_questions import (
    record_display as _record_display,
)


def record_display(state, batch_id, selected):
    existing = next((b for b in state.batches if b.id == batch_id), None)
    snapshot = existing.snapshot_id if existing else evaluate(state).snapshot_id
    return _record_display(state, batch_id, selected, snapshot or "")


def value(text):
    return InputField(status="value", value=text)


def selections(result, limit=3):
    return tuple(
        Selection(id=c.id, text=c.text, why=c.why, kind=c.kind, options=c.options)
        for c in result.candidates[:limit]
    )


@pytest.fixture
def pp():
    return ReviewState(
        material=value("PP"),
        material_basis=value("원자재 표기"),
        form=value("가공 전"),
        contamination=ContaminationField(status="value", value="absent"),
        process=value("생산 중"),
        region=value("부산"),
        quantity=QuantityField(
            status="value", value=Quantity(amount=500, unit="kg", period="월", basis="estimated")
        ),
    )


def test_priority_limit_and_read_only():
    state = ReviewState()
    result = evaluate(state)
    assert [c.id for c in result.candidates][:4] == [
        "material",
        "material_basis",
        "form",
        "contamination",
    ]
    assert len(result.unconfirmed) > 3
    assert evaluate(state) == result
    assert state.batches == ()


def test_display_only_counts_valid_batches_and_is_idempotent():
    state = ReviewState()
    result = evaluate(state)
    chosen = selections(result)
    with pytest.raises(ValueError):
        record_display(state, "one", ())
    one = record_display(state, "one", chosen)
    assert record_display(one, "one", chosen) == one
    assert one.batches[0].questions[0].revision == 0
    assert evaluate(one).status == "awaiting_answers"
    with pytest.raises(ValueError):
        record_display(one, "two", chosen)
    with pytest.raises(ValueError):
        record_display(one, "one", ())
    one = resolve_question(one, "one", "material", value("PP"))
    one = resolve_question(one, "one", "material_basis", skip=True)
    assert evaluate(one).status == "awaiting_answers"
    one = resolve_question(one, "one", "form", skip=True)
    assert evaluate(one).status == "questions"
    second = selections(evaluate(one))
    two = record_display(one, "two", second)
    assert evaluate(two).status == "awaiting_answers"
    for item in second:
        two = resolve_question(two, "two", item.id, skip=True)
    assert evaluate(two).status == "limit_reached"
    assert "quantity" in evaluate(two).unconfirmed
    with pytest.raises(ValueError):
        record_display(two, "three", chosen)


def test_unknown_resolved_and_new_relevant_information(pp):
    state = pp.model_copy(
        update={
            "form": InputField(status="unknown", answered_revision=0),
            "contamination": ContaminationField(status="unknown"),
        }
    )
    result = evaluate(state)
    assert result.status == "complete"
    assert set(result.unconfirmed) == {"form", "contamination"}
    changed = state.model_copy(
        update={
            "form": InputField(
                status="unknown", revision=1, answered_revision=0, reask_reason="새 공정 기록"
            )
        }
    )
    assert [c.id for c in evaluate(changed).candidates] == ["form"]
    assert evaluate(changed).candidates[0].reask_reason == "새 공정 기록"
    with pytest.raises(ValidationError):
        InputField(status="unknown", revision=1, answered_revision=0)


def test_missing_quantity_does_not_prevent_review(pp):
    state = pp.model_copy(update={"quantity": QuantityField(status="unknown")})
    result = evaluate(state)
    assert result.status == "complete"
    assert result.unconfirmed == ("quantity",)
    assert pp.quantity.value.basis == "estimated"


def test_quantity_validity():
    for amount in [-1, float("nan"), float("inf")]:
        with pytest.raises(ValidationError):
            Quantity(amount=amount, unit="kg", period="월", basis="measured")
    assert Quantity(amount=0, unit="kg", period="월", basis="estimated").amount == 0


def test_selection_validation(pp):
    result = evaluate(pp.model_copy(update={"form": InputField()}))
    selected = selections(result)
    validate_selection(result.candidates, selected)
    with pytest.raises(ValueError):
        validate_selection(result.candidates, selected * 2)
    with pytest.raises(ValueError):
        validate_selection(result.candidates, (selected[0].model_copy(update={"id": "region"}),))
    with pytest.raises(ValueError):
        validate_selection(
            result.candidates, (selected[0].model_copy(update={"options": ("가짜",)}),)
        )
    with pytest.raises(ValueError):
        validate_selection(result.candidates, selected * 4)


def test_material_change_to_pe_and_quantity_preserved(pp):
    changed = pp.model_copy(
        update={
            "material": value("PE"),
            "quantity": QuantityField(
                status="value", value=Quantity(amount=800, unit="kg", period="월", basis="measured")
            ),
        }
    )
    assert evaluate(changed).status == "unsupported"
    assert changed.quantity.value.basis == "measured"
    assert changed.quantity.value.amount == 800


def test_processed_forms_and_conflict(pp):
    state = pp.model_copy(
        update={
            "form": value("압축"),
            "contamination": ContaminationField(status="unknown"),
            "region": InputField(status="conflict"),
        }
    )
    assert [c.id for c in evaluate(state).candidates][:3] == ["sorting", "color", "trade_stage"]
    assert "region" in evaluate(state).unconfirmed


def test_contamination_structure_controls_separation(pp):
    absent = pp.model_copy(
        update={
            "contamination": ContaminationField(
                status="value", value="absent", description="금속 없음으로 파악"
            )
        }
    )
    assert evaluate(absent).status == "complete"
    present = pp.model_copy(
        update={
            "contamination": ContaminationField(
                status="value", value="present", description="금속 인서트"
            )
        }
    )
    assert [c.id for c in evaluate(present).candidates] == ["separation"]
    assert present.contamination.description == "금속 인서트"
    with pytest.raises(ValidationError):
        ContaminationField(status="value", value="없음")


def test_all_candidates_and_required_dependencies():
    result = evaluate(ReviewState())
    assert len(result.candidates) > 3
    lower = selections(result, 5)[2:5]
    with pytest.raises(ValueError):
        validate_selection(result.candidates, lower)
    with pytest.raises(ValueError):
        validate_selection(result.candidates, selections(result, 1))
    validate_selection(result.candidates, selections(result))


def test_answer_version_and_partial_pending():
    state = ReviewState()
    chosen = selections(evaluate(state))
    state = record_display(state, "batch", chosen)
    with pytest.raises(ValueError):
        resolve_question(
            state, "batch", "material", InputField(status="value", value="PP", revision=1)
        )
    state = resolve_question(state, "batch", "material", value("PP"))
    with pytest.raises(ValueError):
        resolve_question(state, "batch", "material", value("PP"))
    assert evaluate(state).status == "awaiting_answers"


def test_skipping_is_distinct_from_unknown_and_new_revision_can_be_asked():
    state = ReviewState(material=value("PP"), material_basis=value("표기"))
    chosen = selections(evaluate(state))
    state = record_display(state, "one", chosen)
    for item in chosen:
        state = resolve_question(state, "one", item.id, skip=True)
    assert state.form.status == "unanswered"
    assert "form" not in [c.id for c in evaluate(state).candidates]
    state = state.model_copy(update={"form": InputField(revision=1)})
    assert "form" in [c.id for c in evaluate(state).candidates]


def test_stale_answer_rejected_but_can_skip_without_losing_new_value():
    state = ReviewState()
    state = record_display(state, "one", selections(evaluate(state)))
    state = state.model_copy(
        update={"material": InputField(status="value", value="PE", revision=1)}
    )
    with pytest.raises(ValueError):
        resolve_question(state, "one", "material", value("PP"))
    state = resolve_question(state, "one", "material", skip=True)
    assert state.material.value == "PE"
    assert evaluate(state).status == "awaiting_answers"


def test_form_normalization_initial_and_answer(pp):
    initial = ReviewState(form=InputField(status="value", value=" 압축 "))
    assert initial.form.value == "압축"
    state = pp.model_copy(update={"form": initial.form})
    assert {"sorting", "color", "trade_stage"} <= {c.id for c in evaluate(state).candidates}
    assert ReviewState(form=InputField(status="value", value=" 모름 ")).form.status == "unknown"
    with pytest.raises(ValidationError):
        ReviewState(form=InputField(status="value", value="분쇄만 함"))
    state = pp.model_copy(update={"form": InputField()})
    result = evaluate(state)
    state = _record_display(state, "form", selections(result), result.snapshot_id)
    with pytest.raises(ValidationError):
        resolve_question(state, "form", "form", value("기타"))
    assert state.batches[0].questions[0].outcome == "pending"
    state = resolve_question(state, "form", "form", value(" 압축 "))
    assert state.form.value == "압축"


def test_stale_selection_and_exact_retry(pp):
    state = pp.model_copy(update={"form": InputField()})
    evaluated = evaluate(state)
    chosen = selections(evaluated)
    changed = state.model_copy(update={"form": InputField(revision=1)})
    with pytest.raises(ValueError):
        _record_display(changed, "one", chosen, evaluated.snapshot_id)
    assert changed.batches == ()
    changed = state.model_copy(update={"region": value("서울")})
    with pytest.raises(ValueError):
        _record_display(changed, "one", chosen, evaluated.snapshot_id)
    assert changed.batches == ()
    displayed = _record_display(state, "one", chosen, evaluated.snapshot_id)
    assert _record_display(displayed, "one", chosen, evaluated.snapshot_id) == displayed
    with pytest.raises(ValueError):
        _record_display(displayed, "one", chosen, "different")


@pytest.mark.parametrize(
    "name, field",
    [
        ("form", InputField(status="unknown")),
        ("contamination", ContaminationField(status="unknown")),
        ("quantity", QuantityField(status="unknown")),
    ],
)
def test_initial_unknown_baseline_and_new_evidence(pp, name, field):
    state = pp.model_copy(update={name: field})
    assert name not in [c.id for c in evaluate(state).candidates]
    assert field.answered_revision == 0
    updated = field.model_copy(update={"revision": 1, "reask_reason": "새 기록"})
    state = state.model_copy(update={name: updated})
    assert name in [c.id for c in evaluate(state).candidates]


@pytest.mark.parametrize("name", ["text", "why"])
def test_blank_selection_cannot_consume_batch(pp, name):
    state = pp.model_copy(update={"form": InputField()})
    result = evaluate(state)
    chosen = selections(result)
    invalid = (chosen[0].model_copy(update={name: "  \t"}),)
    with pytest.raises(ValidationError):
        _record_display(state, "one", invalid, result.snapshot_id)
    assert state.batches == ()
    with pytest.raises(ValidationError):
        Selection(id="form", text=" ", why="이유", kind="text")


def test_quantity_whitespace_and_trim():
    for name in ("unit", "period"):
        kwargs = {"amount": 1, "unit": "kg", "period": "월", "basis": "measured"}
        kwargs[name] = " \t "
        with pytest.raises(ValidationError):
            Quantity(**kwargs)
    quantity = Quantity(amount=1, unit=" kg ", period=" 월 ", basis="measured")
    assert (quantity.unit, quantity.period) == ("kg", "월")


def test_two_batches_complete_vs_limit_and_unconfirmed(pp):
    state = pp.model_copy(update={"form": InputField(), "region": InputField()})
    first = evaluate(state)
    choice = selections(first, 1)
    state = _record_display(state, "one", choice, first.snapshot_id)
    state = resolve_question(state, "one", "form", value("가공 전"))
    second = evaluate(state)
    state = _record_display(state, "two", selections(second), second.snapshot_id)
    state = resolve_question(state, "two", "region", value("부산"))
    assert evaluate(state).status == "complete"
    state = pp.model_copy(
        update={
            "form": InputField(),
            "region": InputField(),
            "quantity": QuantityField(status="unknown"),
        }
    )
    first = evaluate(state)
    state = _record_display(state, "one", selections(first, 1), first.snapshot_id)
    state = resolve_question(state, "one", "form", skip=True)
    second = evaluate(state)
    state = _record_display(state, "two", selections(second), second.snapshot_id)
    state = resolve_question(state, "two", "region", skip=True)
    result = evaluate(state)
    assert result.status == "complete"
    assert set(result.unconfirmed) == {"form", "region", "quantity"}


def test_followup_form_unknown_uses_same_baseline(pp):
    state = pp.model_copy(update={"form": InputField()})
    result = evaluate(state)
    state = _record_display(state, "one", selections(result), result.snapshot_id)
    state = resolve_question(state, "one", "form", value(" 모름 "))
    assert state.form.status == "unknown"
    assert state.form.value is None
    assert state.form.answered_revision == 0
    assert evaluate(state).status == "complete"
    changed = state.form.model_copy(update={"revision": 1, "reask_reason": "새 기록"})
    state = state.model_copy(update={"form": changed})
    assert [c.id for c in evaluate(state).candidates] == ["form"]
