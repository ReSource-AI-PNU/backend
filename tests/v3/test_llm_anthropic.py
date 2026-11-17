"""AnthropicChat: 보내는 모양과 받는 모양을 가짜 transport로 확인한다. 실제 호출은 하지 않는다."""

import json

import pytest

from app.v3.agents.llm import AnthropicChat, LLMError

SCHEMA = {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}
TOOL = {"name": "judge_route", "description": "경로 판정", "input_schema": {"type": "object"}}


def fake(reply: dict, status: int = 200):
    sent: dict = {}

    def transport(method, url, headers, body, timeout):
        sent.update(method=method, url=url, headers=headers, payload=json.loads(body))
        return status, {}, json.dumps(reply).encode("utf-8")

    return transport, sent


def test_final_tool_input_becomes_json_content_without_forcing_tool_use():
    reply = {
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "final_response", "input": {"type": "offer"}}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }
    transport, sent = fake(reply)
    llm = AnthropicChat("k-test", "claude-test", transport)
    out = llm.complete(
        [{"role": "system", "content": "규칙"}, {"role": "user", "content": "{}"}],
        tools=[TOOL],
        response_schema=SCHEMA,
    )
    assert json.loads(out["content"]) == {"type": "offer"} and out["tool_calls"] == []
    assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    payload = sent["payload"]
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "k-test" and "anthropic-version" in sent["headers"]
    assert payload["model"] == "claude-test" and "tool_choice" not in payload
    assert [t["name"] for t in payload["tools"]] == ["judge_route", "final_response"]
    assert payload["tools"][-1]["input_schema"] == SCHEMA
    assert payload["system"].startswith("규칙") and "final_response" in payload["system"]
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "{}"}]}]


def test_judge_tool_calls_come_first_and_final_is_held_back():
    reply = {
        "content": [
            {"type": "text", "text": "확인합니다"},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "judge_route",
                "input": {"streamId": "st_a"},
            },
            {
                "type": "tool_use",
                "id": "tu_2",
                "name": "final_response",
                "input": {"type": "offer"},
            },
        ]
    }
    transport, _ = fake(reply)
    out = AnthropicChat("k", "m", transport).complete(
        [{"role": "user", "content": "{}"}], tools=[TOOL], response_schema=SCHEMA
    )
    assert out["tool_calls"] == [
        {"id": "tu_1", "name": "judge_route", "arguments": {"streamId": "st_a"}}
    ]
    assert out["content"] == "확인합니다"


def test_tool_results_and_next_user_text_share_one_user_message():
    transport, sent = fake({"content": [{"type": "text", "text": "{}"}]})
    AnthropicChat("k", "m", transport).complete(
        [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "u1"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tu_1", "name": "judge_route", "arguments": {"a": 1}}],
            },
            {"role": "tool", "tool_call_id": "tu_1", "name": "judge_route", "content": "{}"},
            {"role": "user", "content": "u2"},
        ]
    )
    messages = sent["payload"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == [
        {"type": "tool_use", "id": "tu_1", "name": "judge_route", "input": {"a": 1}}
    ]
    assert [b["type"] for b in messages[2]["content"]] == ["tool_result", "text"]
    assert messages[2]["content"][0]["tool_use_id"] == "tu_1"
    assert "tool_choice" not in sent["payload"] and "tools" not in sent["payload"]


def test_errors_do_not_leak_the_key_and_truncation_is_an_error():
    transport, _ = fake(
        {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}, 401
    )
    with pytest.raises(LLMError) as caught:
        AnthropicChat("k-secret-value", "m", transport).complete(
            [{"role": "user", "content": "{}"}]
        )
    assert caught.value.status == 401 and "k-secret-value" not in str(caught.value)
    assert "k-secret-value" not in repr(AnthropicChat("k-secret-value", "m"))
    transport, _ = fake(
        {"content": [{"type": "text", "text": "잘린"}], "stop_reason": "max_tokens"}
    )
    with pytest.raises(LLMError):
        AnthropicChat("k", "m", transport).complete([{"role": "user", "content": "{}"}])
