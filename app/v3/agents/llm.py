"""LLM 클라이언트. 표준 라이브러리(urllib)만 쓰고, 테스트에서는 transport를 가짜로 바꾼다.

메시지는 제공자와 무관한 한 가지 모양으로 주고받는다.

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str | None, "tool_calls": [{"id", "name", "arguments": dict}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

도구는 {"name", "description", "input_schema"} 목록으로 준다(판단 도구의 ToolSpec과 같은 이름).
complete()는 {"content", "tool_calls": [{"id", "name", "arguments": dict}], "usage"}를 돌려준다.
API 키는 인자로만 받고, 예외 문구와 호출 기록에 넣지 않는다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

Transport = Callable[
    [str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]
]


class LLMError(RuntimeError):
    """모델 호출이 실패했거나 응답을 해석할 수 없다."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, dict[str, str], bytes]:
    if not url.startswith(("https://", "http://")):
        raise LLMError("http(s) 주소만 호출할 수 있다")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LLMError(f"모델 서버에 연결하지 못했다: {type(error).__name__}") from error


class LLM(Protocol):
    provider: str
    model: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class Embeddings(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _post_json(
    transport: Transport, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, _, raw = transport(
        "POST", url, {"Content-Type": "application/json", **headers}, body, timeout
    )
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LLMError("모델 응답이 JSON이 아니다", status) from error
    if status >= 400:
        error = data.get("error") if isinstance(data, dict) else None
        detail = error.get("message") if isinstance(error, dict) else error
        raise LLMError(f"모델 호출 실패({status}): {str(detail or '')[:300]}", status)
    if not isinstance(data, dict):
        raise LLMError("모델 응답 형식이 다르다", status)
    return data


def _function_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema")
                or tool.get("parameters")
                or {"type": "object", "properties": {}},
            },
        }
        for tool in tools or []
    ]


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise LLMError("도구 호출 인자가 JSON이 아니다") from error
    if not isinstance(parsed, dict):
        raise LLMError("도구 호출 인자는 객체여야 한다")
    return parsed


class FoundryChat:
    """Foundry(Azure OpenAI 호환) chat completions."""

    provider = "foundry"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        api_key: str,
        api_version: str = "2024-10-21",
        transport: Transport | None = None,
        *,
        max_completion_tokens: int = 4000,
        timeout: float = 60,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = deployment
        self.api_version = api_version
        self._api_key = api_key
        self._transport = transport or urllib_transport
        self.max_completion_tokens = max_completion_tokens
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"FoundryChat(endpoint={self.endpoint!r}, deployment={self.model!r})"

    @property
    def url(self) -> str:
        return (
            f"{self.endpoint}/openai/deployments/{self.model}/chat/completions"
            f"?api-version={self.api_version}"
        )

    @staticmethod
    def _wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wired = []
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                wired.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": json.dumps(
                                        call.get("arguments") or {}, ensure_ascii=False
                                    ),
                                },
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
            elif message.get("role") == "tool":
                wired.append(
                    {
                        "role": "tool",
                        "tool_call_id": message["tool_call_id"],
                        "content": message.get("content", ""),
                    }
                )
            else:
                wired.append({"role": message["role"], "content": message.get("content", "")})
        return wired

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": self._wire(messages),
            # gpt-5 계열은 max_tokens 대신 max_completion_tokens를 받는다
            "max_completion_tokens": self.max_completion_tokens,
        }
        if tools:
            payload["tools"] = _function_tools(tools)
            payload["tool_choice"] = "auto"
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": response_schema, "strict": False},
            }
        data = _post_json(
            self._transport, self.url, {"api-key": self._api_key}, payload, self.timeout
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMError("모델 응답에 choices가 없다") from error
        calls = [
            {
                "id": call.get("id") or f"call_{index}",
                "name": call["function"]["name"],
                "arguments": _arguments(call["function"].get("arguments")),
            }
            for index, call in enumerate(message.get("tool_calls") or [], 1)
        ]
        return {
            "content": message.get("content"),
            "tool_calls": calls,
            "usage": data.get("usage") or {},
        }


class OllamaChat:
    """Ollama /api/chat. VNet 안의 주소로만 부른다."""

    provider = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        transport: Transport | None = None,
        *,
        timeout: float = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport or urllib_transport
        self.timeout = timeout

    @staticmethod
    def _wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wired = []
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                wired.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": call["name"],
                                    "arguments": call.get("arguments") or {},
                                }
                            }
                            for call in message["tool_calls"]
                        ],
                    }
                )
            elif message.get("role") == "tool":
                wired.append(
                    {
                        "role": "tool",
                        "content": message.get("content", ""),
                        "tool_name": message.get("name", ""),
                    }
                )
            else:
                wired.append({"role": message["role"], "content": message.get("content", "")})
        return wired

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._wire(messages),
            "stream": False,
            "options": {"temperature": 0},
        }
        if tools:
            payload["tools"] = _function_tools(tools)
        if response_schema is not None:
            payload["format"] = response_schema
        data = _post_json(self._transport, f"{self.base_url}/api/chat", {}, payload, self.timeout)
        message = data.get("message")
        if not isinstance(message, dict):
            raise LLMError("모델 응답에 message가 없다")
        calls = [
            {
                "id": call.get("id") or f"call_{index}",
                "name": call["function"]["name"],
                "arguments": _arguments(call["function"].get("arguments")),
            }
            for index, call in enumerate(message.get("tool_calls") or [], 1)
        ]
        return {
            "content": message.get("content") or None,
            "tool_calls": calls,
            "usage": {
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
            },
        }


