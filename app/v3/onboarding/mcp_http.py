"""운영 API 안의 업체용 MCP 입구(POST /mcp/vendor). MCP streamable HTTP의 상태 없는 JSON 응답만 쓴다.

SDK(app/v3/onboarding/mcp.py)와 같은 도구를 같은 run_tool로 부르지만, API 프로세스 안에 별도 세션 관리자를
띄우지 않으려고 JSON-RPC를 직접 다룬다. 받는 메서드는 initialize, ping, tools/list, tools/call과 알림(notifications/*)
뿐이다. 서버가 먼저 보내는 메시지(SSE)는 없으므로 GET은 405다.

인증: Authorization: Bearer <업체가 만든 연결 키>(keys.py). 키가 조직을 정한다.
"""

from __future__ import annotations

import json
from typing import Any

from app.v3.onboarding import keys as mcp_keys
from app.v3.onboarding import tools as T
from app.v3.onboarding.mcp import SERVER_NAME, run_tool, tool_definitions

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "BYPRO 업체 정보 초안 도구입니다. 연결 키에 묶인 처리 업체 한 곳의 업체 정보 초안(허가 품목, 차고지, "
    "차량·수거 지역, 수거 요일, 단가 범위, 매입 기준)을 채웁니다. 값마다 근거가 되는 말을 quote로 그대로 인용하고, "
    "숫자는 그 인용 안에 있어야 합니다. 저장(확정)은 업체 담당자가 화면에서 확인할 때만 됩니다."
)


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _one(app: Any, org_id: str, label: str, message: Any) -> dict[str, Any] | None:
    """JSON-RPC 메시지 하나. 알림(id 없음)이면 None."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "JSON-RPC 2.0 요청이 아닙니다.")
    method = message.get("method")
    request_id = message.get("id")
    if "id" not in message:
        return None  # notifications/initialized 등
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(
            request_id,
            {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        if name not in T.TOOLS:
            return _error(request_id, -32602, f"알 수 없는 도구: {name}")
        try:
            output = run_tool(app, org_id, str(name), params.get("arguments") or {}, label=label)
        except ValueError as error:
            text, failed = json.dumps({"error": str(error)}, ensure_ascii=False), True
        else:
            text, failed = json.dumps(output, ensure_ascii=False), "error" in output
        return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": failed})
    return _error(request_id, -32601, f"지원하지 않는 메서드: {method}")


def handle(app: Any, authorization: str, body: Any) -> tuple[int, Any]:
    """(HTTP 상태, 응답 본문 | None). 본문이 None이면 202(알림만 받음)다."""
    scheme, _, token = str(authorization or "").partition(" ")
    found = mcp_keys.find(app, token.strip()) if scheme.lower() == "bearer" else None
    if found is None:
        return 401, {"error": {"code": "UNAUTHORIZED", "message": "업체 연결 키가 맞지 않습니다."}}
    org_id, key = found
    label = str(key.get("label") or "")
    if isinstance(body, list):
        answers = [a for a in (_one(app, org_id, label, m) for m in body) if a is not None]
        return (200, answers) if answers else (202, None)
    answer = _one(app, org_id, label, body)
    return (200, answer) if answer is not None else (202, None)
