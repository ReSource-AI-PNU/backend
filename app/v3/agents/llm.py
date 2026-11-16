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
