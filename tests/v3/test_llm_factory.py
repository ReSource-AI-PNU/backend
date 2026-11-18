"""모델 선택(agents/factory.py): 두 서버가 같은 환경 변수로 같은 모델을 고른다. 실제 호출은 없다."""

import logging

import pytest

from app.v3 import devserver
from app.v3.agents.factory import (
    ANTHROPIC_DEFAULT_MODEL,
    LLM_MODES,
    LlmConfigError,
    anthropic_key,
    build_llm_factory,
    normalize_mode,
)
from app.v3.agents.llm import AnthropicChat, FoundryChat, OllamaChat

TODAY = "2026-11-18"
KEY = "not-a-real-key"
FOUNDRY = {
    "FOUNDRY_ENDPOINT": "https://example.invalid",
    "FOUNDRY_DEPLOYMENT": "chat",
    "FOUNDRY_API_KEY": KEY,
}
OLLAMA_MODEL = {"provider": "ollama", "name": "qwen2.5:7b"}


def test_modes_and_rule_reads_nothing():
    assert LLM_MODES == ("rule", "foundry", "anthropic")
    assert normalize_mode(None) == "rule" and normalize_mode(" Anthropic ") == "anthropic"
    assert build_llm_factory("rule", {"ANTHROPIC_API_KEY_FILE": "/nonexistent"}) is None
    assert build_llm_factory(None, {}) is None and build_llm_factory("", {}, strict=True) is None
    with pytest.raises(LlmConfigError, match="V3_LLM"):
        build_llm_factory("gpt", {})


def test_foundry_strict_names_missing_values_without_printing_them():
    env = {"FOUNDRY_ENDPOINT": "https://example.invalid", "FOUNDRY_API_KEY": KEY}
    with pytest.raises(LlmConfigError) as error:
        build_llm_factory("foundry", env, strict=True)
    assert "FOUNDRY_DEPLOYMENT" in str(error.value) and KEY not in str(error.value)
    factory = build_llm_factory("foundry", FOUNDRY, strict=True)
    company = factory("company", "org_hanbit", None)
    assert isinstance(company, FoundryChat) and company.model == "chat"
    assert KEY not in repr(company)


def test_foundry_lenient_falls_back_to_rule_and_says_so(caplog):
    with caplog.at_level(logging.WARNING):
        factory = build_llm_factory("foundry", {"OLLAMA_BASE_URL": "http://10.0.0.4:11434"})
    assert "FOUNDRY_ENDPOINT" in caplog.text and "규칙 정책" in caplog.text
    assert factory("company", "org_hanbit", None) is None
    assert factory("vendor", "org_saegil", {"provider": "foundry"}) is None
    # ollama를 고른 업체는 주소가 있으면 본 모델 설정과 관계없이 그 모델을 쓴다
    assert isinstance(factory("vendor", "org_namhae", OLLAMA_MODEL), OllamaChat)


