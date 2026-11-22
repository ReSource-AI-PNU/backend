"""업체 MCP 연결 키와 POST /mcp/vendor 입구: 키가 조직을 정하고, 원문은 한 번만, 끊은 키는 못 쓴다."""

import json

import pytest

from app.v3 import service
from app.v3.onboarding import keys as mcp_keys
from app.v3.onboarding import mcp_http
from app.v3.onboarding import tools as T
from tests.v3.test_onboarding import NEW, REGISTRY_ID, _app


def rpc(method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_keys_are_shown_once_and_stored_as_hashes():
    app, _ = _app()
    made = mcp_keys.post_key(app, NEW, {"label": "사내 AI"}, NEW)
    token = made["token"]
    assert token.startswith("rsai_") and made["key"]["prefix"] == token[:12]
    listed = mcp_keys.get_keys(app, NEW, NEW)
    assert listed["path"] == "/mcp/vendor" and "token" not in json.dumps(listed)
    stored = app.store.get_vendor_profile(NEW)["mcpKeys"][0]
    assert token not in json.dumps(stored)
    assert "mcpKeys" not in service.get_vendor(app, NEW, NEW)
    with pytest.raises(service.ServiceError):
        mcp_keys.post_key(app, NEW, {"label": ""}, NEW)
    with pytest.raises(service.ServiceError):
        mcp_keys.get_keys(app, NEW, "org_hanbit")  # 다른 조직
    for n in range(4):
        mcp_keys.post_key(app, NEW, {"label": f"키{n}"}, NEW)
    with pytest.raises(service.ServiceError) as limit:
        mcp_keys.post_key(app, NEW, {"label": "여섯째"}, NEW)
    assert limit.value.code == "KEY_LIMIT"


def test_mcp_endpoint_lists_calls_and_leaves_a_note_in_the_chat():
    app, _ = _app()
    token = mcp_keys.post_key(app, NEW, {"label": "사내 AI"}, NEW)["token"]
    auth = f"Bearer {token}"
    status, body = mcp_http.handle(app, auth, rpc("initialize", {"protocolVersion": "2025-03-26"}))
    assert status == 200 and body["result"]["protocolVersion"] == "2025-03-26"
    assert mcp_http.handle(
        app, auth, {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) == (
        202,
        None,
    )
    status, body = mcp_http.handle(app, auth, rpc("tools/list"))
    names = [t["name"] for t in body["result"]["tools"]]
    assert names == list(T.TOOLS) and "propose_criteria" in names

    call = rpc(
        "tools/call", {"name": "use_registry_vendor", "arguments": {"registryId": REGISTRY_ID}}
    )
    status, body = mcp_http.handle(app, auth, call)
    output = json.loads(body["result"]["content"][0]["text"])
    assert output["written"] == ["permits", "fleet.depot"] and body["result"]["isError"] is False
    criteria = {
        "rows": [{"material": "PP", "unitPriceKrwPerKg": 300, "quote": "PP는 kg당 300원에 매입"}]
    }
    status, body = mcp_http.handle(
        app, auth, rpc("tools/call", {"name": "propose_criteria", "arguments": criteria}, 2)
    )
    assert json.loads(body["result"]["content"][0]["text"])["written"] == ["criteria"]

    profile = app.store.get_vendor_profile(NEW)
    assert profile["permits"] == [] and profile["criteria"] == []  # 초안만
    draft = profile["onboarding"]["draft"]
    assert draft["criteria"]["source"]["label"] == "연결한 에이전트(사내 AI)가 제출"
    notes = [m["text"] for m in profile["onboarding"]["messages"]]
    assert any(n.startswith("연결한 에이전트(사내 AI)가 초안에 넣었습니다") for n in notes)
    assert profile["mcpKeys"][0]["lastUsedAt"] is not None


def test_mcp_endpoint_refuses_wrong_or_revoked_keys():
    app, _ = _app()
    made = mcp_keys.post_key(app, NEW, {"label": "사내 AI"}, NEW)
    assert mcp_http.handle(app, "Bearer rsai_nope", rpc("tools/list"))[0] == 401
    assert mcp_http.handle(app, "", rpc("tools/list"))[0] == 401
    mcp_keys.delete_key(app, NEW, made["key"]["id"], NEW)
    assert mcp_http.handle(app, f"Bearer {made['token']}", rpc("tools/list"))[0] == 401
    with pytest.raises(service.ServiceError) as missing:
        mcp_keys.delete_key(app, NEW, made["key"]["id"], NEW)
    assert missing.value.code == "KEY_NOT_FOUND"


def test_unknown_method_and_tool_are_json_rpc_errors():
    app, _ = _app()
    auth = f"Bearer {mcp_keys.post_key(app, NEW, {'label': 'a'}, NEW)['token']}"
    assert mcp_http.handle(app, auth, rpc("resources/list"))[1]["error"]["code"] == -32601
    _, body = mcp_http.handle(
        app, auth, rpc("tools/call", {"name": "confirm", "arguments": {}})
    )
    assert body["error"]["code"] == -32602
