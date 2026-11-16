"""Agent Framework 어댑터: 메시지 변환, 응답 변환, 오류, 모델 고르기, 등록 계획.

agent_framework가 없는 곳에서도 돌도록 chat client와 Message를 가짜로 넣는다. 실제 패키지가
있어야 하는 부분은 importorskip으로 나눴다. 회사와 수치는 모두 가상이다.
"""

import asyncio
import json

import pytest

from app.v3 import service
from app.v3.agents import maf
from app.v3.agents.llm import FoundryChat, LLMError, OllamaChat
from app.v3.agents.policy_rule import RulePolicy
from scripts.v3 import register_foundry_agents
from tests.v3.runtime_fixtures import HANBIT, approve_all, make_app, recheck, to_approval

SCHEMA = {"type": "object", "properties": {"type": {"type": "string"}}}


class Response:
    def __init__(self, text, usage=None):
        self.text, self.usage_details = text, usage


class FakeClient:
    """get_response(messages, options=...)만 가진 가짜 chat client."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def get_response(self, messages, options=None):
        self.calls.append((messages, options))
        await asyncio.sleep(0)
        answer = self.answer(messages, options) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeAgent:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def run(self, messages, options=None):
        self.calls.append((messages, options))
        return self.answer


def pair(role, text):
    return (role, text)


def chat(client, **options):
    return maf.MafChat(lambda: client, model="chat-deploy", message_factory=pair, **options)


MESSAGES = [
    {"role": "system", "content": "지시문"},
    {"role": "user", "content": '{"task": "inquiry"}'},
    {"role": "assistant", "content": '{"type": "inquiry"}'},
    {"role": "user", "content": '{"rejections": []}'},
]


def test_plain_turns_keeps_roles_and_drops_empty_text():
    turns = maf.plain_turns(
        [
            *MESSAGES,
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "name": "t"}]},
            {"role": "tool", "tool_call_id": "1", "name": "judge_route", "content": '{"a": 1}'},
        ]
    )
    assert turns == [
        ("system", "지시문"),
        ("user", '{"task": "inquiry"}'),
        ("assistant", '{"type": "inquiry"}'),
        ("user", '{"rejections": []}'),
        ("user", '도구 결과(judge_route): {"a": 1}'),
    ]
    with pytest.raises(LLMError, match="역할"):
        maf.plain_turns([{"role": "developer", "content": "x"}])


def test_complete_passes_messages_and_schema_and_never_returns_tool_calls():
    client = FakeClient(Response('{"type": "offer"}', {"input_token_count": 12}))
    llm = chat(client)
    result = llm.complete(MESSAGES, tools=[{"name": "judge_route"}], response_schema=SCHEMA)
    assert result == {
        "content": '{"type": "offer"}',
        "tool_calls": [],
        "usage": {"input_token_count": 12},
    }
    sent, options = client.calls[0]
    assert sent == maf.plain_turns(MESSAGES) and options == {"response_format": SCHEMA}
    assert (llm.provider, llm.model, llm.via) == ("foundry", "chat-deploy", "agent-framework")
    llm.complete(MESSAGES)
    assert client.calls[1][1] is None  # 스키마가 없으면 options를 주지 않는다
    assert chat(FakeClient(Response(None))).complete(MESSAGES)["content"] is None


def test_provider_errors_become_llm_errors_without_details():
    boom = chat(FakeClient(ValueError("키 test-secret이 틀렸다")))
    with pytest.raises(LLMError) as caught:
        boom.complete(MESSAGES)
    assert "ValueError" in str(caught.value) and "test-secret" not in str(caught.value)
    with pytest.raises(LLMError, match="보낼 메시지"):
        chat(FakeClient(Response("x"))).complete([{"role": "user", "content": " "}])

    async def inside_a_loop():
        return chat(FakeClient(Response("x"))).complete(MESSAGES)

    with pytest.raises(LLMError, match="이벤트 루프"):
        asyncio.run(inside_a_loop())


def test_registered_agent_gets_no_system_message():
    agent = FakeAgent(Response('{"type": "question"}'))
    llm = maf.RegisteredAgentChat(lambda: agent, model="chat-deploy", message_factory=pair)
    assert llm.complete(MESSAGES, response_schema=SCHEMA)["content"] == '{"type": "question"}'
    sent, options = agent.calls[0]
    assert [role for role, _ in sent] == ["user", "assistant", "user"]
    assert options == {"response_format": SCHEMA} and llm.via == "foundry-agent-service"
    with pytest.raises(LLMError, match="보낼 메시지"):
        llm.complete([{"role": "system", "content": "지시문"}])


def test_demo_scenario_runs_through_the_adapter():
    """모델 호출 계층만 바꾼 채 시연 시나리오가 끝까지 돈다(답은 규칙 정책이 쓴다)."""
    rule = RulePolicy()
    seen = []

    def answer(messages, options):
        wire = [{"role": role, "content": text} for role, text in messages]
        seen.append(options)
        reply = rule.complete(
            wire, tools=None, response_schema=(options or {}).get("response_format")
        )
        return Response(reply["content"])

    def factory(role, org_id, model=None):
        return chat(FakeClient(answer))

    app = make_app(factory)
    view = approve_all(app, to_approval(app, HANBIT))
    assert view["session"]["status"] == "confirmed"
    checked, passed = recheck(app, view)
    assert checked > 0 and passed == checked  # 검증기를 통과한 것만 나갔다
    assert seen and all(options and "response_format" in options for options in seen)
    models = {
        json.dumps(t["vendorModel"], sort_keys=True) for t in view["threads"] if t["messages"]
    }
    assert all('"provider": "foundry"' in m and "chat-deploy" in m for m in models)
    assert service.admin_status(app, HANBIT)["models"]["company"]["name"] == "chat-deploy"


# ── 모델 고르기 ──────────────────────────────────────────────────────
def base_factory(role, org_id, model=None):
    if role == "vendor" and (model or {}).get("provider") == "ollama":
        return OllamaChat("http://ollama.invalid", "open-model")
    if org_id == "org_rule":
        return None
    return FoundryChat("https://f.invalid", "chat-deploy", "test-key")


def yes():
    return True


ENDPOINT = {"FOUNDRY_PROJECT_ENDPOINT": "https://f.invalid/api/projects/p"}


def test_factory_is_untouched_unless_configured():
    assert maf.factory_from_env(base_factory, {}, is_available=yes) is base_factory
    for env in (
        {"V3_AGENT_FRAMEWORK": "chat"},  # 프로젝트 주소가 없다
        {"V3_AGENT_FRAMEWORK": "other", **ENDPOINT},
    ):
        assert maf.factory_from_env(base_factory, env, is_available=yes) is base_factory
    env = {"V3_AGENT_FRAMEWORK": "chat", **ENDPOINT}
    assert maf.factory_from_env(base_factory, env, is_available=lambda: False) is base_factory


def test_chat_mode_replaces_only_foundry_calls():
    env = {"V3_AGENT_FRAMEWORK": "chat", **ENDPOINT}
    factory = maf.factory_from_env(base_factory, env, credential=object(), is_available=yes)
    company = factory("company", "org_hanbit", None)
    assert type(company) is maf.MafChat and company.model == "chat-deploy"
    assert company.provider == "foundry"
    assert factory("company", "org_rule", None) is None  # 규칙 정책은 그대로
    kept = factory("vendor", "org_namhae", {"provider": "ollama", "name": "open-model"})
    assert isinstance(kept, OllamaChat)  # 업체가 고른 오픈 모델은 바꾸지 않는다


def test_agent_mode_needs_a_registered_version_per_role():
    env = {"V3_AGENT_FRAMEWORK": "agent", **ENDPOINT, "FOUNDRY_COMPANY_AGENT_VERSION": "3"}
    factory = maf.factory_from_env(base_factory, env, credential=object(), is_available=yes)
    assert type(factory("company", "org_hanbit", None)) is maf.RegisteredAgentChat
    # 업체 측은 버전이 없으므로 바꾸지 않는다
    assert isinstance(factory("vendor", "org_saegil", {"provider": "foundry"}), FoundryChat)


def test_instructions_are_the_runtime_prompts():
    from app.v3.agents import company, vendor

    assert maf.instructions_for("company") == company.SYSTEM_PROMPT
    assert maf.instructions_for("vendor") == vendor.SYSTEM_PROMPT
    with pytest.raises(ValueError, match="역할"):
        maf.instructions_for("admin")


# ── 등록 스크립트 ────────────────────────────────────────────────────
class FakeProject:
    def __init__(self):
        self.created = []
        self.agents = self

    def create_version(self, agent_name, definition):
        self.created.append((agent_name, definition))
        version = str(len(self.created))
        return type(
            "Created", (), {"id": f"{agent_name}:{version}", "name": agent_name, "version": version}
        )()


def definition(model, instructions):
    return {"model": model, "instructions": instructions}


def test_register_plan_and_dry_run(capsys):
    assert register_foundry_agents.main(["--dry-run"], {}) == 2
    assert "FOUNDRY_MODEL" in capsys.readouterr().err
    items = register_foundry_agents.plan({"FOUNDRY_MODEL": "chat-deploy"})
    assert [(i["role"], i["name"]) for i in items] == [
        ("company", "rsai-company-agent"),
        ("vendor", "rsai-vendor-agent"),
    ]
    assert items[0]["instructions"] == maf.instructions_for("company")
    renamed = register_foundry_agents.plan(
        {"FOUNDRY_VENDOR_AGENT_NAME": "my-vendor"}, model="other-deploy"
    )
    assert renamed[1]["name"] == "my-vendor" and renamed[1]["model"] == "other-deploy"
    assert register_foundry_agents.main(["--dry-run"], {"FOUNDRY_MODEL": "chat-deploy"}) == 0
    printed = capsys.readouterr().out
    assert "rsai-company-agent" in printed and "등록하지 않았다" in printed


def test_register_needs_the_project_endpoint_and_reports_versions(capsys):
    env = {"FOUNDRY_MODEL": "chat-deploy"}
    assert register_foundry_agents.main([], env) == 2
    assert "FOUNDRY_PROJECT_ENDPOINT" in capsys.readouterr().err
    project = FakeProject()
    assert register_foundry_agents.main([], env, project=project, definition=definition) == 0
    assert [name for name, _ in project.created] == ["rsai-company-agent", "rsai-vendor-agent"]
    assert project.created[1][1] == {
        "model": "chat-deploy",
        "instructions": maf.instructions_for("vendor"),
    }
    printed = capsys.readouterr().out
    assert "FOUNDRY_COMPANY_AGENT_VERSION=1" in printed
    assert "FOUNDRY_VENDOR_AGENT_VERSION=2" in printed


# ── 실제 패키지가 있을 때만 ──────────────────────────────────────────
def test_real_agent_framework_objects_can_be_built():
    pytest.importorskip("agent_framework")
    message = maf.framework_message("user", "안녕하세요")
    assert message.role == "user"
    assert maf.judge_mcp_tool("http://127.0.0.1:8000/mcp", service_key="test-key") is not None


def test_real_prompt_agent_definition_takes_model_and_instructions():
    models = pytest.importorskip("azure.ai.projects.models")
    project = FakeProject()
    items = register_foundry_agents.plan({"FOUNDRY_MODEL": "chat-deploy"})
    done = register_foundry_agents.register_with(project, items, models.PromptAgentDefinition)
    assert [d["version"] for d in done] == ["1", "2"]
    assert project.created[0][1].model == "chat-deploy"
