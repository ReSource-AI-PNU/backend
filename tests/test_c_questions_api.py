"""저장 없는 C API: 모든 테스트는 Azure 연결 없이 실행한다."""

import pytest
from fastapi.testclient import TestClient

from app.api import c_questions
from app.main import app

PREFIX = "/api/v1/c/questions"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(c_questions.settings, "openai_endpoint", "")
    monkeypatch.setattr(c_questions.settings, "chat_deployment", "")
    monkeypatch.setattr(c_questions.settings, "foundry_key", "")
    return TestClient(app)


def post(client, action, body, status=200):
    response = client.post(f"{PREFIX}/{action}", json=body)
    assert response.status_code == status, response.text
    return response.json()


def choose(client, state):
    return post(client, "select", {"state": state})


def show(client, state, selected, batch):
    return post(
        client,
        "display",
        {
            "state": state,
            "selected": selected["selected"],
            "snapshotId": selected["snapshotId"],
            "batchId": batch,
        },
    )


def test_end_to_end_and_idempotency(client):
    state = {"material": {"status": "value", "value": "PP"}}
    initial = post(client, "evaluate", {"state": state})
    assert initial["status"] == "questions"
    selected = choose(client, state)
    assert selected["source"] == "rule_fallback"
    assert selected["fallbackReason"] == "missing_config"
    assert selected["snapshotId"] == initial["snapshotId"]
    assert state == {"material": {"status": "value", "value": "PP"}}
    assert post(client, "evaluate", {"state": state}) == initial
    shown = show(client, state, selected, "batch1")
    current = shown["state"]
    assert len(current["batches"]) == 1
    assert shown["evaluation"]["status"] == "awaiting_answers"
    retry = show(client, current, selected, "batch1")
    assert retry["state"] == current
    post(
        client,
        "display",
        {
            "state": current,
            "selected": selected["selected"][:1],
            "snapshotId": selected["snapshotId"],
            "batchId": "batch1",
        },
        409,
    )
    for index, q in enumerate(selected["selected"]):
        payload = {"state": current, "batchId": "batch1", "questionId": q["id"]}
        if q["id"] == "material_basis":
            payload["answer"] = {"status": "unknown"}
        else:
            payload["skip"] = True
        result = post(client, "answer", payload)
        current = result["state"]
        if index < len(selected["selected"]) - 1:
            pending = choose(client, current)
            assert pending["evaluation"]["status"] == "awaiting_answers"
            assert pending["source"] == "not_selected"
            post(
                client,
                "display",
                {
                    "state": current,
                    "batchId": "early",
                    "selected": selected["selected"],
                    "snapshotId": selected["snapshotId"],
                },
                409,
            )
    assert current["materialBasis"]["status"] == "unknown"
    assert any(q["outcome"] == "skipped" for q in current["batches"][0]["questions"])
    assert {"material_basis", "form"} <= set(result["evaluation"]["unconfirmed"])
    post(
        client,
        "answer",
        {"state": current, "batchId": "batch1", "questionId": "form", "skip": True},
        409,
    )
    second = choose(client, current)
    shown = show(client, current, second, "batch2")
    current = shown["state"]
    for q in second["selected"]:
        current = post(
            client,
            "answer",
            {"state": current, "batchId": "batch2", "questionId": q["id"], "skip": True},
        )["state"]
    assert post(client, "evaluate", {"state": current})["status"] == "complete"


def test_stale_and_invalid_inputs(client):
    state = {"material": {"status": "value", "value": "PP"}}
    selected = choose(client, state)
    changed = {**state, "form": {"revision": 1}}
    post(
        client,
        "display",
        {
            "state": changed,
            "batchId": "one",
            "selected": selected["selected"],
            "snapshotId": selected["snapshotId"],
        },
        409,
    )
    shown = show(client, state, selected, "one")
    current = shown["state"]
    qid = selected["selected"][0]["id"]
    post(
        client,
        "answer",
        {
            "state": current,
            "batchId": "one",
            "questionId": qid,
            "answer": {"status": "value", "value": "기록", "revision": 1},
        },
        409,
    )
    post(client, "evaluate", {"state": {"form": {"status": "value", "value": "가짜"}}}, 422)
    invalid = [dict(q) for q in selected["selected"]]
    invalid[0]["text"] = " "
    post(
        client,
        "display",
        {
            "state": state,
            "batchId": "one",
            "selected": invalid,
            "snapshotId": selected["snapshotId"],
        },
        422,
    )
    invalid = [dict(q) for q in selected["selected"]]
    invalid[0]["options"] = ["변조"]
    post(
        client,
        "display",
        {
            "state": state,
            "batchId": "one",
            "selected": invalid,
            "snapshotId": selected["snapshotId"],
        },
        422,
    )


