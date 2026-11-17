"""시연 시나리오 측정 스크립트(scripts/v3/llm_conformance.py). 규칙 정책으로만 돌린다(네트워크 없음)."""

import json

import pytest

from app.v3.agents.factory import LlmConfigError
from scripts.v3 import llm_conformance as script


def test_rule_run_reaches_approval_and_counts_drafts(tmp_path):
    dump = tmp_path / "calls.jsonl"
    result = script.run("rule", today="2026-11-18", max_calls=0, dump=dump, env={})
    assert result["sessionStatus"] == "awaiting_approval" and result["approvals"] == 3
    assert result["agreedThreads"] == "3/3"
    assert result["firstTryPassRate"] == 1.0 and result["rejections"] == {}
    assert result["modelCalls"] == 0  # 규칙 정책은 모델 호출로 세지 않는다
    assert set(result["callsByRole"]) == {"company:rule", "vendor:rule"}
    assert all(t["status"] == "agreed" and t["drafts"] for t in result["threads"])
    assert {a["field"] for a in result["answered"]} <= {"moisturePct", "currentCost"}
    rows = [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["guide"] for row in rows)


def test_real_models_need_explicit_settings_and_respect_the_call_budget():
    with pytest.raises(LlmConfigError):
        script.run("foundry", today="2026-11-18", max_calls=1, env={})

    class Counting:
        provider, model = "foundry", "fake"

        def complete(self, messages, *, tools=None, response_schema=None):
            raise AssertionError("한도를 넘으면 부르지 않는다")

    meter = script.Meter(0, None)
    wrapped = script.MeteredLLM(Counting(), meter, "company", "org_hanbit")
    with pytest.raises(script.LLMError, match="호출 한도"):
        wrapped.complete([{"role": "user", "content": "{}"}])
    assert meter.calls == []
