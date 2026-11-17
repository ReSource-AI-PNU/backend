"""CI에서 처음 도는 가장자리 코드를 소스(ast, 글)로 대조한다(순수 파이썬).

fastapi·sqlalchemy·alembic·ortools를 불러오지 않는다. 정적 검토(docs/v3/static-review.md)에서 고친
곳이 다시 어긋나지 않게 하려는 것이다.
"""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def tree(path: str) -> ast.Module:
    return ast.parse(read(path))


def requirement_name(requirement: str) -> str:
    return re.split(r"[\[<>=!~; ]", requirement, maxsplit=1)[0].lower().replace("_", "-")


# ── 의존성: pyproject와 uv.lock ──────────────────────────────────────
def test_lockfile_lists_every_declared_dependency():
    """CI는 uv sync --locked로 설치한다. pyproject에 더한 의존성이 uv.lock에 없으면 설치에서 멈춘다."""
    project = tomllib.loads(read("pyproject.toml"))["project"]
    lock = tomllib.loads(read("uv.lock"))
    (own,) = [p for p in lock["package"] if p["name"] == project["name"]]
    locked = {p["name"] for p in lock["package"]}

    declared = {requirement_name(item) for item in project["dependencies"]}
    assert {d["name"] for d in own["dependencies"]} == declared
    assert declared <= locked
    extras = project["optional-dependencies"]
    assert set(own["optional-dependencies"]) == set(extras)
    for extra, items in extras.items():
        names = {requirement_name(item) for item in items}
        assert {d["name"] for d in own["optional-dependencies"][extra]} == names, extra
        assert names <= locked, extra
    # 범위(>=, <)도 같아야 --locked가 통과한다
    specifiers = {
        (item["name"], item.get("marker", "")): item.get("specifier", "")
        for item in own["metadata"]["requires-dist"]
    }
    for item in project["dependencies"]:
        spec = re.sub(r"^[A-Za-z0-9_.\-]+(\[[^\]]*\])?", "", item).strip()
        assert specifiers[(requirement_name(item), "")] == spec, item


def test_ci_installs_the_extras_the_v3_tests_need():
    workflow = read(".github/workflows/ci.yml")
    assert "uv sync --extra dev --extra mcp-server --locked" in workflow
    assert "uv run ruff check ." in workflow and "uv run pytest -q" in workflow


# ── 마이그레이션 ─────────────────────────────────────────────────────
def assigned(module: ast.Module, name: str):
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name}이 없다")


def test_migration_chain_is_unbroken():
    files = sorted((ROOT / "migrations/versions").glob("0*.py"))
    assert [f.name[:4] for f in files] == [f"{i:04d}" for i in range(1, len(files) + 1)]
    previous = None
    for file in files:
        module = ast.parse(file.read_text(encoding="utf-8"))
        assert assigned(module, "down_revision") == previous, file.name
        previous = assigned(module, "revision")
        assert previous == file.stem, file.name
    assert previous == "0004_v3"


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def is_true(call: ast.Call, keyword: str) -> bool:
    return any(k.arg == keyword and ast.literal_eval(k.value) is True for k in call.keywords)


def model_tables() -> dict[str, dict[str, tuple[str, bool]]]:
    """app/v3/sql.py의 모델: {테이블: {열: (타입 이름, 기본 키)}}."""
    tables = {}
    for node in tree("app/v3/sql.py").body:
        if not isinstance(node, ast.ClassDef) or "Base" not in [call_name(b) for b in node.bases]:
            continue
        name, columns = None, {}
        for item in node.body:
            if isinstance(item, ast.Assign) and call_name(item.targets[0]) == "__tablename__":
                name = ast.literal_eval(item.value)
            if isinstance(item, ast.AnnAssign) and call_name(item.value) == "mapped_column":
                columns[item.target.id] = (
                    call_name(item.value.args[0]),
                    is_true(item.value, "primary_key"),
                )
        tables[name] = columns
    return tables


