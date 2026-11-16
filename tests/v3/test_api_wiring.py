"""가장자리 파일이 부르는 함수의 이름·인자와 경로 목록을 소스(ast)로 대조한다(순수 파이썬).

api.py, vendor_service.py, mcp_server.py, sql.py는 여기서 import하지 않는다. fastapi 등이 없어
그 파일의 테스트가 건너뛰어지는 환경에서도 연결이 어긋난 곳을 찾으려는 것이다.
"""

import inspect

import pytest

from app.v3 import service
from tests.v3 import wiring

EDGES = (
    "app/v3/api.py",
    "app/v3/devserver.py",
    "app/v3/vendor_service.py",
    "app/v3/mcp_server.py",
    "app/v3/sql.py",
    "app/v3/tracing.py",
    "app/v3/agents/maf.py",
    "scripts/v3/register_foundry_agents.py",
    "scripts/v3/ingest_laws.py",
    "scripts/v3/index_evidence.py",
)
# 계약 8장에는 있는데 api.py(FastAPI)에 아직 없는 경로. devserver에는 있다.
# api.py는 다른 작업이 고치고 있어 여기서는 적어만 둔다. 경로가 생기면 이 목록에서 지운다.
KNOWN_API_GAPS = {("GET", "/health"), ("POST", "/intake/documents/confirm")}
# 경로가 부르지 않는 서비스 함수: 시드는 서버가 뜰 때, 업체 진입점은 vendor_service가 부른다.
NOT_ROUTED = {"seed", "handle_vendor_message"}


def public_service_functions():
    return {
        name
        for name, value in vars(service).items()
        if inspect.isfunction(value)
        and value.__module__ == service.__name__
        and not name.startswith("_")
    }


def service_targets(path, via=None):
    return {
        call.target.rsplit(".", 1)[1]
        for call in wiring.calls_in(path)
        if call.target.startswith("app.v3.service.") and (via is None or call.via == via)
    }


def test_ast_signature_matches_the_real_one():
    """ast로 만든 서명이 실제 함수의 서명과 같은 모양인지(도우미 자체의 검사)."""
    known = wiring.definitions("app.v3.service")
    for name in public_service_functions():
        real = inspect.signature(getattr(service, name))
        made = known.signatures[name]
        assert [(p.name, p.kind) for p in made.parameters.values()] == [
            (p.name, p.kind) for p in real.parameters.values()
        ], name
        assert [p.default is inspect.Parameter.empty for p in made.parameters.values()] == [
            p.default is inspect.Parameter.empty for p in real.parameters.values()
        ], name
    made_app = known.signatures["App"]
    real_app = inspect.signature(service.App)
    assert list(made_app.parameters) == list(real_app.parameters)


def test_helper_catches_a_wrong_call(tmp_path):
    """인자 수가 틀리거나 없는 함수를 부르면 잡아낸다."""
    bad = tmp_path / "bad_edge.py"
    bad.write_text(
        "from app.v3 import service\n"
        "from app.v3.judge.toolbox import call_tool, no_such_tool\n"
        "async def a(call):\n"
        "    await call(service.get_view, 'ses_1')\n"  # viewer가 빠졌다
        "    service.no_such_function(1)\n"
        "    service.approve(None, 'ses_1', 'th_1', 'org', 'extra')\n"
        "    call_tool('x', {}, registry=None, org=None, today='')\n",
        encoding="utf-8",
    )
    problems = "\n".join(wiring.check_calls(bad))
    assert "get_view" in problems and "no_such_function" in problems
    assert "approve" in problems and "no_such_tool" in problems and "call_tool" in problems
    assert len(wiring.check_calls(bad)) == 5


@pytest.mark.parametrize("path", EDGES)
def test_edge_calls_match_definitions(path):
    if not (wiring.ROOT / path).is_file():
        pytest.skip(f"{path}가 아직 없다")
    assert wiring.check_calls(path) == []


def test_api_and_devserver_call_the_same_service_functions():
    api = service_targets("app/v3/api.py", via="call")
    dev = service_targets("app/v3/devserver.py") - {"App", "ServiceError", "seed"}
    public = public_service_functions()
    assert api <= public and dev <= public
    assert dev == public - NOT_ROUTED  # devserver는 서비스 함수를 모두 잇는다
    # api.py에 아직 없는 경로가 부를 함수만 빠져 있어야 한다
    assert dev - api == ({"confirm_document"} if KNOWN_API_GAPS else set())


def test_vendor_service_and_mcp_server_call_their_entry_points():
    vendor = {(c.target, c.via, c.positional) for c in wiring.calls_in("app/v3/vendor_service.py")}
    # call(service.handle_vendor_message, vendor_org_id, message, context) -> fn(app, 3개)
    assert ("app.v3.service.handle_vendor_message", "call", 4) in vendor
    mcp = {c.target: c for c in wiring.calls_in("app/v3/mcp_server.py")}
    tool_call = mcp["app.v3.judge.toolbox.call_tool"]
    assert tool_call.positional == 2 and set(tool_call.keywords) == {"registry", "org_id", "today"}
    # api.py에서 가져다 쓰는 이름이 실제로 있다
    api_names = wiring.definitions("app.v3.api").names
    assert {"ApiError", "call", "guarded", "json_object", "set_state", "get_state"} <= api_names


def test_service_app_options_used_by_edges_exist():
    options = set(inspect.signature(service.App).parameters)
    for path in ("app/v3/api.py", "app/v3/devserver.py"):
        for call in wiring.calls_in(path):
            if call.target == "app.v3.service.App":
                assert call.keywords is None or set(call.keywords) <= options, (path, call)


# ── 경로 목록 ────────────────────────────────────────────────────────
def test_contract_routes_are_read():
    routes = wiring.contract_routes()
    assert ("POST", "/demo/reset") in routes and ("GET", "/evidence/{}") in routes
    assert ("GET", "/evidence") in routes and ("PUT", "/orgs/{}/streams/{}") in routes
    assert ("POST", "/intake/documents/confirm") in routes and ("GET", "/health") in routes
    assert len(routes) == 24


def test_devserver_routes_equal_the_contract():
    assert wiring.match_routes("app/v3/devserver.py") == wiring.contract_routes()


def test_api_routes_equal_the_contract_except_known_gaps():
    contract, api = wiring.contract_routes(), wiring.fastapi_routes("app/v3/api.py")
    assert api - contract == set(), "계약에 없는 경로가 api.py에 있다(계약 8장에 적는다)"
    assert contract - api <= KNOWN_API_GAPS, "계약의 경로가 api.py에 없다"


def test_api_prefix_matches_devserver():
    tree = wiring.parse("app/v3/api.py")
    source = (wiring.ROOT / "app/v3/api.py").read_text(encoding="utf-8")
    assert 'APIRouter(prefix="/api/v3"' in source and tree is not None
    assert 'PREFIX = "/api/v3"' in (wiring.ROOT / "app/v3/devserver.py").read_text(encoding="utf-8")