class AnthropicChat:
    """Anthropic Messages API(Claude). 설계의 기본 모델은 아니고, 실제 모델로 돌려 보는 용도다.

    - system 역할의 글은 system 인자로 모아 보낸다.
    - 도구 결과는 바로 앞 assistant의 tool_use 다음 user 메시지에 tool_result 블록으로 넣는다.
      이어지는 user 글은 같은 메시지에 붙인다(같은 역할이 연달아 나가지 않게 한다).
    - response_schema가 있으면 그 스키마를 입력으로 받는 도구(FINAL_TOOL)를 함께 준다. 모델이 그
      도구를 부르면 입력을 JSON 글로 바꿔 content로 돌려준다. 다른 도구를 함께 불렀으면 그 도구들이
      먼저다(최종 답은 다음 차례에 다시 낸다). 도구 사용을 강제하지는 않는다(tool_choice any/tool을
      받지 않는 모델이 있다). 모델이 글로 답하면 그 글이 content로 가고, JSON인지는 부른 쪽이 본다.
    """

    provider = "anthropic"
    FINAL_TOOL = "final_response"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        transport: Transport | None = None,
        *,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 4000,
        timeout: float = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._transport = transport or urllib_transport
        self.max_tokens = max_tokens
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"AnthropicChat(model={self.model!r})"

    @staticmethod
    def _wire(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system: list[str] = []
        wired: list[dict[str, Any]] = []

        def add(role: str, blocks: list[dict[str, Any]]) -> None:
            if not blocks:
                return
            if wired and wired[-1]["role"] == role:
                wired[-1]["content"].extend(blocks)
            else:
                wired.append({"role": role, "content": list(blocks)})

        for message in messages:
            role = message.get("role")
            text = message.get("content")
            if role == "system":
                if text:
                    system.append(str(text))
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if text:
                    blocks.append({"type": "text", "text": str(text)})
                for call in message.get("tool_calls") or []:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": call.get("arguments") or {},
                        }
                    )
                add("assistant", blocks)
            elif role == "tool":
                add(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": message["tool_call_id"],
                            "content": str(message.get("content", "")),
                        }
                    ],
                )
            else:
                add("user", [{"type": "text", "text": str(text or "")}])
        return "\n\n".join(system), wired

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        system, wired = self._wire(messages)
        specs = [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema")
                or tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
            for tool in tools or []
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": wired,
        }
        if response_schema is not None:
            specs.append(
                {
                    "name": self.FINAL_TOOL,
                    "description": "최종 답을 낸다. 다른 도구를 더 부를 일이 없을 때 이 도구로만 답한다.",
                    "input_schema": response_schema,
                }
            )
            system = (
                f"{system}\n\n최종 답은 글로 쓰지 말고 {self.FINAL_TOOL} 도구의 입력으로 내라."
            ).strip()
        if system:
            payload["system"] = system
        if specs:
            payload["tools"] = specs
        data = _post_json(
            self._transport,
            f"{self.base_url}/v1/messages",
            {"x-api-key": self._api_key, "anthropic-version": self.API_VERSION},
            payload,
            self.timeout,
        )
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise LLMError("모델 응답에 content가 없다")
        text = "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
        final: dict[str, Any] | None = None
        calls: list[dict[str, Any]] = []
        for index, block in enumerate(blocks, 1):
            if block.get("type") != "tool_use":
                continue
            arguments = _arguments(block.get("input"))
            if block.get("name") == self.FINAL_TOOL:
                final = arguments
            else:
                calls.append(
                    {
                        "id": block.get("id") or f"call_{index}",
                        "name": str(block.get("name")),
                        "arguments": arguments,
                    }
                )
        if data.get("stop_reason") == "max_tokens" and final is None and not calls:
            raise LLMError("모델 응답이 길이 제한에서 잘렸다")
        content: str | None = text or None
        if final is not None and not calls:
            content = json.dumps(final, ensure_ascii=False)
        usage = data.get("usage") or {}
        return {
            "content": content,
            "tool_calls": calls,
            "usage": {
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
            },
        }


