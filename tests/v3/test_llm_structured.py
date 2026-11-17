"""모델 호출 계층의 구조화 출력, 추론 강도, 길이 제한 처리. 가짜 transport로만 확인한다(네트워크 없음)."""

import json
import logging

import pytest

from app.v3.agents.factory import (
    DEFAULT_OLLAMA_NUM_CTX,
    DEFAULT_REASONING_EFFORT,
    LlmConfigError,
    build_llm_factory,
    reasoning_effort,
)
from app.v3.agents.llm import FoundryChat, LLMError, NotStrict, OllamaChat, strict_schema

KEY = "not-a-real-key"
FOUNDRY = {
    "FOUNDRY_ENDPOINT": "https://example.invalid",
    "FOUNDRY_DEPLOYMENT": "gpt-5-mini",
    "FOUNDRY_API_KEY": KEY,
}
TURN = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["offer", "conditional"]},
        "stage": {"type": "integer", "minimum": 1, "maximum": 6},
        "text": {"type": "string"},
        "terms": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"streamId": {"type": "string"}},
                        "required": ["streamId"],
                    },
                },
                "pickupsPerMonthRef": {"type": "string", "enum": ["load_pickups"]},
            },
            "required": ["items"],
        },
        "reason": {"type": "string"},
    },
    "required": ["type", "stage", "text"],
}


class Fake:
    def __init__(self, *payloads):
        self.payloads, self.requests = list(payloads), []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append(json.loads(body))
        return 200, {}, json.dumps(self.payloads.pop(0), ensure_ascii=False).encode()


def foundry_reply(content, finish="stop", **message):
    return {
        "choices": [{"message": {"content": content, **message}, "finish_reason": finish}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 4000}},
    }


def test_strict_schema_requires_every_key_and_makes_optional_ones_nullable():
    strict = strict_schema(TURN)
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["type", "stage", "text", "terms", "reason"]
    props = strict["properties"]
    assert props["type"] == TURN["properties"]["type"]  # 필수 키는 그대로
    assert props["stage"] == {"type": "integer"}  # 엄격 모드가 받지 않는 범위 키워드는 뺀다
    assert props["reason"] == {"type": ["string", "null"]}
    terms = props["terms"]["anyOf"]
    assert terms[1] == {"type": "null"} and terms[0]["additionalProperties"] is False
    assert terms[0]["required"] == ["items", "pickupsPerMonthRef"]
    ref = terms[0]["properties"]["pickupsPerMonthRef"]
    assert ref == {"type": ["string", "null"], "enum": ["load_pickups", None]}
    item = terms[0]["properties"]["items"]["items"]
    assert item["additionalProperties"] is False and item["required"] == ["streamId"]
    assert TURN["properties"]["stage"]["minimum"] == 1  # 원본은 바꾸지 않는다


def test_strict_schema_refuses_free_form_objects():
    loose = {"type": "object", "properties": {"arguments": {"type": "object"}}}
    with pytest.raises(NotStrict):
        strict_schema(loose)
    with pytest.raises(NotStrict):
        strict_schema({"type": "object", "properties": {"ids": {"type": "array"}}})


def test_foundry_sends_strict_schema_and_reasoning_effort():
    fake = Fake(foundry_reply('{"type": "offer", "stage": 5, "text": "", "terms": null}'))
    llm = FoundryChat(
        "https://aif.example.com", "gpt-5-mini", KEY, transport=fake, reasoning_effort="low"
    )
    out = llm.complete([{"role": "user", "content": "{}"}], response_schema=TURN)
    body = fake.requests[0]
    assert body["reasoning_effort"] == "low" and "tools" not in body
    sent = body["response_format"]["json_schema"]
    assert sent["strict"] is True and sent["schema"] == strict_schema(TURN)
    assert json.loads(out["content"])["terms"] is None


def test_foundry_falls_back_to_non_strict_for_free_form_schemas():
    loose = {"type": "object", "properties": {"arguments": {"type": "object"}}}
    fake = Fake(foundry_reply("{}"))
    FoundryChat("https://x", "d", KEY, transport=fake).complete([], response_schema=loose)
    sent = fake.requests[0]["response_format"]["json_schema"]
    assert sent["strict"] is False and sent["schema"] == loose
    fake = Fake(foundry_reply("{}"))
    FoundryChat("https://x", "d", KEY, transport=fake, strict=False).complete(
        [], response_schema=TURN
    )
    assert fake.requests[0]["response_format"]["json_schema"]["strict"] is False


