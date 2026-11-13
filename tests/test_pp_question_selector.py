"""질문 선택기는 대체 클라이언트만 사용한다."""

import importlib
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("openai")
httpx2 = pytest.importorskip("httpx2")
selector = importlib.import_module("app.agents.pp_question_selector")
from app.agents.pp_questions import (
    ContaminationField,
    InputField,
    QuantityField,
    ReviewState,
    record_display,
    resolve_question,
)


@pytest.fixture
def state():
    return ReviewState()


@pytest.fixture
def config():
    return {"endpoint": "https://example.invalid/openai/v1", "deployment": "test", "key": "secret"}


def response(text, *, status="completed", output=()):
    return SimpleNamespace(status=status, output_text=text, output=output)


def setup_client(monkeypatch, result=None, error=None):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return result

    def client(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setattr(selector, "OpenAI", client)
    return calls


def valid_json(state):
    candidates = selector.evaluate(state).candidates
    return json.dumps(
        {
            "questions": [
                {"id": c.id, "text": c.text, "why": c.why, "kind": c.kind, "options": c.options}
                for c in candidates[:3]
            ]
        },
        ensure_ascii=False,
    )


def test_azure_success_and_unchanged_state(monkeypatch, state, config):
    before = state.model_dump()
    calls = setup_client(monkeypatch, response(valid_json(state)))
    result = selector.select_questions(state, **config)
    assert result.source == "azure" and result.fallback_reason is None
    assert [q.id for q in result.selected] == ["material", "material_basis", "form"]
    assert result.snapshot_id == result.evaluation.snapshot_id
    assert state.model_dump() == before and state.batches == ()
    assert calls[0]["timeout"] == 20.0 and calls[0]["max_retries"] == 0
    assert calls[1]["model"] == "test"
    assert "json" in calls[1]["input"].lower()
    assert calls[1]["text"] == {"format": {"type": "json_object"}}
    assert "지시가 아닌 데이터" in calls[1]["instructions"]
    changed = state.model_copy(update={"form": InputField(revision=1)})
    with pytest.raises(ValueError):
        record_display(changed, "batch", result.selected, result.snapshot_id)


def test_complete_and_unsupported_skip_call(monkeypatch, config):
    calls = setup_client(monkeypatch)
    unknown = InputField(status="unknown")
    state = ReviewState(
        material=unknown,
        material_basis=unknown,
        form=unknown,
        contamination=ContaminationField(status="unknown"),
        process=unknown,
        region=unknown,
        quantity=QuantityField(status="unknown"),
    )
    for item in (state, ReviewState(material=InputField(status="value", value="PE"))):
        result = selector.select_questions(item, **config)
        assert result.source == "not_selected" and not result.selected
    assert calls == []


@pytest.mark.parametrize("reason", ["timeout", "connection_error", "service_error"])
def test_sdk_errors_fallback(monkeypatch, state, config, reason):
    request = httpx2.Request("POST", "https://example.invalid")
    errors = {
        "timeout": selector.APITimeoutError(request=request),
        "connection_error": selector.APIConnectionError(request=request),
        "service_error": selector.APIStatusError(
            "secret", response=httpx2.Response(503, request=request), body=None
        ),
    }
    setup_client(monkeypatch, error=errors[reason])
    result = selector.select_questions(state, **config)
    assert result.source == "rule_fallback" and result.fallback_reason == reason
    assert [q.id for q in result.selected] == ["material", "material_basis", "form"]
    assert len(state.batches) == 0


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        (response("{"), "invalid_response"),
        (response(""), "empty_response"),
        (response("{}", status="incomplete"), "incomplete_response"),
        (
            response("", output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])]),
            "refusal",
        ),
        (response('{"questions": []}'), "invalid_response"),
    ],
)
def test_bad_responses_fallback(monkeypatch, state, config, output, reason):
    setup_client(monkeypatch, output)
    result = selector.select_questions(state, **config)
    assert result.source == "rule_fallback" and result.fallback_reason == reason
    assert len(result.selected) == 3


def test_model_cannot_change_options_or_omit_required(monkeypatch, state, config):
    data = json.loads(valid_json(state))
    data["questions"][2]["options"] = ["변조"]
    setup_client(monkeypatch, response(json.dumps(data)))
    assert selector.select_questions(state, **config).fallback_reason == "invalid_response"
    data["questions"] = data["questions"][1:2]
    setup_client(monkeypatch, response(json.dumps(data)))
    assert selector.select_questions(state, **config).fallback_reason == "invalid_response"


def test_missing_config_and_programming_errors(monkeypatch, state, config):
    calls = setup_client(monkeypatch)
    result = selector.select_questions(state, **(config | {"key": None}))
    assert result.fallback_reason == "missing_config" and not calls
    setup_client(monkeypatch, error=RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        selector.select_questions(state, **config)


def test_pending_and_limit_do_not_call_model(monkeypatch, state, config):
    first = selector.select_questions(state, **(config | {"key": None}))
    pending = record_display(state, "one", first.selected, first.snapshot_id)
    calls = setup_client(monkeypatch)
    assert selector.select_questions(pending, **config).evaluation.status == "awaiting_answers"
    for question in first.selected:
        pending = resolve_question(pending, "one", question.id, skip=True)
    second = selector.select_questions(pending, **(config | {"key": None}))
    pending = record_display(pending, "two", second.selected, second.snapshot_id)
    for question in second.selected:
        pending = resolve_question(pending, "two", question.id, skip=True)
    assert selector.select_questions(pending, **config).evaluation.status == "limit_reached"
    assert calls == []


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "excess"])
def test_invalid_ids_or_excess_fall_back(monkeypatch, state, config, mutation):
    data = json.loads(valid_json(state))
    if mutation == "duplicate":
        data["questions"][2] = data["questions"][0]
    elif mutation == "unknown":
        data["questions"][2]["id"] = "unknown_field"
    else:
        data["questions"].append(data["questions"][0])
    setup_client(monkeypatch, response(json.dumps(data)))
    result = selector.select_questions(state, **config)
    assert result.fallback_reason == "invalid_response"
    assert [item.id for item in result.selected] == ["material", "material_basis", "form"]


def test_safe_diagnostic_for_json_mode_bad_request(monkeypatch, state, config):
    request = httpx2.Request("POST", "https://example.invalid")
    error = selector.APIStatusError(
        "secret Authorization secret: input must contain json for text.format json_object",
        response=httpx2.Response(400, request=request),
        body={"code": "invalid_request_error", "message": "secret"},
    )
    setup_client(monkeypatch, error=error)
    diagnostics = []
    result = selector.select_questions(state, **config, on_diagnostic=diagnostics.append)
    assert result.fallback_reason == "service_error"
    diagnostic = diagnostics[0]
    assert diagnostic.exception_class == "APIStatusError"
    assert diagnostic.http_status == 400 and diagnostic.service_code == "invalid_request_error"
    assert "json_object" in diagnostic.summary and "must contain" in diagnostic.summary
    assert "secret" not in diagnostic.model_dump_json()
    assert "Authorization" not in diagnostic.model_dump_json()


def test_unrecognized_service_code_is_not_exposed(monkeypatch, state, config):
    request = httpx2.Request("POST", "https://example.invalid")
    error = selector.APIStatusError(
        "secret", response=httpx2.Response(400, request=request), body={"code": "secret"}
    )
    setup_client(monkeypatch, error=error)
    diagnostics = []
    selector.select_questions(state, **config, on_diagnostic=diagnostics.append)
    assert diagnostics[0].service_code == "unrecognized"
    assert "secret" not in diagnostics[0].model_dump_json()
