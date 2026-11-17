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


# 다시 불러 볼 만한 실패: 연결·시간 초과(status 없음), 요청 한도, 서버 쪽 오류
TRANSIENT_STATUSES = (408, 429, 500, 502, 503, 504)


def _post_retrying(
    transport: Transport,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    *,
    retries: int,
) -> dict[str, Any]:
    """_post_json을 부르고, 일시적인 실패면 retries번까지 곧바로 다시 부른다(초안 시도 횟수와 별개다).

    모델이 낸 답의 형식이나 검증과는 관계없는 연결 문제다. 같은 요청을 그대로 다시 보낸다.
    기다리지 않는다(에이전트 코드는 시계를 읽지 않는다. 시간 초과는 이미 timeout만큼 기다린 뒤다).
    """
    for attempt in range(retries + 1):
        try:
            return _post_json(transport, url, headers, payload, timeout)
        except LLMError as error:
            transient = error.status is None or error.status in TRANSIENT_STATUSES
            if not transient or attempt >= retries:
                raise
    raise LLMError("모델을 부르지 못했다")  # 닿지 않는다(위 고리가 돌려주거나 예외를 낸다)


class NotStrict(ValueError):
    """엄격 모드(structured outputs strict)로 바꿀 수 없는 스키마다(속성을 정하지 않은 객체 등)."""


