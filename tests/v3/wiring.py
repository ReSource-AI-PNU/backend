"""가장자리 파일을 import하지 않고 소스(ast)만 읽어 연결을 대조하는 도우미.

fastapi, sqlalchemy, mcp가 없는 곳에서도 api.py, sql.py, mcp_server.py가 부르는 함수의 이름과
인자가 실제 정의와 맞는지 확인하려는 것이다. 서명은 정의의 ast에서 만든다(대상 모듈도
import하지 않는다).
"""

from __future__ import annotations

import ast
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def module_path(module: str) -> Path | None:
    """app.v3.service -> app/v3/service.py. 저장소 안에 없으면 None."""
    base = ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def parse(path: str | Path) -> ast.Module:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def signature_of(node: ast.FunctionDef | ast.AsyncFunctionDef, *, drop_self: bool = False):
    """ast 함수 정의를 inspect.Signature로 바꾼다(주석과 기본값의 내용은 버린다)."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    params = []
    for index, (arg, default) in enumerate(zip(positional, defaults, strict=True)):
        kind = (
            inspect.Parameter.POSITIONAL_ONLY
            if index < len(args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        params.append(
            inspect.Parameter(
                arg.arg, kind, default=inspect.Parameter.empty if default is None else ...
            )
        )
    if drop_self and params:
        params = params[1:]
    if args.vararg:
        params.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        params.append(
            inspect.Parameter(
                arg.arg,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if default is None else ...,
            )
        )
    if args.kwarg:
        params.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(params)


def shape_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, str, str | None]]:
    """(이름, 종류, 기본값 소스) 목록. 두 구현의 서명이 같은지 비교할 때 쓴다."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    out = [
        (arg.arg, "positional", ast.unparse(default) if default is not None else None)
        for arg, default in zip(positional, defaults, strict=True)
    ]
    if args.vararg:
        out.append((args.vararg.arg, "var_positional", None))
    out += [
        (arg.arg, "keyword_only", ast.unparse(default) if default is not None else None)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    ]
    if args.kwarg:
        out.append((args.kwarg.arg, "var_keyword", None))
    return out


