"""모델 형식 준수 시험. 규칙 정책과 스크립트로만 돌린다(실제 모델은 미측정)."""

import json

import pytest

from app.v3.agents.llm import LLMError, ScriptedLLM
from app.v3.agents.policy_rule import RulePolicy
from app.v3.eval import format_compliance as fc


@pytest.fixture(scope="module")
def situations():
    return fc.build_situations()


def rule_output(situation):
    messages = [{"role": "user", "content": json.dumps(situation.briefing, ensure_ascii=False)}]
    return json.loads(RulePolicy().complete(messages)["content"])


def writer(situations, role="vendor", kind=None):
    for situation in situations:
        output = rule_output(situation)
        if situation.role == role and (kind is None or output["type"] == kind):
            return situation, output
    raise AssertionError("그런 상황이 없다")


def codes(situation, output):
    return [
        p["code"]
        for p in fc.check_response(situation, json.dumps(output, ensure_ascii=False))["problems"]
    ]


def test_the_fixed_set_has_twenty_situations_from_both_agents(situations):
    assert len(situations) == fc.SITUATION_COUNT == 20
    assert [s.id for s in situations] == [f"F{n:02d}" for n in range(1, 21)]
    assert {s.role for s in situations} == {"company", "vendor"}
    assert {"inquiry", "reply", "confirm"} <= {s.task for s in situations}
    again = fc.build_situations()
    assert [s.briefing for s in again] == [s.briefing for s in situations]  # 결정적
    assert all(s.briefing["pool"] and s.system and s.schema["required"] for s in situations)


def test_rule_policy_passes_every_situation_first_time(situations):
    result = fc.run_compliance(RulePolicy(), situations)
    assert result["model"] == {"provider": "rule", "name": "규칙 정책(모델 미연결)"}
    assert result["firstPassed"] == result["finalPassed"] == 20
    assert result["firstPassRate"] == result["finalPassRate"] == 1.0
    assert result["rewritten"] == 0 and result["firstFailureCodes"] == {}


def test_deliberately_wrong_script_is_caught(situations):
    result = fc.run_compliance(fc.bad_script(situations), situations)
    assert result["model"]["provider"] == "scripted"
    assert (result["firstPassed"], result["finalPassed"], result["rewritten"]) == (14, 18, 6)
    assert result["firstPassRate"] == 0.7 and result["finalPassRate"] == 0.9
    assert result["firstFailureCodes"] == {
        "INVALID_JSON": 1,
        "NUMBER_OUTSIDE_PLACEHOLDER": 1,
        "PLACEHOLDER_REF_MISMATCH": 1,
        "SCHEMA": 1,
        "TYPE_NOT_ALLOWED": 1,
        "UNKNOWN_CLAIM_REF": 1,
    }
    still_wrong = [r for r in result["results"] if not r["finalPass"]]
    assert [r["finalCodes"] for r in still_wrong] == [
        ["TYPE_NOT_ALLOWED"],
        ["NUMBER_OUTSIDE_PLACEHOLDER"],
    ]
    by_check = {c["key"]: c["passed"] for c in result["byCheck"]}
    assert by_check == {"json": 19, "schema": 18, "claimRefs": 18, "numbers": 19}


def test_each_check_names_its_own_problem(situations):
    situation, good = writer(situations, "vendor", "offer")
    assert codes(situation, good) == []
    assert fc.check_response(situation, "죄송합니다, 다시 쓰겠습니다.")["problems"][0]["code"] == (
        fc.INVALID_JSON
    )
    assert codes(situation, {k: v for k, v in good.items() if k != "text"})[0] == fc.SCHEMA
    # claimRefs는 차례 스키마에 없다(에이전트가 본문의 자리표시자에서 정한다). 빠져도 통과한다
    assert codes(situation, {k: v for k, v in good.items() if k != "claimRefs"}) == []
    assert codes(situation, {**good, "stage": "5"}) == [fc.SCHEMA]
    assert codes(situation, {**good, "type": "inquiry"}) == [fc.TYPE_NOT_ALLOWED]
    assert codes(situation, {**good, "streamIds": ["st_nowhere"]})[0] == fc.UNKNOWN_STREAM
    made_up = {
        **good,
        "text": good["text"] + " {{ghost}}",
        "claimRefs": [*good["claimRefs"], "ghost"],
    }
    assert codes(situation, made_up) == [fc.UNKNOWN_CLAIM_REF]
    unused = {**good, "claimRefs": [*good["claimRefs"], next(iter(situation.pool))]}
    assert (
        fc.REF_MISMATCH in codes(situation, unused) or unused["claimRefs"][-1] in good["claimRefs"]
    )
    assert codes(situation, {**good, "text": good["text"] + " 약 300원입니다."}) == [
        "NUMBER_OUTSIDE_PLACEHOLDER"
    ]
    assert codes(situation, {**good, "terms": {"items": []}}) == [fc.TERMS]
    priced = {**good, "terms": {"items": [{"streamId": good["streamIds"][0], "unitPrice": 300}]}}
    assert codes(situation, priced) == [fc.TERMS]  # 단가를 숫자로 적으면 안 된다