def migration_tables() -> dict[str, dict[str, tuple[str, bool]]]:
    tables = {}
    for node in ast.walk(tree("migrations/versions/0004_v3.py")):
        if isinstance(node, ast.Call) and call_name(node) == "create_table":
            columns = {}
            for column in node.args[1:]:
                assert call_name(column) == "Column"
                columns[ast.literal_eval(column.args[0])] = (
                    call_name(column.args[1]),
                    is_true(column, "primary_key"),
                )
            tables[ast.literal_eval(node.args[0])] = columns
    return tables


def test_v3_models_and_migration_have_the_same_columns():
    """모델(sql.py)과 마이그레이션(0004)이 같은 테이블·열·기본 키를 만든다."""
    models = model_tables()
    assert set(models) == {"v3_evidence", "v3_records", "v3_events"}
    assert models == migration_tables()
    downgrade = read("migrations/versions/0004_v3.py").split("def downgrade", 1)[1]
    assert all(f'drop_table("{name}")' in downgrade for name in models)


def test_sql_store_never_changes_json_in_place():
    """JSON 열은 새 값을 대입해야 저장된다. row.payload[...] = …이나 .append 같은 제자리 수정이 없다."""
    for node in ast.walk(tree("app/v3/sql.py")):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript):
                assert "row" not in ast.unparse(target.value), ast.unparse(node)
        changes = ("append", "update", "setdefault", "pop", "extend", "clear")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in changes
        ):
            owner = ast.unparse(node.func.value)
            assert ".payload" not in owner and ".access" not in owner, ast.unparse(node)


# ── api.py: FastAPI가 서명을 읽는 방식과 맞는지 ──────────────────────
def routes(path: str, owner: str):
    for node in tree(path).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [ast.unparse(d) for d in node.decorator_list]
        if decorators and decorators[0].startswith(f"{owner}."):
            yield node, decorators


def test_api_handlers_only_take_path_parameters_and_the_request():
    """경로 변수와 request 말고 다른 매개변수가 있으면 FastAPI가 질의 매개변수로 읽어 422를 낸다."""
    found = list(routes("app/v3/api.py", "router"))
    assert len(found) >= 25
    for node, decorators in found:
        # router 장식자가 맨 바깥, guarded가 그 안쪽이어야 오류가 계약의 모양으로 나간다
        assert decorators[1:] == ["guarded"], node.name
        assert isinstance(node, ast.AsyncFunctionDef), node.name
        route = ast.literal_eval(node.decorator_list[0].args[0])
        in_path = {part.split(":")[0] for part in re.findall(r"\{([^}]+)\}", route)}
        names = [a.arg for a in node.args.args]
        assert set(names) == in_path | {"request"}, node.name
        assert not node.args.kwonlyargs and node.args.vararg is None and node.args.kwarg is None
        assert node.returns is None, node.name  # 반환 주석이 있으면 응답 모델로 걸러진다


def test_guarded_keeps_the_handler_signature():
    """functools.wraps가 없으면 FastAPI가 (*args, **kwargs)를 질의 매개변수로 읽는다."""
    source = read("app/v3/api.py")
    guarded = source.split("def guarded", 1)[1].split("\n# ──", 1)[0]
    assert "@functools.wraps(handler)" in guarded and "async def wrapped" in guarded


def test_unreadable_multipart_is_a_client_error_on_both_servers():
    api, dev = read("app/v3/api.py"), read("app/v3/devserver.py")
    assert "from starlette.exceptions import HTTPException" in api
    assert re.search(r'except HTTPException:.*?"INVALID_MULTIPART",[^\n]*400', api, re.DOTALL)
    assert re.search(r'"INVALID_MULTIPART",[^\n]*400\)', dev)


def test_fixed_paths_come_before_path_converters():
    """/evidence/{id:path}가 /evidence보다 먼저 등록되면 검색 요청을 가로챈다."""
    order = [
        ast.literal_eval(node.decorator_list[0].args[0])
        for node, _ in routes("app/v3/api.py", "router")
    ]
    assert order.index("/evidence") < order.index("/evidence/{evidence_id:path}")