@dataclass
class Definitions:
    """모듈 하나의 최상위 정의."""

    module: str
    names: set[str] = field(default_factory=set)
    signatures: dict[str, inspect.Signature] = field(default_factory=dict)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def definitions(module: str) -> Definitions | None:
    path = module_path(module)
    if path is None:
        return None
    found = Definitions(module)
    for node in _top_level(parse(path)):
        if isinstance(node, _FUNCS):
            found.names.add(node.name)
            found.signatures[node.name] = signature_of(node)
        elif isinstance(node, ast.ClassDef):
            found.names.add(node.name)
            found.classes[node.name] = node
            init = next(
                (n for n in node.body if isinstance(n, _FUNCS) and n.name == "__init__"), None
            )
            if init is not None:
                found.signatures[node.name] = signature_of(init, drop_self=True)
            elif _is_dataclass(node):
                found.signatures[node.name] = _dataclass_signature(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        found.names.add(name.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found.names.add((alias.asname or alias.name).split(".")[0])
    return found


def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name == "dataclass":
            return True
    return False


def _dataclass_signature(node: ast.ClassDef) -> inspect.Signature:
    """dataclass의 필드를 순서대로 인자로 본다(상속과 kw_only는 다루지 않는다)."""
    params = [
        inspect.Parameter(
            item.target.id,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=inspect.Parameter.empty if item.value is None else ...,
        )
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    ]
    return inspect.Signature(params)


def _top_level(tree: ast.Module):
    """최상위 문장. try/if 안에 든 정의도 모듈 수준이므로 함께 본다."""
    stack = list(tree.body)
    while stack:
        node = stack.pop(0)
        yield node
        if isinstance(node, ast.Try):
            stack = [*node.body, *node.orelse, *node.finalbody] + stack
            for handler in node.handlers:
                stack = list(handler.body) + stack
        elif isinstance(node, ast.If):
            stack = [*node.body, *node.orelse] + stack


def class_methods(module: str, class_name: str) -> dict[str, ast.FunctionDef]:
    found = definitions(module)
    assert found is not None and class_name in found.classes, f"{module}.{class_name}이 없다"
    return {node.name: node for node in found.classes[class_name].body if isinstance(node, _FUNCS)}


@dataclass(frozen=True)
class Call:
    target: str  # "app.v3.service.reset_demo"
    line: int
    positional: int | None  # None이면 *args가 있어 셀 수 없다
    keywords: tuple[str, ...] | None  # None이면 **kwargs가 있다
    via: str  # "direct" | "call"


def _count(args: list[ast.expr], keywords: list[ast.keyword]):
    positional = None if any(isinstance(a, ast.Starred) for a in args) else len(args)
    names = None if any(k.arg is None for k in keywords) else tuple(str(k.arg) for k in keywords)
    return positional, names


def imports_of(tree: ast.Module, prefix: str = "app.") -> tuple[dict[str, str], dict[str, str]]:
    """(모듈 별칭 -> 모듈, 이름 -> "모듈.이름"). 함수 안의 import도 본다."""
    modules: dict[str, str] = {}
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if not node.module.startswith(prefix):
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                full = f"{node.module}.{alias.name}"
                if module_path(full) is not None:
                    modules[local] = full
                else:
                    names[local] = full
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(prefix) and alias.asname:
                    modules[alias.asname] = alias.name
    return modules, names


def calls_in(path: str | Path, *, indirect: tuple[str, ...] = ("call",)) -> list[Call]:
    """파일이 저장소 안 모듈의 함수를 부르는 곳을 모은다.

    - 직접 호출: `service.reset_demo(app)`, `call_tool(name, ...)`
    - 간접 호출: `call(service.reset_demo, a, b)`. api.py의 call은 fn(app, *args)로 부르므로
      위치 인자가 하나 더 있는 것으로 센다.
    """
    tree = parse(path)
    modules, names = imports_of(tree)
    found: list[Call] = []

    def resolve(node: ast.expr) -> str | None:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            return f"{modules[node.value.id]}.{node.attr}"
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = resolve(node.func)
        if target is not None:
            found.append(Call(target, node.lineno, *_count(node.args, node.keywords), "direct"))
        if isinstance(node.func, ast.Name) and node.func.id in indirect and node.args:
            target = resolve(node.args[0])
            if target is not None:
                positional, keywords = _count(node.args[1:], node.keywords)
                if positional is not None:
                    positional += 1  # app
                found.append(Call(target, node.lineno, positional, keywords, "call"))
    return found


def check_calls(path: str | Path, *, indirect: tuple[str, ...] = ("call",)) -> list[str]:
    """어긋난 곳을 사람이 읽을 문장 목록으로 돌려준다. 비어 있으면 모두 맞는다."""
    problems: list[str] = []
    tree = parse(path)
    modules, names = imports_of(tree)
    for local, full in sorted(names.items()):
        module, _, name = full.rpartition(".")
        known = definitions(module)
        if known is None:
            problems.append(f"{path}: {module} 모듈이 없다 ({local})")
        elif name not in known.names:
            problems.append(f"{path}: {module}에 {name}이 없다")
    for local, full in sorted(modules.items()):
        if module_path(full) is None:
            problems.append(f"{path}: {full} 모듈이 없다 ({local})")
    for call in calls_in(path, indirect=indirect):
        module, _, name = call.target.rpartition(".")
        known = definitions(module)
        if known is None:
            continue  # 위에서 이미 적었다
        if name not in known.names:
            problems.append(f"{path}:{call.line}: {module}에 {name}이 없다")
            continue
        signature = known.signatures.get(name)
        if signature is None or call.positional is None or call.keywords is None:
            continue  # 서명을 알 수 없거나(dataclass 등) 인자를 셀 수 없다
        try:
            signature.bind(*([None] * call.positional), **dict.fromkeys(call.keywords))
        except TypeError as error:
            problems.append(
                f"{path}:{call.line}: {name}{signature} 를 위치 인자 {call.positional}개, "
                f"키워드 {list(call.keywords)}로 불렀다 ({error})"
            )
    return problems


# ── 경로 목록 ────────────────────────────────────────────────────────
_PARAM = re.compile(r"\{[^}]*\}")
_ROUTE = re.compile(r"`(GET|POST|PUT|DELETE|PATCH) (/[^`\s?]*)")


def normalize(path: str) -> str:
    """경로 변수 이름을 지운다: /orgs/{orgId}/streams -> /orgs/{}/streams"""
    return _PARAM.sub("{}", path).rstrip("/") or "/"


def contract_routes(doc: str | Path = "docs/v3/CONTRACT.md") -> set[tuple[str, str]]:
    """계약 8장에 적힌 (메서드, 경로). 표와 "구현하면서 더한 주소"를 함께 읽는다."""
    text = (ROOT / doc).read_text(encoding="utf-8")
    start = text.index("## 8. HTTP API")
    end = text.find("\n## ", start + 1)
    section = text[start : end if end != -1 else len(text)]
    return {(method, normalize(path)) for method, path in _ROUTE.findall(section)}


def contract_table_routes(
    title: str, doc: str | Path = "docs/v3/CONTRACT.md", after: str = ""
) -> set[tuple[str, str]]:
    """계약의 한 절(title, after 다음에 처음 나오는 것)에 있는 표의 첫 칸에 적힌 (메서드, 경로)만."""
    text = (ROOT / doc).read_text(encoding="utf-8")
    start = text.index(title, text.index(after) if after else 0)
    end = text.find("\n#", start + len(title))
    rows = [
        line.split("|")[1]
        for line in text[start : end if end != -1 else len(text)].splitlines()
        if line.startswith("| `")
    ]
    return {(method, normalize(path)) for row in rows for method, path in _ROUTE.findall(row)}


def fastapi_routes(path: str | Path, router: str = "router") -> set[tuple[str, str]]:
    """`@router.get("/path")` 꼴의 장식자에서 (메서드, 경로)를 모은다."""
    found = set()
    for node in ast.walk(parse(path)):
        if not isinstance(node, _FUNCS):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == router
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                found.add((decorator.func.attr.upper(), normalize(str(decorator.args[0].value))))
    return found


def match_routes(path: str | Path, function: str = "route") -> set[tuple[str, str]]:
    """devserver.route의 `case ("GET", ["orgs", org_id, "streams"])`에서 (메서드, 경로)를 모은다."""
    found = set()
    for node in ast.walk(parse(path)):
        if not (isinstance(node, _FUNCS) and node.name == function):
            continue
        for match in (n for n in ast.walk(node) if isinstance(n, ast.Match)):
            for case in match.cases:
                pattern = case.pattern
                if not (isinstance(pattern, ast.MatchSequence) and len(pattern.patterns) == 2):
                    continue
                method, parts = pattern.patterns
                if not (
                    isinstance(method, ast.MatchValue) and isinstance(parts, ast.MatchSequence)
                ):
                    continue
                segments = []
                for part in parts.patterns:
                    if isinstance(part, ast.MatchValue) and isinstance(part.value, ast.Constant):
                        segments.append(str(part.value.value))
                    else:  # 이름으로 받는 자리, *rest
                        segments.append("{}")
                found.add((str(ast.literal_eval(method.value)), "/" + "/".join(segments)))
    return found
