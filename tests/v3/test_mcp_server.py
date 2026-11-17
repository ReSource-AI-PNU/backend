"""판단 도구 MCP 서버: toolbox와 같은 도구 목록, 호출 전달, streamable HTTP 한 바퀴."""

import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient

from app.v3 import mcp_server
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import load_seed_dir
from app.v3.judge.defaults import register_defaults
from app.v3.judge.toolbox import TOOLS

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
CALL = {
    "org_id": "org_hanbit",
    "today": "2026-11-17",
    "arguments": {"query": "폐기물 위탁", "limit": 2},
}


@pytest.fixture
def registry():
    made = InMemoryRegistry()
    load_seed_dir(made)
    register_defaults(made)
    return made


def rpc(client, method, params=None, headers=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(mcp_server.MCP_PATH, headers={**HEADERS, **(headers or {})}, json=body)


def test_definitions_come_from_toolbox():
    definitions = mcp_server.tool_definitions()
    assert [d["name"] for d in definitions] == list(TOOLS) and len(definitions) == 10
    for definition in definitions:
        spec = TOOLS[definition["name"]]
        assert spec.description in definition["description"]
        schema = definition["inputSchema"]
        assert schema["properties"]["arguments"] == spec.input_schema
        assert schema["required"] == ["org_id", "today", "arguments"]


def test_run_tool_passes_org_and_date(registry):
    result = mcp_server.run_tool("search_clauses", CALL, registry=registry)
    assert result["tool"] == "search_clauses"
    assert mcp_server.run_tool("search_clauses", CALL, registry=registry) == result  # 결정적
    bad_calls = (
        {**CALL, "org_id": ""},
        {**CALL, "today": "어제"},
        {**CALL, "today": None},
        {**CALL, "extra": 1},
        {"org_id": "org_hanbit", "today": "2026-11-17"},
    )
    for bad in bad_calls:
        with pytest.raises(ValueError, match="."):
            mcp_server.run_tool("search_clauses", bad, registry=registry)
    with pytest.raises(ValueError, match="알 수 없는 도구"):
        mcp_server.run_tool("없는_도구", CALL, registry=registry)


def test_streamable_http_lists_and_calls_tools(registry):
    with TestClient(mcp_server.build_asgi_app(registry)) as client:
        assert client.get("/healthz").json() == {"status": "ok", "tools": list(TOOLS)}
        listed = rpc(client, "tools/list")
        assert listed.status_code == 200, listed.text
        tools = listed.json()["result"]["tools"]
        assert [t["name"] for t in tools] == list(TOOLS)
        assert tools[0]["inputSchema"]["properties"]["arguments"] == (
            TOOLS[tools[0]["name"]].input_schema
        )

        called = rpc(client, "tools/call", {"name": "search_clauses", "arguments": CALL}, None, 2)
        assert called.status_code == 200, called.text
        result = called.json()["result"]
        assert not result.get("isError")
        output = json.loads(result["content"][0]["text"])
        assert output == mcp_server.run_tool("search_clauses", CALL, registry=registry)

        failed = rpc(
            client,
            "tools/call",
            {"name": "search_clauses", "arguments": {**CALL, "today": "어제"}},
            None,
            3,
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["result"]["isError"] is True


def test_service_key_guards_the_endpoint(registry):
    with TestClient(mcp_server.build_asgi_app(registry, service_key="test-key")) as client:
        assert rpc(client, "tools/list").status_code == 401
        assert rpc(client, "tools/list", headers={"Authorization": "Bearer no"}).status_code == 401
        allowed = rpc(client, "tools/list", headers={"Authorization": "Bearer test-key"})
        assert allowed.status_code == 200, allowed.text