# ── OR-Tools: 9.15의 cp_model 이름 ───────────────────────────────────
def test_cp_sat_uses_the_snake_case_api():
    """ortools 9.15의 CpModel·CpSolver 메서드 이름(new_bool_var, add, maximize, minimize, solve, value)."""
    function = next(
        node
        for node in tree("app/v3/judge/assign.py").body
        if isinstance(node, ast.FunctionDef) and node.name == "cp_sat_pick"
    )
    used = {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and call_name(node.func.value) in ("model", "solver", "cp_model")
    }
    assert used == {
        "CpModel",
        "CpSolver",
        "new_bool_var",
        "add",
        "maximize",
        "minimize",
        "solve",
        "value",
    }


# ── 배포 파일 ────────────────────────────────────────────────────────
def settings_fields() -> set[str]:
    (settings,) = [
        node
        for node in tree("app/core/config.py").body
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    ]
    return {
        item.target.id.upper()
        for item in settings.body
        if isinstance(item, ast.AnnAssign) and item.target.id != "model_config"
    }


def test_migration_step_runs_where_the_image_keeps_alembic_ini():
    """ACR 작업 단계의 기본 작업 폴더는 /workspace다. alembic.ini가 있는 WORKDIR로 맞춰야 한다."""
    dockerfile = read("Dockerfile")
    (workdir,) = re.findall(r"^WORKDIR (\S+)$", dockerfile, re.MULTILINE)
    assert "COPY alembic.ini ./" in dockerfile and "COPY migrations ./migrations" in dockerfile
    task = read("infra/deploy.sh").split("<<'YAML'", 1)[1].split("\nYAML", 1)[0]
    assert "alembic upgrade head" in task and f"workingDirectory: {workdir}" in task


def test_start_script_roles_match_the_modules():
    script = read("scripts/v3/start.sh")
    assert "uvicorn app.main:app" in script and "uvicorn app.v3.vendor_service:app" in script
    assert "python -m app.v3.mcp_server" in script and "alembic upgrade head" in script
    assert (ROOT / "app/v3/vendor_service.py").exists() and (ROOT / "app/v3/mcp_server.py").exists()


def test_app_service_settings_are_names_the_app_reads():
    bicep = read("infra/appservice.bicep")
    names = set(re.findall(r"name: '([A-Z][A-Z0-9_]+)'", bicep))
    platform = {
        "WEBSITES_PORT",
        "WEBSITES_ENABLE_APP_SERVICE_STORAGE",
        "DOCKER_REGISTRY_SERVER_URL",
        "DOCKER_REGISTRY_SERVER_USERNAME",
        "DOCKER_REGISTRY_SERVER_PASSWORD",
        "APP_ROLE",  # scripts/v3/start.sh
        "MCP_SERVICE_KEY",  # app/v3/mcp_server.py
        "PORT",  # 프런트(Next.js)
        "HOSTNAME",
    }
    assert names - platform <= settings_fields(), sorted(names - platform - settings_fields())
    assert {"V3_AUTH", "V3_DEMO_OPEN", "V3_ORG_TOKENS"} <= names
    assert "SecretName=v3-org-tokens" in bicep and "param v3Auth string = 'header'" in bicep
    script = read("infra/deploy.sh")
    assert '"v3Auth=$V3_AUTH"' in script and '"v3DemoOpen=$V3_DEMO_OPEN"' in script
    # 사전·목록 설정은 빈 문자열을 주면 pydantic-settings가 JSON으로 읽다 실패한다
    complex_fields = {"CORS_ORIGINS", "V3_VENDOR_AGENTS", "PLATFORM_STAGE_ENDPOINTS"}
    for name in complex_fields & names:
        value = re.search(rf"name: '{name}'\s+value: (.+)", bicep).group(1)
        assert value.strip() not in ("''", '""'), name
