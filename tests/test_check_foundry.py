"""Foundry 호출은 대체 클라이언트로만 검증한다."""

import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("openai")
check_foundry = importlib.import_module("scripts.check_foundry")


def test_manual_check_uses_expected_settings(monkeypatch, capsys):
    for name, value in {
        "OPENAI_ENDPOINT": "https://example.invalid/openai/v1",
        "CHAT_DEPLOYMENT": "deployment",
        "FOUNDRY_KEY": "test-secret",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(check_foundry, "load_dotenv", lambda *a, **kw: None)
    observed = {}

    def client(**kwargs):
        observed.update(kwargs)

        def create(**request):
            observed.update(request)
            return SimpleNamespace(status="completed", output_text="확인 완료")

        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setattr(check_foundry, "OpenAI", client)
    assert check_foundry.main() == 0
    output = capsys.readouterr()
    assert output.out == "확인 완료\n" and output.err == ""
    assert observed == {
        "base_url": "https://example.invalid/openai/v1",
        "api_key": "test-secret",
        "timeout": 20.0,
        "max_retries": 0,
        "model": "deployment",
        "input": check_foundry.PROMPT,
    }


def test_failure_never_prints_exception_or_secret(monkeypatch, capsys):
    for name in ("OPENAI_ENDPOINT", "CHAT_DEPLOYMENT", "FOUNDRY_KEY"):
        monkeypatch.setenv(name, "test-secret")
    monkeypatch.setattr(check_foundry, "load_dotenv", lambda *a, **kw: None)

    def fail(**kwargs):
        raise ValueError("test-secret Authorization: test-secret")

    monkeypatch.setattr(check_foundry, "OpenAI", fail)
    assert check_foundry.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "test-secret" not in output.err and "Authorization" not in output.err


def test_missing_settings_do_not_call_client(monkeypatch, capsys):
    for name in ("OPENAI_ENDPOINT", "CHAT_DEPLOYMENT", "FOUNDRY_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(check_foundry, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(check_foundry, "OpenAI", lambda **kwargs: pytest.fail("unexpected call"))
    assert check_foundry.main() == 1
    assert "FOUNDRY_KEY" in capsys.readouterr().err