def test_no_call_on_complete_unsupported_and_pending(client, monkeypatch):
    from app.agents import pp_question_selector

    def fail(**kwargs):
        pytest.fail("unexpected Azure call")

    monkeypatch.setattr(pp_question_selector, "OpenAI", fail)
    for name in ("openai_endpoint", "chat_deployment", "foundry_key"):
        monkeypatch.setattr(c_questions.settings, name, "test")
    unknown = {"status": "unknown"}
    state = {
        key: unknown
        for key in (
            "material",
            "materialBasis",
            "form",
            "contamination",
            "process",
            "region",
            "quantity",
        )
    }
    for value, expected in (
        (state, "complete"),
        ({"material": {"status": "value", "value": "PE"}}, "unsupported"),
    ):
        result = choose(client, value)
        assert result["evaluation"]["status"] == expected
        assert result["source"] == "not_selected"
    monkeypatch.setattr(c_questions.settings, "foundry_key", "")
    partial = {"material": {"status": "value", "value": "PP"}}
    selected = choose(client, partial)
    pending = show(client, partial, selected, "one")["state"]
    monkeypatch.setattr(c_questions.settings, "foundry_key", "test")
    assert choose(client, pending)["evaluation"]["status"] == "awaiting_answers"


def test_limit_reached_with_remaining_candidates(client):
    state = {}
    for batch in ("one", "two"):
        selected = choose(client, state)
        state = show(client, state, selected, batch)["state"]
        for question in selected["selected"]:
            state = post(
                client,
                "answer",
                {"state": state, "batchId": batch, "questionId": question["id"], "skip": True},
            )["state"]
    result = post(client, "evaluate", {"state": state})
    assert result["status"] == "limit_reached"
    assert "quantity" in result["unconfirmed"]
    assert choose(client, state)["source"] == "not_selected"


def test_azure_service_error_returns_fallback(client, monkeypatch):
    from app.agents import pp_question_selector

    for name in ("openai_endpoint", "chat_deployment", "foundry_key"):
        monkeypatch.setattr(c_questions.settings, name, "test")

    class Client:
        def __init__(self, **kwargs):
            self.responses = self

        def create(self, **kwargs):
            import httpx2

            request = httpx2.Request("POST", "https://example.invalid")
            raise pp_question_selector.APIStatusError(
                "private", response=httpx2.Response(503, request=request), body=None
            )

    monkeypatch.setattr(pp_question_selector, "OpenAI", Client)
    result = choose(client, {})
    assert result["source"] == "rule_fallback"
    assert result["fallbackReason"] == "service_error"
    assert len(result["selected"]) == 3


def test_mock_azure_selection_uses_server_settings(client, monkeypatch):
    import json
    from types import SimpleNamespace

    from app.agents import pp_question_selector
    from app.agents.pp_questions import ReviewState, evaluate

    for name, value in (
        ("openai_endpoint", "https://example.invalid/openai/v1"),
        ("chat_deployment", "test"),
        ("foundry_key", "private-key"),
    ):
        monkeypatch.setattr(c_questions.settings, name, value)
    seen = {}
    candidates = evaluate(ReviewState()).candidates[:3]
    data = {
        "questions": [
            {"id": c.id, "text": c.text, "why": c.why, "kind": c.kind, "options": c.options}
            for c in candidates
        ]
    }

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.responses = self

        def create(self, **kwargs):
            return SimpleNamespace(status="completed", output=(), output_text=json.dumps(data))

    monkeypatch.setattr(pp_question_selector, "OpenAI", Client)
    result = choose(client, {})
    assert result["source"] == "azure" and len(result["selected"]) == 3
    assert result["snapshotId"] == result["evaluation"]["snapshotId"]
    assert seen["api_key"] == "private-key"
    assert "private-key" not in json.dumps(result)
