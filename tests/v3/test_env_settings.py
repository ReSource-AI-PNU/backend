"""환경 변수 이름이 설정 모듈, .env.example, 읽는 코드에서 어긋나지 않는지 본다(순수 파이썬).

app/core/config.py는 pydantic이 있어야 불러올 수 있어 여기서는 소스(ast)로 필드 이름만 읽는다.
"""

import ast
from pathlib import Path

import pytest

from app.v3 import httpio

ROOT = Path(__file__).resolve().parents[2]
# 가장자리 작업에서 생긴 환경 변수. 설정 모듈의 필드 이름은 이것의 소문자다
V3_ENV = (
    "V3_LLM",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_FILE",
    "ANTHROPIC_MODEL",
    "V3_AUTH",
    "V3_ORG_TOKENS",
    "V3_ORG_TOKENS_FILE",
    "V3_CORS_ORIGINS",
    "V3_DISTANCE",
    "KAKAO_REST_API_KEY",
)
SECRET_ENV = ("ANTHROPIC_API_KEY", "V3_ORG_TOKENS", "KAKAO_REST_API_KEY")


def settings_fields():
    tree = ast.parse((ROOT / "app/core/config.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    return {
        node.target.id: node.value
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def test_settings_module_declares_every_v3_env_name_with_the_current_defaults():
    fields = settings_fields()
    assert not [name for name in V3_ENV if name.lower() not in fields]
    defaults = {
        name: ast.literal_eval(value)
        for name, value in fields.items()
        if isinstance(value, ast.Constant)
    }
    # 기본값은 지금 동작 그대로다: 규칙 정책, header 모드, 거리 제공자 없음
    assert defaults["v3_llm"] == "rule" and defaults["v3_auth"] == "header"
    assert defaults["v3_distance"] == "" and defaults["v3_cors_origins"] == ""
    assert all(defaults[name.lower()] == "" for name in SECRET_ENV)


def test_env_example_lists_every_name_and_carries_no_secret_value():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    assert not [name for name in V3_ENV if name not in values]
    assert all(values[name] == "" for name in SECRET_ENV)
    assert all(values[name] == "" for name in ("ANTHROPIC_API_KEY_FILE", "V3_ORG_TOKENS_FILE"))


def test_tokens_file_is_read_before_the_inline_value(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"org_hanbit": "test-hanbit-aaaaaaaaaaaa"}', encoding="utf-8")
    assert httpio.tokens_text("inline", "") == "inline" and httpio.tokens_text(None, None) is None
    auth = httpio.make_auth("token", httpio.tokens_text("", str(path)))
    assert auth.viewer({"Authorization": "Bearer test-hanbit-aaaaaaaaaaaa"}) == "org_hanbit"
    missing = str(tmp_path / "none.json")
    with pytest.raises(ValueError) as error:
        httpio.tokens_text("", missing)
    assert missing not in str(error.value)  # 경로는 문구에 싣지 않는다


def test_extra_origins_are_split_cleaned_and_checked():
    assert httpio.extra_origins(None) == [] and httpio.extra_origins(" , ") == []
    raw = "https://web.example.com/, http://localhost:3110,https://web.example.com"
    assert httpio.extra_origins(raw) == ["https://web.example.com", "http://localhost:3110"]
    for bad in ("*", "web.example.com", "https://web.example.com/path"):
        with pytest.raises(ValueError):
            httpio.extra_origins(bad)
