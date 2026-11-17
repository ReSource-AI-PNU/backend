"""Microsoft Agent Framework(Python) 어댑터. 모델 호출 계층만 Agent Framework로 바꾼다.

로컬 미실행: 작성한 환경에서는 agent_framework, azure.identity를 불러올 수 없었다. 그 패키지를
부르는 줄은 공식 문서의 예시에 있는 호출만 썼고(아래 "확인한 API"), 나머지(메시지 변환, 응답 변환,
오류 처리, 모델 고르기)는 가짜 chat client로 tests/v3/test_maf.py가 로컬에서 확인한다.

우리 런타임은 그대로다. 초안 → 검증 → 재작성 → handoff 고리와 claim 풀(agents/draft.py)이
모델에게 JSON만 받고, 숫자와 조항은 코드가 채운다. 여기서는 LLM 프로토콜(agents/llm.py)의
complete()를 Agent Framework의 chat client나 Foundry Agent Service에 등록한 에이전트로 구현한다.

- MafChat: FoundryChatClient.get_response로 부른다(앱이 지시문과 대화를 가진다).
- RegisteredAgentChat: Foundry Agent Service에 등록한 Prompt Agent를 FoundryAgent.run으로 부른다
  (지시문은 등록된 것을 쓴다. scripts/v3/register_foundry_agents.py가 같은 지시문으로 등록한다).
- 판단 도구는 모델이 직접 부르지 않는다. 모델은 JSON의 toolCalls에 도구 이름과 흐름 ID만 적고,
  코드(AgentTools)가 조직과 입력을 정해 부른다. 그래서 complete()는 tool_calls를 늘 비워 돌려준다.
- judge_mcp_tool은 판단 도구 MCP 서버(app/v3/mcp_server.py)를 Agent Framework의 MCP 도구로 잇는
  구성이다. 그 서버의 도구는 호출자가 org_id를 정하므로, 모델에게 그대로 쥐여 주면 조직 경계를
  모델이 정하게 된다. 그래서 MafChat에는 연결하지 않고, 조직이 하나로 고정된 자리(예: 한 업체만
  대변하는 진단용 에이전트)에서만 build_agent로 쓴다.

확인한 API (2026-11-17, Agent Framework python-1.25.0 문서):
- agent_framework: Agent(client=, name=, instructions=, tools=), Message(role=, contents=[글]),
  MCPStreamableHTTPTool(name=, url=) + static_headers(본문에 설명만 있고 예시 코드는 없다)
- agent_framework.foundry: FoundryChatClient(project_endpoint=, model=, credential=),
  FoundryAgent(project_endpoint=, agent_name=, agent_version=, credential=)
- await client.get_response(messages, options={"response_format": ...}) → .text, .usage_details
- await agent.run(messages, options={"response_format": JSON 스키마 dict}) → .text
  https://learn.microsoft.com/agent-framework/agents/providers/microsoft-foundry
  https://learn.microsoft.com/agent-framework/agents/providers/openai
  https://learn.microsoft.com/agent-framework/agents/structured-output
  https://learn.microsoft.com/agent-framework/user-guide/model-context-protocol/using-mcp-tools
  https://learn.microsoft.com/agent-framework/integrations/by-component/agent-services/foundry
필요한 패키지: agent-framework-foundry, azure-identity (MCP 도구를 쓰면 mcp>=2.2,<3. 판단 도구
MCP 서버의 mcp>=1.10,<2와 한 환경에 함께 둘 수 없으니 서버와 에이전트의 이미지를 나눈다).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from app.v3.agents.llm import FoundryChat, LLMError

logger = logging.getLogger(__name__)

COMPANY_AGENT, VENDOR_AGENT = "rsai-company-agent", "rsai-vendor-agent"
AGENT_NAMES = {"company": COMPANY_AGENT, "vendor": VENDOR_AGENT}
MODES = ("chat", "agent")


def instructions_for(role: str) -> str:
    """등록하는 에이전트의 지시문. 런타임이 쓰는 시스템 프롬프트와 같은 글이다."""
    if role == "company":
        from app.v3.agents.company import SYSTEM_PROMPT
    elif role == "vendor":
        from app.v3.agents.vendor import SYSTEM_PROMPT
    else:
        raise ValueError(f"역할은 company나 vendor다: {role!r}")
    return SYSTEM_PROMPT


def plain_turns(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """우리 메시지 목록을 (역할, 글) 목록으로 바꾼다. 빈 글은 뺀다.

    도구 결과 메시지는 여기서 생기지 않지만(tool_calls를 돌려주지 않는다), 들어오면 버리지 않고
    user 글로 옮긴다.
    """
    turns: list[tuple[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        text = message.get("content")
        text = "" if text is None else str(text)
        if role == "tool":
            role, text = "user", f"도구 결과({message.get('name') or ''}): {text}"
        if role not in ("system", "user", "assistant"):
            raise LLMError(f"알 수 없는 메시지 역할: {role}")
        if text.strip():
            turns.append((role, text))
    return turns


def framework_message(role: str, text: str) -> Any:
    """Agent Framework의 Message. 로컬 미실행."""
    from agent_framework import Message

    return Message(role=role, contents=[text])


def default_credential() -> Any:
    """관리 ID나 az 로그인으로 토큰을 받는다. 로컬 미실행."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def foundry_client_factory(
    project_endpoint: str, model: str, credential: Any = None
) -> Callable[[], Any]:
    """부를 때마다 FoundryChatClient를 새로 만드는 함수. 로컬 미실행.

    complete()가 호출마다 새 이벤트 루프를 쓰므로 클라이언트를 루프 사이에 나눠 쓰지 않는다.
    """

    def make() -> Any:
        from agent_framework.foundry import FoundryChatClient

        return FoundryChatClient(
            project_endpoint=project_endpoint,
            model=model,
            credential=credential if credential is not None else default_credential(),
        )

    return make


