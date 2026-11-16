"""SqlStore·SqlRegistry가 메모리 구현과 같은 메서드를 같은 서명으로 갖는지 소스(ast)로 대조한다.

sql.py는 sqlalchemy가 없으면 import할 수 없어 test_sql.py가 건너뛰어진다. 여기서는 소스만 읽는다.
"""

import ast
import re

import pytest

from tests.v3 import wiring

STORE, SQL = "app.v3.store", "app.v3.sql"
REGISTRY = "app.v3.evidence.registry"
# 서비스가 hasattr로 확인하고 부르는 메서드(프로토콜 밖). SQL 구현에도 있어야 reset이 같게 돈다.
OPTIONAL_REGISTRY = {"clear"}


def public(methods):
    return {name: node for name, node in methods.items() if not name.startswith("_")}


@pytest.mark.parametrize("implementation", [(STORE, "InMemoryStore"), (SQL, "SqlStore")])
def test_store_implementations_follow_the_protocol(implementation):
    protocol = wiring.class_methods(STORE, "Store")
    assert len(protocol) >= 25
    methods = wiring.class_methods(*implementation)
    assert sorted(set(protocol) - set(methods)) == []
    for name, node in protocol.items():
        assert wiring.shape_of(methods[name]) == wiring.shape_of(node), name


def test_sql_store_has_everything_the_memory_store_has():
    memory = public(wiring.class_methods(STORE, "InMemoryStore"))
    sql = public(wiring.class_methods(SQL, "SqlStore"))
    protocol = wiring.class_methods(STORE, "Store")
    # snapshot은 파일 저장(JsonFileStore)만 쓰는 메모리 구현의 것이다
    assert sorted(set(memory) - set(sql)) == ["snapshot"]
    assert sorted(set(sql) - set(protocol)) == []


@pytest.mark.parametrize("implementation", [(REGISTRY, "InMemoryRegistry"), (SQL, "SqlRegistry")])
def test_registry_implementations_follow_the_protocol(implementation):
    protocol = wiring.class_methods(REGISTRY, "EvidenceRegistry")
    assert set(protocol) == {"put", "get", "exists", "list"}
    methods = wiring.class_methods(*implementation)
    for name, node in protocol.items():
        assert wiring.shape_of(methods[name]) == wiring.shape_of(node), name


def test_sql_registry_has_everything_the_memory_registry_has():
    memory = public(wiring.class_methods(REGISTRY, "InMemoryRegistry"))
    sql = public(wiring.class_methods(SQL, "SqlRegistry"))
    assert sorted(set(memory) - set(sql)) == []
    for name in OPTIONAL_REGISTRY:
        assert wiring.shape_of(sql[name]) == wiring.shape_of(memory[name]), name


def _method_calls(receiver: str) -> dict[str, set[str]]:
    """app/v3의 순수 모듈이 `<...>.store.x(` 또는 `store.x(` 꼴로 부르는 메서드 이름과 파일."""
    used: dict[str, set[str]] = {}
    for path in sorted((wiring.ROOT / "app/v3").rglob("*.py")):
        for node in ast.walk(wiring.parse(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = node.func.value
            name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
            if re.fullmatch(rf"_?{receiver}", name or ""):
                used.setdefault(node.func.attr, set()).add(path.name)
    return used


def test_code_only_calls_store_methods_in_the_protocol():
    protocol = set(wiring.class_methods(STORE, "Store"))
    used = _method_calls("store")
    assert len(used) >= 15  # 도우미가 실제로 호출을 찾고 있다
    assert {name: files for name, files in used.items() if name not in protocol} == {}


def test_code_only_calls_registry_methods_both_implementations_have():
    sql = set(public(wiring.class_methods(SQL, "SqlRegistry")))
    used = _method_calls("registry")
    assert {"put", "get", "list"} <= set(used)
    assert {name: files for name, files in used.items() if name not in sql} == {}
