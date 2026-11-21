"""온보딩 도구 MCP 서버. 업체가 쓰는 에이전트가 이 서버에 붙어 자기 업체 정보 초안을 채운다.

    ONBOARDING_MCP_KEYS='키1=org_a,키2=org_b' python -m app.v3.onboarding.mcp --port 8010   # /mcp

- 도구는 대화 화면의 업체 측 AI와 같은 tools.TOOLS다(명부 찾기, 명부 값 넣기, 말한 값 넣기, 현황 보기).
  저장(확정) 도구는 없다. 초안은 업체 담당자가 화면에서 확인한 항목만 업체 정보가 된다.
- 조직은 호출자가 정하지 않는다. Bearer 키 하나가 업체 조직 하나에 묶이고(ONBOARDING_MCP_KEYS), 도구 입력에
  org_id 칸이 없다. 판단 도구 MCP 서버(app/v3/mcp_server.py)처럼 호출자가 org_id를 넣는 방식이 아니라서
  키를 받은 업체는 자기 초안만 고칠 수 있다.
- 대화 밖에서 부르므로 인용(quote)을 대조할 담당자 메시지가 없다. 그래도 숫자는 인용 안에 있어야 하고,
  출처는 "연결한 에이전트가 제출"로 남아 화면에서 대화 출처와 구별된다.
- MCP Python SDK 1.x 저수준 API를 쓴다(mcp_server.py와 같은 이유와 같은 상한, pyproject의 mcp-server 선택 의존성).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hmac
import json
import os
from typing import Any

from app.v3.onboarding import agent as onboarding
from app.v3.onboarding import tools as T

SERVER_NAME = "resource-ai-vendor-onboarding"
MCP_PATH = "/mcp"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def parse_keys(text: str) -> dict[str, str]:
    """'키=org_a,키2=org_b' → {키: 조직 ID}. 빈 키나 조직은 버린다."""
    out = {}
    for part in (text or "").split(","):
        key, _, org = part.strip().partition("=")
        if key.strip() and org.strip():
            out[key.strip()] = org.strip()
    return out


def org_for(headers: list[tuple[bytes, bytes]], keys: dict[str, str]) -> str | None:
    """Bearer 키에 묶인 조직. 맞는 키가 없으면 None."""
    value = next((v for k, v in headers if k.lower() == b"authorization"), b"")
    scheme, _, token = value.decode("latin-1").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    given = token.strip().encode("utf-8")
    found = None
    for key, org in keys.items():
        if hmac.compare_digest(given, key.encode("utf-8")):
            found = org
    return found


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
        for t in T.TOOLS.values()
    ]


def run_tool(app: Any, org_id: str, name: str, arguments: Any) -> dict[str, Any]:
    """도구 하나를 그 업체의 초안 위에서 돌리고, 초안을 저장한다(대화 기록은 그대로 둔다)."""
    if name not in T.TOOLS:
        raise ValueError(f"알 수 없는 도구: {name}")
    org = app.store.get_org(org_id)
    if org.get("role") != "vendor":
        raise ValueError("업체 조직에 묶인 키가 아닙니다.")
    state = onboarding._state(onboarding._profile(app, org_id))
    ctx = T.Context(app=app, org_id=org_id, draft=state["draft"], said=None)
    result, _ = T.call_tool(ctx, name, arguments)
    onboarding._save(app, org_id, state)
    return result


def build_app() -> Any:
    from app.v3.api import build_app as build_service_app

    return build_service_app()


def build_asgi_app(app: Any, *, keys: dict[str, str]) -> Any:
    """Starlette 앱: POST /mcp (streamable HTTP, 상태 없음, JSON 응답), GET /healthz."""
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    server = Server(SERVER_NAME)
    caller: dict[str, str] = {}

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**definition) for definition in tool_definitions()]

    @server.call_tool()
    async def handle_call(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        org_id = caller.get("org")
        if not org_id:
            raise ValueError("업체 키가 없습니다.")
        work = functools.partial(run_tool, app, org_id, name, arguments or {})
        result = await anyio.to_thread.run_sync(work)
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=True, stateless=True
    )

    class McpEndpoint:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            org_id = org_for(scope.get("headers") or [], keys)
            if org_id is None:
                body = {"error": {"code": "UNAUTHORIZED", "message": "업체 키가 맞지 않습니다."}}
                await JSONResponse(body, status_code=401)(scope, receive, send)
                return
            # 상태 없는 서버라 요청 하나를 끝까지 처리한 뒤 다음 요청을 받는다(조직이 섞이지 않게)
            async with lock:
                caller["org"] = org_id
                try:
                    await manager.handle_request(scope, receive, send)
                finally:
                    caller.pop("org", None)

    lock = anyio.Lock()

    async def healthz(request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "tools": list(T.TOOLS)})

    @contextlib.asynccontextmanager
    async def lifespan(_: Any) -> Any:
        async with manager.run():
            yield

    return Starlette(
        routes=[Route("/healthz", healthz), Route(MCP_PATH, endpoint=McpEndpoint())],
        lifespan=lifespan,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re:Source AI 업체 온보딩 MCP 서버")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8010")))
    args = parser.parse_args(argv)
    keys = parse_keys(os.environ.get("ONBOARDING_MCP_KEYS", ""))
    if not keys:
        parser.error("ONBOARDING_MCP_KEYS(키=업체 조직 ID, 쉼표로 구분)가 필요하다")

    import uvicorn

    uvicorn.run(build_asgi_app(build_app(), keys=keys), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
