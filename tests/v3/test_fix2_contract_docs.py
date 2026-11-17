"""계약 문서(CONTRACT 7·8장)가 코드와 맞는지: 메시지를 쓴 구현의 이름과 오류 코드 전체 목록."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.v3 import httpio
from app.v3.a2a import guardrails as rails
from app.v3.a2a import term_words
from app.v3.agents import draft, orchestrator
from app.v3.agents.llm import FoundryChat, OllamaChat
from app.v3.agents.policy_rule import RulePolicy
from tests.v3.runtime_fixtures import approve_all, make_app, to_approval

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = (ROOT / "docs/v3/CONTRACT.md").read_text(encoding="utf-8")
ERROR_CLASSES = ("ServiceError", "OrchestratorError", "FeedbackError", "ApiError")


def section(title: str) -> str:
    start = CONTRACT.index(title)
    following = re.search(r"\n#{2,3} ", CONTRACT[start + len(title) :])
    end = start + len(title) + following.start() if following else len(CONTRACT)
    return CONTRACT[start:end]


def raised_codes() -> set[str]:
    """app/v3 코드가 오류로 내는 코드(글자 그대로 적힌 것)."""
    found: set[str] = set()
    for path in sorted((ROOT / "app/v3").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            first = node.args[0]
            if name in ERROR_CLASSES and isinstance(first, ast.Constant):
                found.add(str(first.value))
    return found


def test_every_error_code_the_code_raises_is_in_the_table():
    table = section("### 오류 코드 전체 목록")
    codes = raised_codes() | set(httpio.REQUEST_ERRORS)
    codes |= {httpio.TOKEN_REQUIRED, httpio.INVALID_TOKEN, "INTERNAL"}
    assert len(codes) > 30
    missing = sorted(code for code in codes if f"`{code}`" not in table)
    assert missing == []
    for named in ("INVALID_FIELD", "PERMIT_MISMATCH", "UNSUPPORTED_MEDIA", "APPROVAL_BLOCKED"):
        assert f"`{named}`" in table
    # 표에 적힌 코드는 실제로 있는 코드다(지어낸 코드를 적지 않는다)
    listed = set(re.findall(r"`([A-Z][A-Z_]{3,})`", table)) - {"V3_DEMO_OPEN"}
    assert listed <= codes, sorted(listed - codes)


def test_block_codes_table_matches_the_code():
    table = section("### 승인을 막는 사유 코드")
    for code in orchestrator.BLOCK_CODES:
        assert f"`{code}`" in table
    assert {"STREAM_CHANGED", "VENDOR_TERMS_CHANGED"} <= set(orchestrator.BLOCK_CODES)
    assert "blockedCodes" in table and "`APPROVAL_BLOCKED`" in table


def test_rejection_codes_table_matches_the_code():
    table = section("### 초안 거부 코드")
    for code in draft.PUBLIC_MESSAGES:
        assert f"`{code}`" in table, code
    assert f"`{term_words.CODE}`" in table and "term_words.py" in table
    guardrail = {"PRICE_BELOW_FLOOR", "PRICE_ABOVE_CEILING", "DAY_NOT_ALLOWED"}
    guardrail |= {"PICKUPS_BELOW_MINIMUM", "AUTO_DECLINE_CONDITION"}
    source = (ROOT / "app/v3/a2a/guardrails.py").read_text(encoding="utf-8")
    for code in guardrail:
        assert f'"{code}"' in source and f"`GUARDRAIL_{code}`" in table
    vendor = (ROOT / "app/v3/agents/vendor.py").read_text(encoding="utf-8")
    for code in set(re.findall(r'"(GUARDRAIL_[A-Z_]+)"', vendor)):
        assert f"`{code}`" in table, code
    assert rails.check_term_words.__doc__ and "UNBACKED_TERM" in rails.check_term_words.__doc__


def test_provider_table_lists_every_implementation_that_can_write_a_message():
    table = section("### 메시지를 쓴 구현 (`model.provider`)")
    real = {RulePolicy.provider, FoundryChat.provider, OllamaChat.provider, "code"}
    assert real == {"rule", "foundry", "ollama", "code"}
    for provider in real:
        assert f"| `{provider}` |" in table
    assert RulePolicy.model in table and "진행기(코드)" in table
    # 시연에서 실제로 나온 값도 이 표 안에 있다
    app = make_app()
    view = approve_all(app, to_approval(app))
    seen = {m["model"]["provider"] for t in view["threads"] for m in t["messages"]}
    seen |= {t["vendorModel"]["provider"] for t in view["threads"]}
    assert seen and seen <= real