# 엄격 모드가 받지 않는 키워드. 값의 범위는 enum으로 적는다
_LOOSE_KEYWORDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "format",
    "default",
)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON 스키마를 OpenAI 호환 structured outputs의 strict 모양으로 바꾼다(새 사본).

    - 모든 객체에 additionalProperties: false, 모든 속성을 required에 넣는다.
    - 원래 required가 아니던 속성은 null도 받게 한다(모델은 쓰지 않을 때 null을 낸다).
    - 속성을 정하지 않은 객체가 있으면 NotStrict(엄격 모드로 보낼 수 없다).
    """

    def nullable(node: dict[str, Any]) -> dict[str, Any]:
        if "anyOf" in node:
            options = list(node["anyOf"])
            if not any(o.get("type") == "null" for o in options if isinstance(o, dict)):
                options.append({"type": "null"})
            return {**node, "anyOf": options}
        kind = node.get("type")
        if kind in ("object", "array"):
            return {"anyOf": [node, {"type": "null"}]}
        if isinstance(kind, str):
            node = {**node, "type": [kind, "null"]}
        elif isinstance(kind, list) and "null" not in kind:
            node = {**node, "type": [*kind, "null"]}
        if isinstance(node.get("enum"), list) and None not in node["enum"]:
            node["enum"] = [*node["enum"], None]
        return node

    def walk(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        node = {k: v for k, v in node.items() if k not in _LOOSE_KEYWORDS}
        if "anyOf" in node:
            node["anyOf"] = [walk(option) for option in node["anyOf"]]
            return node
        kind = node.get("type")
        if kind == "object" or (isinstance(kind, list) and "object" in kind):
            properties = node.get("properties")
            if not isinstance(properties, dict) or not properties:
                raise NotStrict("속성을 정하지 않은 객체는 엄격 모드로 보낼 수 없다")
            required = set(node.get("required") or [])
            node["properties"] = {
                name: walk(sub) if name in required else nullable(walk(sub))
                for name, sub in properties.items()
            }
            node["required"] = list(properties)
            node["additionalProperties"] = False
        elif kind == "array" or (isinstance(kind, list) and "array" in kind):
            if not isinstance(node.get("items"), dict):
                raise NotStrict("items가 없는 배열은 엄격 모드로 보낼 수 없다")
            node["items"] = walk(node["items"])
        return node

    return walk(json.loads(json.dumps(schema)))


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


REASONING_EFFORTS = ("minimal", "low", "medium", "high")


class FoundryChat:
    """Foundry(Azure OpenAI 호환) chat completions.

    - response_schema가 있으면 structured outputs로 보낸다. strict(기본)면 스키마를 strict_schema로
      바꿔 모델이 정해진 모양(종류 목록, 흐름 ID 목록 등)을 벗어나지 못하게 한다. 바꿀 수 없는
      스키마(속성을 정하지 않은 객체)는 strict 없이 보낸다.
    - reasoning_effort를 주면 그대로 보낸다(gpt-5 계열. 기본 강도는 추론 토큰을 많이 써서 응답이
      느리고, max_completion_tokens를 추론에 다 쓰면 본문이 비어 돌아온다).
    - 응답이 길이 제한에서 잘렸거나 모델이 거절했으면 LLMError다(빈 글을 형식 오류로 보이지 않게).
    - 연결 실패·시간 초과·429·5xx는 retries번(기본 한 번) 다시 부른다. 초안 시도로 세지 않는다.
    """

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
        reasoning_effort: str | None = None,
        strict: bool = True,
        retries: int = 1,
    ) -> None:
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"reasoning_effort는 {', '.join(REASONING_EFFORTS)} 가운데 하나다")
        self.endpoint = endpoint.rstrip("/")
        self.model = deployment
        self.api_version = api_version
        self._api_key = api_key
        self._transport = transport or urllib_transport
        self.max_completion_tokens = max_completion_tokens
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.strict = strict
        self.retries = retries

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
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = _function_tools(tools)
            payload["tool_choice"] = "auto"
        if response_schema is not None:
            schema, strict = response_schema, False
            if self.strict:
                try:
                    schema, strict = strict_schema(response_schema), True
                except NotStrict:
                    schema, strict = response_schema, False
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": strict},
            }
        data = _post_retrying(
            self._transport,
            self.url,
            {"api-key": self._api_key},
            payload,
            self.timeout,
            retries=self.retries,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
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
        if message.get("refusal"):
            raise LLMError("모델이 답을 거절했다")
        if choice.get("finish_reason") == "length" and not calls:
            usage = data.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            raise LLMError(
                "모델 응답이 길이 제한(max_completion_tokens)에서 잘렸다"
                f"(추론 토큰 {details.get('reasoning_tokens', '?')}개)"
            )
        return {
            "content": message.get("content"),
            "tool_calls": calls,
            "usage": data.get("usage") or {},
        }


class OllamaChat:
    """Ollama /api/chat. VNet 안의 주소로만 부른다.

    - response_schema는 format(JSON 스키마)으로 보낸다. Ollama가 그 스키마로 출력을 묶는다
      (종류 목록, 흐름 ID 목록 같은 enum도 지킨다).
    - num_ctx: Ollama의 기본 문맥 길이(2048~4096 토큰)는 에이전트 입력(수천~만 토큰)보다 짧아 앞부분이
      잘린다. 그래서 문맥 길이를 늘려 보낸다. num_predict는 출력 길이의 상한이다.
    - 응답이 길이 제한에서 끝났으면(done_reason이 length) LLMError다.
    """

    provider = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        transport: Transport | None = None,
        *,
        timeout: float = 120,
        num_ctx: int | None = 16384,
        num_predict: int | None = 2048,
        retries: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport or urllib_transport
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.retries = retries

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
        options: dict[str, Any] = {"temperature": 0}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        if self.num_predict:
            options["num_predict"] = self.num_predict
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._wire(messages),
            "stream": False,
            "options": options,
        }
        if tools:
            payload["tools"] = _function_tools(tools)
        if response_schema is not None:
            payload["format"] = response_schema
        data = _post_retrying(
            self._transport,
            f"{self.base_url}/api/chat",
            {},
            payload,
            self.timeout,
            retries=self.retries,
        )
        message = data.get("message")
        if not isinstance(message, dict):
            raise LLMError("모델 응답에 message가 없다")
        if data.get("done_reason") == "length" and not message.get("tool_calls"):
            raise LLMError("모델 응답이 길이 제한(num_predict)에서 잘렸다")
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