def registered_agent_factory(
    project_endpoint: str, agent_name: str, agent_version: str, credential: Any = None
) -> Callable[[], Any]:
    """부를 때마다 등록된 Prompt Agent에 붙는 FoundryAgent를 만드는 함수. 로컬 미실행."""

    def make() -> Any:
        from agent_framework.foundry import FoundryAgent

        return FoundryAgent(
            project_endpoint=project_endpoint,
            agent_name=agent_name,
            agent_version=agent_version,
            credential=credential if credential is not None else default_credential(),
        )

    return make


class MafChat:
    """LLM 프로토콜 구현: Agent Framework chat client의 get_response로 답을 받는다."""

    via = "agent-framework"

    def __init__(
        self,
        client_factory: Callable[[], Any],
        *,
        model: str,
        provider: str = "foundry",
        message_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._message = message_factory or framework_message
        self.provider = provider
        self.model = model

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={self.provider!r}, model={self.model!r})"

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """{"content", "tool_calls": [], "usage"}. 도구는 모델에 넘기지 않는다(모듈 설명 참고)."""
        turns = plain_turns(messages)
        if not turns:
            raise LLMError("보낼 메시지가 없다")
        request = self._ask(turns, response_schema)
        try:
            text, usage = asyncio.run(request)
        except LLMError:
            raise
        except RuntimeError as error:
            if "event loop" in str(error):
                raise LLMError(
                    "이벤트 루프 안에서는 부를 수 없다(작업 스레드에서 부른다)"
                ) from error
            raise LLMError(f"Agent Framework 호출 실패: {type(error).__name__}") from error
        except Exception as error:
            raise LLMError(f"Agent Framework 호출 실패: {type(error).__name__}") from error
        finally:
            request.close()  # 루프를 열지 못했을 때 남는 코루틴을 닫는다
        if text is not None and not isinstance(text, str):
            text = str(text)
        return {"content": text, "tool_calls": [], "usage": usage}

    async def _ask(
        self, turns: list[tuple[str, str]], response_schema: dict[str, Any] | None
    ) -> tuple[Any, dict[str, Any]]:
        client = self._client_factory()
        wire = [self._message(role, text) for role, text in turns]
        if response_schema is None:
            response = await client.get_response(wire)
        else:
            response = await client.get_response(wire, options={"response_format": response_schema})
        return response.text, _usage(response)


class RegisteredAgentChat(MafChat):
    """LLM 프로토콜 구현: Foundry Agent Service에 등록한 Prompt Agent를 FoundryAgent.run으로 부른다.

    지시문은 등록된 것을 쓰므로 system 메시지는 보내지 않는다. 등록한 지시문이 코드의 시스템
    프롬프트와 달라졌으면 register_foundry_agents로 새 버전을 등록하고 버전 설정을 올린다.
    """

    via = "foundry-agent-service"

    async def _ask(
        self, turns: list[tuple[str, str]], response_schema: dict[str, Any] | None
    ) -> tuple[Any, dict[str, Any]]:
        agent = self._client_factory()
        wire = [self._message(role, text) for role, text in turns if role != "system"]
        if not wire:
            raise LLMError("보낼 메시지가 없다")
        if response_schema is None:
            response = await agent.run(wire)
        else:
            response = await agent.run(wire, options={"response_format": response_schema})
        return response.text, _usage(response)


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage_details", None)
    return dict(usage) if isinstance(usage, Mapping) else {}