def test_anthropic_key_from_value_or_file(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text(f"  {KEY}\n", encoding="utf-8")
    assert anthropic_key({"ANTHROPIC_API_KEY_FILE": str(key_file)}) == KEY
    assert anthropic_key({"ANTHROPIC_API_KEY": "direct-key", "ANTHROPIC_API_KEY_FILE": "x"}) == (
        "direct-key"
    )
    assert anthropic_key({}) == ""
    with pytest.raises(LlmConfigError, match="ANTHROPIC_API_KEY_FILE"):
        anthropic_key({"ANTHROPIC_API_KEY_FILE": str(tmp_path / "missing")})
    factory = build_llm_factory("anthropic", {"ANTHROPIC_API_KEY_FILE": str(key_file)}, strict=True)
    company = factory("company", "org_hanbit", None)
    assert isinstance(company, AnthropicChat) and company.model == ANTHROPIC_DEFAULT_MODEL
    assert KEY not in repr(company)
    named = build_llm_factory("anthropic", {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_MODEL": "m-1"})
    assert named("vendor", "org_saegil", {"provider": "foundry", "name": "gpt-5-mini"}).model == (
        "m-1"
    )


def test_anthropic_missing_key_strict_stops_lenient_runs_rule(tmp_path, caplog):
    with pytest.raises(LlmConfigError, match="ANTHROPIC_API_KEY"):
        build_llm_factory("anthropic", {}, strict=True)
    with pytest.raises(LlmConfigError, match="읽을 수 없습니다"):
        build_llm_factory(
            "anthropic", {"ANTHROPIC_API_KEY_FILE": str(tmp_path / "missing")}, strict=True
        )
    with caplog.at_level(logging.WARNING):
        empty = build_llm_factory("anthropic", {})
        unreadable = build_llm_factory(
            "anthropic", {"ANTHROPIC_API_KEY_FILE": str(tmp_path / "missing")}
        )
    assert caplog.text.count("규칙 정책") == 2
    assert empty("company", "org_hanbit", None) is None
    assert unreadable("vendor", "org_saegil", None) is None


def test_ollama_vendor_is_never_swapped_unless_asked():
    env = {"ANTHROPIC_API_KEY": KEY}
    factory = build_llm_factory("anthropic", env, strict=True)
    assert factory("vendor", "org_namhae", OLLAMA_MODEL) is None  # 주소가 없으면 규칙 정책
    everyone = build_llm_factory("anthropic", {**env, "V3_LLM_ALL_VENDORS": "1"}, strict=True)
    assert isinstance(everyone("vendor", "org_namhae", OLLAMA_MODEL), AnthropicChat)
    with_ollama = build_llm_factory(
        "anthropic",
        {**env, "V3_LLM_ALL_VENDORS": "1", "OLLAMA_BASE_URL": "http://10.0.0.4:11434"},
        strict=True,
    )
    local = with_ollama("vendor", "org_namhae", OLLAMA_MODEL)
    assert isinstance(local, OllamaChat) and local.model == "qwen2.5:7b"
    # foundry에서도 V3_LLM_ALL_VENDORS=1이면 Ollama 주소가 없는 업체를 Foundry가 맡는다
    assert (
        build_llm_factory("foundry", FOUNDRY, strict=True)("vendor", "org_namhae", OLLAMA_MODEL)
        is None
    )
    foundry = build_llm_factory("foundry", {**FOUNDRY, "V3_LLM_ALL_VENDORS": "1"}, strict=True)
    assert isinstance(foundry("vendor", "org_namhae", OLLAMA_MODEL), FoundryChat)
    # 주 모델이 없으면(rule) 이 설정도 아무것도 바꾸지 않는다(모두 규칙 정책)
    assert build_llm_factory("rule", {"V3_LLM_ALL_VENDORS": "1"}, strict=True) is None


def test_devserver_uses_the_shared_builder_and_stops_on_missing_key(tmp_path):
    with pytest.raises(SystemExit) as error:
        devserver.build_app(llm="anthropic", today=TODAY, env={})
    assert "ANTHROPIC_API_KEY" in str(error.value)
    key_file = tmp_path / "key"
    key_file.write_text(KEY, encoding="utf-8")
    app = devserver.build_app(
        llm="anthropic", today=TODAY, env={"ANTHROPIC_API_KEY_FILE": str(key_file)}
    )
    company = app.llm_for("company", "org_hanbit", None)
    assert (company.provider, company.model) == ("anthropic", ANTHROPIC_DEFAULT_MODEL)
    assert app.llm_for("vendor", "org_namhae", OLLAMA_MODEL).provider == "rule"
    with pytest.raises(SystemExit, match="V3_LLM"):
        devserver.build_app(llm="gpt", today=TODAY, env={})


def test_devserver_main_reads_v3_llm_from_the_environment(monkeypatch):
    """--llm을 주지 않으면 V3_LLM을 읽는다. 틀린 값이면 서버를 띄우기 전에 멈춘다."""
    monkeypatch.setenv("V3_LLM", "gpt")
    with pytest.raises(SystemExit, match="V3_LLM"):
        devserver.main(["--port", "0"])
    monkeypatch.setenv("V3_LLM", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_FILE", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        devserver.main(["--port", "0"])
