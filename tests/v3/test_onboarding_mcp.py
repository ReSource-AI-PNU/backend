"""온보딩 MCP 서버: 키가 조직을 정하고, 확정 도구가 없고, 초안만 고친다."""

import json

import pytest

from app.v3.onboarding import mcp as onboarding_mcp
from app.v3.onboarding import tools as T
from tests.v3.test_onboarding import NEW, REGISTRY_ID, _app

KEYS = {"woori-key": NEW, "hanbit-key": "org_hanbit"}


def test_keys_bind_one_org_each():
    assert onboarding_mcp.parse_keys(" a=org_a, b=org_b ,=x,c=") == {"a": "org_a", "b": "org_b"}
    assert onboarding_mcp.org_for([(b"authorization", b"Bearer woori-key")], KEYS) == NEW
    assert onboarding_mcp.org_for([(b"authorization", b"Bearer nope")], KEYS) is None
    assert onboarding_mcp.org_for([], KEYS) is None


def test_tools_have_no_org_argument_and_no_confirm():
    names = [d["name"] for d in onboarding_mcp.tool_definitions()]
    assert names == list(T.TOOLS)
    assert not any("confirm" in n or "save" in n for n in names)
    for definition in onboarding_mcp.tool_definitions():
        assert "org_id" not in json.dumps(definition["inputSchema"])


def test_run_tool_writes_only_the_draft():
    app, _ = _app()
    out = onboarding_mcp.run_tool(app, NEW, "use_registry_vendor", {"registryId": REGISTRY_ID})
    assert out["written"] == ["permits", "fleet.depot"]
    out = onboarding_mcp.run_tool(
        app,
        NEW,
        "propose_fields",
        {"fields": [{"key": "fleet.vehicleCount", "value": 2, "quote": "차량 2대"}]},
    )
    assert out["written"] == ["fleet.vehicleCount"]
    profile = app.store.get_vendor_profile(NEW)
    assert profile["permits"] == [] and profile["fleet"] == {}
    assert profile["onboarding"]["draft"]["fleet.vehicleCount"]["source"]["kind"] == "agent"
    with pytest.raises(ValueError, match="업체 조직"):
        onboarding_mcp.run_tool(app, "org_hanbit", "profile_status", {})


def test_streamable_http_round_trip():
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    app, _ = _app()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer woori-key",
    }

    def rpc(client, method, params=None, request_id=1, auth=True):
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        sent = headers if auth else {k: v for k, v in headers.items() if k != "Authorization"}
        return client.post(onboarding_mcp.MCP_PATH, headers=sent, json=body)

    with TestClient(onboarding_mcp.build_asgi_app(app, keys=KEYS)) as client:
        assert rpc(client, "tools/list", auth=False).status_code == 401
        listed = rpc(client, "tools/list")
        assert listed.status_code == 200, listed.text
        assert [t["name"] for t in listed.json()["result"]["tools"]] == list(T.TOOLS)
        called = rpc(
            client,
            "tools/call",
            {"name": "use_registry_vendor", "arguments": {"registryId": REGISTRY_ID}},
            2,
        )
        assert called.status_code == 200, called.text
        output = json.loads(called.json()["result"]["content"][0]["text"])
        assert output["written"] == ["permits", "fleet.depot"]
    assert "permits" in app.store.get_vendor_profile(NEW)["onboarding"]["draft"]
