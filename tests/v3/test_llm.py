import json

import pytest

from app.v3.agents.llm import (
    FoundryChat,
    FoundryEmbeddings,
    LLMError,
    OllamaChat,
    OllamaEmbeddings,
    ScriptedLLM,
)

TOOLS = [
    {
        "name": "judge_route",
        "description": "경로 판정",
        "input_schema": {"type": "object", "properties": {"streamId": {"type": "string"}}},
    }
]
SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
HISTORY = [
    {"role": "system", "content": "규칙"},
    {"role": "user", "content": "판정해 줘"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "name": "judge_route", "arguments": {"streamId": "st_sprue"}}
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "name": "judge_route", "content": "{}"},
]


class Fake:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.requests = payload, status, []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": json.loads(body),
                "timeout": timeout,
            }
        )
        return self.status, {}, json.dumps(self.payload, ensure_ascii=False).encode()


def test_foundry_request_and_tool_call_parsing():
    fake = Fake(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {
                                    "name": "judge_route",
                                    "arguments": '{"streamId": "st_film"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 12},
        }
    )
    llm = FoundryChat("https://aif.example.com/", "gpt-5-mini", "k-secret", transport=fake)
    out = llm.complete(HISTORY, tools=TOOLS, response_schema=SCHEMA)
    req = fake.requests[0]
    assert req["url"] == (
        "https://aif.example.com/openai/deployments/gpt-5-mini/chat/completions"
        "?api-version=2024-10-21"
    )
    assert req["headers"]["api-key"] == "k-secret" and req["method"] == "POST"
    body = req["body"]
    assert "max_completion_tokens" in body and "max_tokens" not in body
    assert body["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert (
        body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"streamId": "st_sprue"}'
    )
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "{}"}
    assert out == {
        "content": None,
        "usage": {"total_tokens": 12},
        "tool_calls": [
            {"id": "call_9", "name": "judge_route", "arguments": {"streamId": "st_film"}}
        ],
    }
    assert "k-secret" not in repr(llm)


def test_foundry_error_hides_key():
    fake = Fake({"error": {"code": "429", "message": "rate limit"}}, status=429)
    llm = FoundryChat("https://aif.example.com", "gpt-5-mini", "k-secret", transport=fake)
    with pytest.raises(LLMError) as caught:
        llm.complete([{"role": "user", "content": "안녕"}])
    assert caught.value.status == 429 and "k-secret" not in str(caught.value)
    with pytest.raises(LLMError):
        FoundryChat("https://x", "d", "k", transport=Fake({"choices": []})).complete([])


def test_ollama_request_and_parsing():
    fake = Fake(
        {
            "message": {
                "role": "assistant",
                "content": '{"text": "네"}',
                "tool_calls": [
                    {"function": {"name": "judge_route", "arguments": {"streamId": "st_purge"}}}
                ],
            },
            "prompt_eval_count": 5,
            "eval_count": 7,
        }
    )
    llm = OllamaChat("http://10.60.2.4:11434/", "qwen2.5:7b", transport=fake)
    out = llm.complete(HISTORY, tools=TOOLS, response_schema=SCHEMA)
    req = fake.requests[0]
    assert req["url"] == "http://10.60.2.4:11434/api/chat"
    assert req["body"]["model"] == "qwen2.5:7b" and req["body"]["stream"] is False
    assert req["body"]["format"] == SCHEMA and req["body"]["tools"][0]["type"] == "function"
    wired = req["body"]["messages"]
    assert wired[2]["tool_calls"][0]["function"]["arguments"] == {"streamId": "st_sprue"}
    assert wired[3] == {"role": "tool", "content": "{}", "tool_name": "judge_route"}
    assert out["content"] == '{"text": "네"}'
    assert out["tool_calls"] == [
        {"id": "call_1", "name": "judge_route", "arguments": {"streamId": "st_purge"}}
    ]
    assert out["usage"] == {"prompt_tokens": 5, "completion_tokens": 7}
    assert (llm.provider, FoundryChat.provider) == ("ollama", "foundry")


def test_scripted_llm_records_calls():
    llm = ScriptedLLM(
        [
            {"tool_calls": [{"name": "judge_route", "arguments": {"streamId": "st_sprue"}}]},
            {"text": "완료"},
            lambda messages, tools, schema: f"메시지 {len(messages)}개",
        ]
    )
    first = llm.complete([{"role": "user", "content": "a"}], tools=TOOLS)
    assert first["content"] is None and first["tool_calls"][0]["id"] == "call_1"
    assert json.loads(llm.complete([], response_schema=SCHEMA)["content"]) == {"text": "완료"}
    assert llm.complete([{"role": "user", "content": "b"}])["content"] == "메시지 1개"
    assert [c["tools"] for c in llm.calls] == [["judge_route"], [], []]
    assert llm.calls[1]["response_schema"] == SCHEMA and llm.remaining == 0
    with pytest.raises(LLMError):
        llm.complete([])


def test_embeddings():
    fake = Fake({"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    emb = OllamaEmbeddings("http://10.60.2.4:11434", transport=fake)
    assert emb.embed(["가", "나"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert fake.requests[0]["url"].endswith("/api/embed")
    assert fake.requests[0]["body"] == {"model": "bge-m3", "input": ["가", "나"]}
    assert emb.embed([]) == [] and len(fake.requests) == 1
    with pytest.raises(LLMError):
        OllamaEmbeddings("http://x", transport=Fake({"embeddings": [[0.1]]})).embed(["가", "나"])
    azure = Fake({"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]})
    out = FoundryEmbeddings("https://aif.example.com", "emb", "k", transport=azure).embed(
        ["a", "b"]
    )
    assert out == [[1.0], [2.0]] and "/deployments/emb/embeddings" in azure.requests[0]["url"]
