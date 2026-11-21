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
# 계약 8장에는 있는데 api.py(FastAPI)에 아직 없는 경로. 지금은 없다(두 서버의 경로가 같다).
KNOWN_API_GAPS: set[tuple[str, str]] = set()
# 경로가 부르지 않는 서비스 함수: 시드는 서버가 뜰 때, 업체 진입점은 vendor_service가 부른다.
# execute_run은 두 서버가 POST /sessions/{id}/run?mode=async 뒤에 띄운 스레드가 부른다.
NOT_ROUTED = {"seed", "handle_vendor_message", "execute_run"}


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
    assert dev == api  # 두 서버가 같은 서비스 함수를 잇는다


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
    assert ("POST", "/intake/media") in routes
    assert ("GET", "/sessions") in routes and ("GET", "/sessions/{}/stream") in routes
    assert ("GET", "/trust") in routes
    assert ("GET", "/sessions/{}/quote-request") in routes
    assert ("GET", "/orgs/{}/report") in routes
    assert ("POST", "/intake/criteria/confirm") in routes
    assert ("POST", "/intake/scan") in routes and ("GET", "/orgs/{}/scans/{}/points") in routes
    assert ("PUT", "/sessions/{}/outcomes/{}") in routes  # 검수 기록 정정(새 반입 등록과 따로)
    assert ("POST", "/sessions/{}/permit-checks") in routes
    assert ("GET", "/orgs/{}/memory") in routes and ("PUT", "/orgs/{}/memory") in routes
    assert ("GET", "/sessions/{}/adaptive") in routes
    assert ("POST", "/sessions/{}/scenarios/preview") in routes
    assert ("POST", "/sessions/{}/scenarios/apply") in routes
    assert ("POST", "/vendors/{}/onboarding/confirm") in routes
    assert ("GET", "/vendors/{}/permit-catalog") in routes
    assert ("DELETE", "/vendors/{}/mcp-keys/{}") in routes  # 업체 MCP 연결 키 3개
    assert ("GET", "/sessions/{}/complex-plan") in routes  # 같은 단지 평면도
    assert ("GET", "/survey") in routes and ("POST", "/survey/weights") in routes
    assert len(routes) == 60  # 업체 연결 8개, 조건 비교 3개, 같은 단지 평면도, 설문 후속 7개 포함


def test_devserver_routes_equal_the_contract():
    assert wiring.match_routes("app/v3/devserver.py") == wiring.contract_routes()


def test_account_routes_equal_the_contract_table():
    """9장 "경로" 표의 경로와 accounts_api.py(bff 모드 전용)의 경로가 같다. devserver는 bff를 쓰지 않는다."""
    table = wiring.contract_table_routes("### 경로", after="## 9. 계정과 조직")
    assert ("DELETE", "/accounts/me") in table and ("PATCH", "/orgs/{}") in table
    assert wiring.fastapi_routes("app/v3/accounts_api.py") == table
    assert table & wiring.contract_routes() == set()  # 8장과 겹치지 않는다
    assert table & wiring.match_routes("app/v3/devserver.py") == set()


def test_api_routes_equal_the_contract():
    contract, api = wiring.contract_routes(), wiring.fastapi_routes("app/v3/api.py")
    assert KNOWN_API_GAPS == set()
    assert api - contract == set(), "계약에 없는 경로가 api.py에 있다(계약 8장에 적는다)"
    assert contract - api == set(), "계약의 경로가 api.py에 없다"


# ── 요청 오류 코드 ───────────────────────────────────────────────────
REQUEST_EDGES = ("app/v3/api.py", "app/v3/devserver.py", "app/v3/vendor_service.py")
OLD_CODES = {"INVALID_REQUEST", "FILE_TOO_LARGE"}


def string_constants(path):
    import ast

    return {
        node.value
        for node in ast.walk(wiring.parse(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


@pytest.mark.parametrize("path", REQUEST_EDGES)
def test_edges_do_not_use_the_old_request_error_codes(path):
    assert string_constants(path) & OLD_CODES == set()


@pytest.mark.parametrize("path", REQUEST_EDGES[:2])
def test_request_errors_come_from_one_place(path):
    """요청 오류(JSON, 본문 모양, 파일, 크기)는 두 서버가 httpio의 같은 함수로 낸다."""
    from app.v3 import httpio

    # JSON·파일·크기 오류 코드를 가장자리에서 글자로 다시 적지 않는다
    own = string_constants(path) & (set(httpio.REQUEST_ERRORS) - {httpio.INVALID_BODY})
    assert own == set(), own
    used = {c.target.rsplit(".", 1)[1] for c in wiring.calls_in(path) if ".httpio." in c.target}
    assert {"parse_object", "answer_body", "required", "check_upload", "file_required"} <= used


def test_contract_lists_the_request_error_codes():
    from app.v3 import httpio

    text = (wiring.ROOT / "docs/v3/CONTRACT.md").read_text(encoding="utf-8")
    section = text[text.index("## 8. HTTP API") :]
    for code, status in httpio.REQUEST_ERRORS.items():
        assert f"| `{code}` | {status} |" in section, code


# ── mcp 버전 ─────────────────────────────────────────────────────────
def test_mcp_range_stays_on_the_sdk_line_the_server_is_written_for():
    """mcp_server.py는 SDK 1.x의 저수준 API(장식자)를 쓴다. 옮기기 전에 상한을 풀면 여기서 걸린다."""
    import ast

    tree = wiring.parse("app/v3/mcp_server.py")
    decorators = {
        d.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for d in node.decorator_list
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
    }
    uses_v1 = {"list_tools", "call_tool"} <= decorators
    project = (wiring.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pinned = '"mcp>=1.10,<2"' in project
    assert uses_v1 == pinned, "mcp_server.py의 API와 pyproject의 mcp 범위가 어긋난다"
    readme = (wiring.ROOT / "README.md").read_text(encoding="utf-8")
    assert "MCP 서버 이미지와 Agent Framework 이미지는 분리" in readme
    assert "mcp>=2.2,<3" in readme and "mcp>=1.10,<2" in readme


def test_api_prefix_matches_devserver():
    tree = wiring.parse("app/v3/api.py")
    source = (wiring.ROOT / "app/v3/api.py").read_text(encoding="utf-8")
    assert 'APIRouter(prefix="/api/v3"' in source and tree is not None
    assert 'PREFIX = "/api/v3"' in (wiring.ROOT / "app/v3/devserver.py").read_text(encoding="utf-8")