class ScriptedLLM:
    """테스트용. 미리 정한 응답을 차례로 돌려주고 호출을 기록한다.

    응답은 문자열(content), dict({"content", "tool_calls"}), 또는
    (messages, tools, response_schema)를 받아 그 둘 중 하나를 돌려주는 함수다.
    """

    def __init__(
        self, responses: list[Any], *, provider: str = "scripted", model: str = "scripted"
    ) -> None:
        self.provider = provider
        self.model = model
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    @property
    def remaining(self) -> int:
        return len(self._responses)

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages, ensure_ascii=False, default=str)),
                "tools": [tool["name"] for tool in tools or []],
                "response_schema": response_schema,
            }
        )
        if not self._responses:
            raise LLMError("ScriptedLLM에 남은 응답이 없다")
        response = self._responses.pop(0)
        if callable(response):
            response = response(messages, tools, response_schema)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            response = {"content": response}
        elif not isinstance(response, dict) or not ({"content", "tool_calls"} & set(response)):
            response = {"content": json.dumps(response, ensure_ascii=False)}
        calls = [
            {
                "id": call.get("id") or f"call_{index}",
                "name": call["name"],
                "arguments": dict(call.get("arguments") or {}),
            }
            for index, call in enumerate(response.get("tool_calls") or [], 1)
        ]
        content = response.get("content")
        if content is not None and not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return {"content": content, "tool_calls": calls, "usage": response.get("usage") or {}}


class FoundryEmbeddings:
    def __init__(
        self,
        endpoint: str,
        deployment: str,
        api_key: str,
        api_version: str = "2024-10-21",
        transport: Transport | None = None,
        *,
        timeout: float = 60,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = deployment
        self.api_version = api_version
        self._api_key = api_key
        self._transport = transport or urllib_transport
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = (
            f"{self.endpoint}/openai/deployments/{self.model}/embeddings"
            f"?api-version={self.api_version}"
        )
        data = _post_json(
            self._transport, url, {"api-key": self._api_key}, {"input": list(texts)}, self.timeout
        )
        rows = sorted(data.get("data") or [], key=lambda row: row.get("index", 0))
        vectors = [row.get("embedding") for row in rows]
        return _checked_vectors(vectors, len(texts))


class OllamaEmbeddings:
    def __init__(
        self,
        base_url: str,
        model: str = "bge-m3",
        transport: Transport | None = None,
        *,
        timeout: float = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport or urllib_transport
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = _post_json(
            self._transport,
            f"{self.base_url}/api/embed",
            {},
            {"model": self.model, "input": list(texts)},
            self.timeout,
        )
        return _checked_vectors(data.get("embeddings"), len(texts))


def _checked_vectors(vectors: Any, expected: int) -> list[list[float]]:
    if (
        not isinstance(vectors, list)
        or len(vectors) != expected
        or any(not isinstance(v, list) or not v for v in vectors)
    ):
        raise LLMError("임베딩 응답의 개수나 형식이 다르다")
    return [[float(x) for x in vector] for vector in vectors]