def judge_mcp_tool(url: str, *, service_key: str = "", name: str = "resource-ai-judge") -> Any:
    """판단 도구 MCP 서버(streamable HTTP)를 Agent Framework의 MCP 도구로 잇는다. 로컬 미실행.

    서비스 키는 Authorization: Bearer 머리글로 보낸다(mcp_server.authorized가 확인하는 모양).
    async with로 열어 Agent의 tools나 agent.run(tools=)에 넘긴다.
    """
    from agent_framework import MCPStreamableHTTPTool

    if service_key:
        return MCPStreamableHTTPTool(
            name=name, url=url, static_headers={"Authorization": f"Bearer {service_key}"}
        )
    return MCPStreamableHTTPTool(name=name, url=url)


def build_agent(role: str, client: Any, *, mcp_tool: Any = None) -> Any:
    """역할의 지시문을 가진 Agent Framework Agent. 로컬 미실행.

    런타임은 이것을 쓰지 않는다(MafChat이 chat client를 직접 부른다). 조직이 하나로 고정된
    자리에서 판단 도구 MCP 서버를 붙여 시험할 때 쓴다.
    """
    from agent_framework import Agent

    if mcp_tool is None:
        return Agent(client=client, name=AGENT_NAMES[role], instructions=instructions_for(role))
    return Agent(
        client=client, name=AGENT_NAMES[role], instructions=instructions_for(role), tools=mcp_tool
    )


def available() -> bool:
    try:
        import agent_framework
        import agent_framework.foundry  # noqa: F401
        import azure.identity  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def factory_from_env(
    base: Callable[..., Any],
    env: Mapping[str, str],
    *,
    credential: Any = None,
    is_available: Callable[[], bool] = available,
) -> Callable[..., Any]:
    """llm_factory를 감싸, Foundry로 가는 호출만 Agent Framework로 바꾼다.

    V3_AGENT_FRAMEWORK가 비어 있으면 base 그대로다.
    - chat: FOUNDRY_PROJECT_ENDPOINT가 있어야 한다. 모델은 base가 고른 배포 이름 그대로다.
    - agent: 등록한 에이전트를 부른다. FOUNDRY_PROJECT_ENDPOINT와, 역할마다
      FOUNDRY_COMPANY_AGENT_VERSION / FOUNDRY_VENDOR_AGENT_VERSION이 있어야 한다
      (이름은 FOUNDRY_COMPANY_AGENT_NAME / FOUNDRY_VENDOR_AGENT_NAME, 기본은 등록 스크립트의 이름).
    설정이 빠졌거나 패키지가 없으면 바꾸지 않고 base를 그대로 쓴다(같은 모델을 urllib로 부른다).
    base가 None(규칙 정책)이나 OllamaChat을 준 조직은 건드리지 않는다.
    """
    mode = (env.get("V3_AGENT_FRAMEWORK") or "").strip().lower()
    if not mode:
        return base
    endpoint = (env.get("FOUNDRY_PROJECT_ENDPOINT") or "").strip()
    if mode not in MODES:
        logger.warning("V3_AGENT_FRAMEWORK는 chat이나 agent다. 쓰지 않는다: %r", mode)
        return base
    if not endpoint:
        logger.warning("FOUNDRY_PROJECT_ENDPOINT가 없어 Agent Framework를 쓰지 않는다")
        return base
    if not is_available():
        logger.warning(
            "agent-framework-foundry나 azure-identity가 없어 Agent Framework를 쓰지 않는다"
        )
        return base

    def wrapped(role: str, org_id: str, model: dict[str, Any] | None = None) -> Any:
        llm = base(role, org_id, model)
        if not isinstance(llm, FoundryChat):
            return llm
        if mode == "chat":
            return MafChat(foundry_client_factory(endpoint, llm.model, credential), model=llm.model)
        prefix = "FOUNDRY_COMPANY_AGENT" if role == "company" else "FOUNDRY_VENDOR_AGENT"
        name = (env.get(f"{prefix}_NAME") or AGENT_NAMES.get(role, "")).strip()
        version = (env.get(f"{prefix}_VERSION") or "").strip()
        if not name or not version:
            logger.warning("%s_VERSION이 없어 %s는 등록한 에이전트를 쓰지 않는다", prefix, org_id)
            return llm
        return RegisteredAgentChat(
            registered_agent_factory(endpoint, name, version, credential), model=llm.model
        )

    return wrapped