def test_foundry_truncation_and_refusal_are_model_errors_not_empty_text():
    # 추론에 토큰을 다 써서 본문이 비어 돌아온 경우(배포 서버에서 본 INVALID_OUTPUT의 원인)
    fake = Fake(foundry_reply("", finish="length"))
    with pytest.raises(LLMError, match="길이 제한"):
        FoundryChat("https://x", "d", KEY, transport=fake).complete([])
    fake = Fake(foundry_reply(None, refusal="거절"))
    with pytest.raises(LLMError, match="거절"):
        FoundryChat("https://x", "d", KEY, transport=fake).complete([])
    with pytest.raises(ValueError):
        FoundryChat("https://x", "d", KEY, reasoning_effort="extreme")


def test_ollama_sends_schema_as_format_with_a_long_enough_context():
    fake = Fake({"message": {"role": "assistant", "content": '{"type": "offer"}'}, "done": True})
    llm = OllamaChat("http://10.60.2.4:11434", "qwen2.5:7b", transport=fake)
    out = llm.complete([{"role": "user", "content": "{}"}], response_schema=TURN)
    body = fake.requests[0]
    assert body["format"] == TURN and "tools" not in body
    assert body["options"] == {"temperature": 0, "num_ctx": 16384, "num_predict": 2048}
    assert out["content"] == '{"type": "offer"}'
    fake = Fake({"message": {"role": "assistant", "content": "{"}, "done_reason": "length"})
    with pytest.raises(LLMError, match="길이 제한"):
        OllamaChat("http://x", "m", transport=fake).complete([], response_schema=TURN)
    fake = Fake({"message": {"role": "assistant", "content": "{}"}})
    OllamaChat("http://x", "m", transport=fake, num_ctx=None, num_predict=None).complete([])
    assert fake.requests[0]["options"] == {"temperature": 0}


def test_factory_reads_reasoning_effort_and_limits():
    assert DEFAULT_REASONING_EFFORT == "low"
    assert (
        reasoning_effort({}) == "low"
        and reasoning_effort({"FOUNDRY_REASONING_EFFORT": "none"}) is None
    )
    assert reasoning_effort({"FOUNDRY_REASONING_EFFORT": " Minimal "}) == "minimal"
    chat = build_llm_factory("foundry", FOUNDRY, strict=True)("company", "org_hanbit", None)
    assert (chat.reasoning_effort, chat.max_completion_tokens) == ("low", 4000)
    env = {
        **FOUNDRY,
        "FOUNDRY_REASONING_EFFORT": "minimal",
        "FOUNDRY_MAX_COMPLETION_TOKENS": "2500",
    }
    chat = build_llm_factory("foundry", env, strict=True)("company", "org_hanbit", None)
    assert (chat.reasoning_effort, chat.max_completion_tokens) == ("minimal", 2500)
    for bad in ({"FOUNDRY_REASONING_EFFORT": "max"}, {"FOUNDRY_MAX_COMPLETION_TOKENS": "-1"}):
        with pytest.raises(LlmConfigError):
            build_llm_factory("foundry", {**FOUNDRY, **bad}, strict=True)


def test_factory_lenient_keeps_defaults_and_sets_ollama_context(caplog):
    env = {
        **FOUNDRY,
        "FOUNDRY_REASONING_EFFORT": "max",
        "OLLAMA_BASE_URL": "http://10.0.0.4:11434",
        "OLLAMA_NUM_CTX": "abc",
    }
    with caplog.at_level(logging.WARNING):
        factory = build_llm_factory("foundry", env)
    assert "FOUNDRY_REASONING_EFFORT" in caplog.text and "OLLAMA_NUM_CTX" in caplog.text
    assert KEY not in caplog.text
    assert factory("company", "org_hanbit", None).reasoning_effort == DEFAULT_REASONING_EFFORT
    ollama = factory("vendor", "org_namhae", {"provider": "ollama", "name": "qwen2.5:7b"})
    assert isinstance(ollama, OllamaChat) and ollama.num_ctx == DEFAULT_OLLAMA_NUM_CTX
    env = {**FOUNDRY, "OLLAMA_BASE_URL": "http://10.0.0.4:11434", "OLLAMA_NUM_CTX": "32768"}
    ollama = build_llm_factory("foundry", env, strict=True)(
        "vendor", "org_namhae", {"provider": "ollama", "name": "qwen2.5:7b"}
    )
    assert ollama.num_ctx == 32768
