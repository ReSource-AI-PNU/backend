"""판단 도구 MCP 서버. toolbox의 도구 10개를 그대로 노출한다(streamable HTTP).

주의: 이 파일의 서버 부분은 작성한 환경에서 실행해 보지 못했다(mcp를 불러올 수 없었다).
tests/v3/test_mcp_server.py가 CI에서 확인한다.

    python -m app.v3.mcp_server --host 127.0.0.1 --port 8000     # 주소: http://<host>:<port>/mcp

- 도구 이름과 설명은 toolbox에서 가져온다. 입력은 {"org_id", "today", "arguments"}이고
  arguments의 스키마가 toolbox의 input_schema 그대로다. 호출마다 org_id와 today를 받아
  toolbox.call_tool에 넘긴다. 결과(dict)는 JSON 문자열 하나로 돌려준다.
- 준비된 JSON Schema를 그대로 싣기 위해 MCP Python SDK의 저수준 Server를 쓴다
  (FastMCP는 함수 서명에서 스키마를 만들어 toolbox의 스키마를 그대로 실을 수 없다).
- 근거 레지스트리는 DATABASE_URL이 있으면 API와 같은 SQL 레지스트리다. 없으면 이 프로세스
  안의 메모리 레지스트리(법령·기준값·가정값 시드만 든 것)라서 API와 근거를 나누지 못한다.
- 호출자가 org_id를 정하므로 내부망에서만 쓴다. 127.0.0.1이 아닌 주소로 띄우려면
  MCP_SERVICE_KEY(Bearer)가 있어야 한다. 도로 거리 제공자는 연결하지 않았다(가정값 경로).
"""

import argparse
import contextlib
import datetime
import functools
import hmac
import json
import os
from typing import Any

from app.v3.judge.toolbox import TOOLS, call_tool

SERVER_NAME = "resource-ai-judge"
MCP_PATH = "/mcp"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def tool_definitions() -> list[dict[str, Any]]:
    """MCP tools/list에 실을 정의. toolbox의 순서와 스키마를 그대로 쓴다."""
    return [
        {
            "name": spec.name,
            "description": f"[{spec.stage}단계] {spec.description}",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "호출하는 조직 ID"},
                    "today": {"type": "string", "description": "기준일 (YYYY-MM-DD)"},
                    "arguments": spec.input_schema,
                },
                "required": ["org_id", "today", "arguments"],
                "additionalProperties": False,
            },
        }
        for spec in TOOLS.values()
    ]


def run_tool(name: str, payload: dict[str, Any], *, registry: Any) -> dict[str, Any]:
    """MCP 호출 하나를 toolbox.call_tool로 넘긴다. 입력이 틀리면 ValueError."""
    if name not in TOOLS:
        raise ValueError(f"알 수 없는 도구: {name}")
    if not isinstance(payload, dict):
        raise ValueError("입력은 객체여야 한다")  # noqa: TRY004
    unknown = sorted(set(payload) - {"org_id", "today", "arguments"})
    if unknown:
        raise ValueError(f"알 수 없는 입력: {', '.join(unknown)}")
    org_id, today = payload.get("org_id"), payload.get("today")
    if not isinstance(org_id, str) or not org_id.strip():
        raise ValueError("org_id가 필요하다")
    try:
        datetime.date.fromisoformat(today if isinstance(today, str) else "")
    except ValueError:
        raise ValueError("today는 YYYY-MM-DD 날짜여야 한다") from None
    return call_tool(
        name, payload.get("arguments"), registry=registry, org_id=org_id.strip(), today=today
    )


def authorized(headers: list[tuple[bytes, bytes]], service_key: str) -> bool:
    """service_key가 비어 있으면 검사하지 않는다(127.0.0.1로만 띄울 때)."""
    if not service_key:
        return True
    value = next((v for k, v in headers if k.lower() == b"authorization"), b"")
    scheme, _, token = value.decode("latin-1").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        token.strip().encode("utf-8"), service_key.encode("utf-8")
    )


def build_registry() -> Any:
    from app.core.config import settings

    if settings.database_url:
        from app.platform.db import engine
        from app.v3.sql import SqlRegistry

        return SqlRegistry(engine())
    from app.v3.evidence.registry import InMemoryRegistry
    from app.v3.evidence.seeds import load_seed_dir
    from app.v3.judge.defaults import register_defaults

    registry = InMemoryRegistry()
    load_seed_dir(registry)
    register_defaults(registry)
    return registry


def build_asgi_app(registry: Any, *, service_key: str = "") -> Any:
    """Starlette 앱: POST /mcp (streamable HTTP, 상태 없음, JSON 응답), GET /healthz."""
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**definition) for definition in tool_definitions()]

    @server.call_tool()
    async def handle_call(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        # 도구는 동기 함수라 작업 스레드에서 돌린다. 예외는 SDK가 오류 결과(isError)로 바꾼다.
        work = functools.partial(run_tool, name, arguments or {}, registry=registry)
        result = await anyio.to_thread.run_sync(work)
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=True, stateless=True
    )

    class McpEndpoint:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if not authorized(scope.get("headers") or [], service_key):
                body = {"error": {"code": "UNAUTHORIZED", "message": "서비스 키가 맞지 않습니다."}}
                await JSONResponse(body, status_code=401)(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

    async def healthz(request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "tools": list(TOOLS)})

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> Any:
        async with manager.run():
            yield

    return Starlette(
        routes=[Route("/healthz", healthz), Route(MCP_PATH, endpoint=McpEndpoint())],
        lifespan=lifespan,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re:Source AI 판단 도구 MCP 서버")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8000")))
    args = parser.parse_args(argv)
    service_key = os.environ.get("MCP_SERVICE_KEY", "")
    if args.host not in LOCAL_HOSTS and not service_key:
        parser.error("127.0.0.1이 아닌 주소로 띄우려면 MCP_SERVICE_KEY가 있어야 한다")

    import uvicorn

    uvicorn.run(
        build_asgi_app(build_registry(), service_key=service_key), host=args.host, port=args.port
    )


if __name__ == "__main__":
    main()