def test_company_decisions_and_tool_calls_are_checked(situations):
    situation, _ = writer(situations, "company")
    reply = next(s for s in situations if s.role == "company" and fc.ACCEPT in s.allowed_types)
    base = {"stage": 5, "streamIds": [], "text": "", "claimRefs": [], "toolCalls": []}
    assert fc.check_response(reply, json.dumps({**base, "type": "accept"}))["kind"] == "decision"
    stream_id = reply.briefing["thread"]["streamIds"][0]
    asked = {
        **base,
        "type": "ask_user",
        "userQuestions": [{"streamId": stream_id, "field": "moisturePct"}],
    }
    assert codes(reply, asked) == []
    invented = {
        **base,
        "type": "ask_user",
        "userQuestions": [{"streamId": stream_id, "field": "budget"}],
    }
    assert codes(reply, invented) == [fc.USER_QUESTION]
    call = {
        **base,
        "type": "answer",
        "toolCalls": [{"name": "judge_route", "arguments": {"streamId": stream_id}}],
    }
    checked = fc.check_response(reply, json.dumps(call))
    assert checked == {"ok": True, "kind": "tool_calls", "problems": []}
    bad = {
        **base,
        "type": "answer",
        "toolCalls": [{"name": "judge_route", "arguments": {"price": 300}}],
    }
    assert codes(reply, bad) == [fc.TOOL_CALL]
    assert codes(reply, {**base, "type": "answer", "toolCalls": [{"name": "transfer_money"}]}) == [
        fc.TOOL_CALL
    ]
    # 도구는 입력의 목록(이름, 하는 일, 입력 키)과 응답 스키마의 enum으로 함께 간다
    names = {t["name"] for t in situation.briefing["tools"]}
    calls = situation.schema["properties"]["toolCalls"]["items"]["properties"]
    assert names and set(calls["name"]["enum"]) == names


def test_tool_only_answers_and_model_errors_fail_the_situation(situations):
    situation, good = writer(situations, "vendor", "offer")
    stream_id = good["streamIds"][0]
    call = {
        **good,
        "text": "",
        "claimRefs": [],
        "toolCalls": [{"name": "judge_route", "arguments": {"streamId": stream_id}}],
    }
    # 도구를 한 번 부른 뒤 메시지를 쓰면 첫 시도로 친다
    one_round = fc.run_situation(ScriptedLLM([call, good]), situation)
    assert one_round["firstPass"] and not one_round["rewritten"]
    # 도구만 거듭 부르면 실패다
    stuck = fc.run_situation(ScriptedLLM([call] * 6), situation)
    assert not stuck["finalPass"] and stuck["firstCodes"] == [fc.TOO_MANY_TOOL_ROUNDS]
    down = fc.run_situation(ScriptedLLM([LLMError("연결 실패"), LLMError("연결 실패")]), situation)
    assert not down["finalPass"] and down["firstCodes"] == [fc.MODEL_ERROR]
    # 거부 사유는 다시 쓸 때 입력으로 돌려준다
    llm = ScriptedLLM(["JSON이 아님", good])
    recovered = fc.run_situation(llm, situation)
    assert recovered["rewritten"] and recovered["finalPass"] and not recovered["firstPass"]
    second = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert second["rejections"][0]["code"] == fc.INVALID_JSON


def test_cli_measures_local_providers_and_leaves_real_models_unmeasured(tmp_path, capsys):
    out = tmp_path / "rule.json"
    assert fc.main(["--provider", "rule", "--limit", "5", "--out", str(out)], env={}) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["measured"] is True and saved["situationCount"] == 5 and saved["finalPassed"] == 5
    assert fc.main(["--provider", "scripted"], env={}) == 0
    for provider in ("foundry", "ollama"):
        path = tmp_path / f"{provider}.json"
        assert fc.main(["--provider", provider, "--out", str(path)], env={}) == 0
        result = json.loads(path.read_text(encoding="utf-8"))
        assert result["measured"] is False and result["status"] == "미측정"
        assert "firstPassRate" not in result  # 재지 않은 숫자를 채우지 않는다
    printed = capsys.readouterr().out
    assert "foundry: 미측정" in printed and "ollama: 미측정" in printed


def test_real_providers_are_built_only_from_explicit_settings():
    class Args:
        model, base_url = "qwen2.5:7b", ""

    assert fc.make_llm("ollama", Args, {}) is None
    llm = fc.make_llm("ollama", Args, {"OLLAMA_BASE_URL": "http://10.0.0.4:11434"})
    assert (llm.provider, llm.model) == ("ollama", "qwen2.5:7b")
    env = {
        "FOUNDRY_ENDPOINT": "https://x.invalid",
        "FOUNDRY_DEPLOYMENT": "d",
        "FOUNDRY_API_KEY": "k",
    }
    assert fc.make_llm("foundry", Args, {"FOUNDRY_ENDPOINT": "https://x.invalid"}) is None
    assert fc.make_llm("foundry", Args, env).provider == "foundry"
