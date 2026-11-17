"""차례마다 만드는 응답 스키마, 안내(guide), 자리표시자에서 정하는 claimRefs, 재작성 안내.

실제 모델 대신 가짜 transport를 단 FoundryChat·OllamaChat을 쓴다(네트워크 없음). 가짜 모델은
규칙 정책의 답을 모델처럼 바꿔 낸다: claimRefs를 빼고, 쓰지 않는 항목은 null로 채운다.
"""

import json

from app.v3.agents import company, vendor
from app.v3.agents.draft import (
    RETRY_HINTS,
    claim_refs,
    drop_nulls,
    retry_guide,
    turn_schema,
)
from app.v3.agents.llm import FoundryChat, OllamaChat, strict_schema
from app.v3.agents.policy_rule import RulePolicy
from app.v3.evidence.validator import find_numbers
from tests.v3.runtime_fixtures import (
    NAMHAE,
    SAEGIL,
    make_app,
    only_for,
    recheck,
    thread_of,
    to_approval,
)


def test_claim_refs_come_from_the_placeholders_in_the_text():
    text = "단가는 {{price}}이고 수거 횟수는 {{ pickups }}입니다. 다시 {{price}}."
    assert claim_refs({"text": text}) == ["price", "pickups"]
    # 모델이 낸 순서를 따르되, 본문에 없는 ID는 싣지 않고 본문에만 있는 ID는 뒤에 붙인다
    listed = {"text": text, "claimRefs": ["pickups", "unused", "pickups"]}
    assert claim_refs(listed) == ["pickups", "price"]
    assert claim_refs({"text": "숫자 없음", "claimRefs": ["x"]}) == []
    assert claim_refs({"text": None, "claimRefs": "x"}) == []


def test_nulls_from_strict_outputs_are_treated_as_missing():
    raw = {
        "type": "conditional",
        "terms": {"items": [{"streamId": "st_a", "unitPriceRef": "p"}], "pickupsPerMonthRef": None},
        "toolCalls": [
            {"name": "judge_route", "arguments": {"streamId": "st_a", "streamIds": None}}
        ],
        "reason": None,
    }
    assert drop_nulls(raw) == {
        "type": "conditional",
        "terms": {"items": [{"streamId": "st_a", "unitPriceRef": "p"}]},
        "toolCalls": [{"name": "judge_route", "arguments": {"streamId": "st_a"}}],
    }


def test_turn_schema_enumerates_this_turn_and_converts_to_strict():
    schema = turn_schema(
        types=("question", "answer"),
        stream_ids=("st_a", "st_b"),
        tools=("judge_route",),
        extra={"reason": {"type": "string"}},
    )
    props = schema["properties"]
    assert props["type"]["enum"] == ["question", "answer"]
    assert props["streamIds"]["items"]["enum"] == ["st_a", "st_b"]
    assert props["stage"]["enum"] == [4, 5, 6]
    assert "claimRefs" not in props  # 코드가 본문에서 정한다
    call = props["toolCalls"]["items"]["properties"]
    assert call["name"]["enum"] == ["judge_route"]
    assert call["arguments"]["properties"]["streamId"]["enum"] == ["st_a", "st_b"]
    strict = strict_schema(schema)  # 속성 없는 객체가 없어 엄격 모드로 보낼 수 있다
    assert strict["properties"]["reason"]["type"] == ["string", "null"]
    assert schema["required"] == ["type", "stage", "streamIds", "text", "toolCalls"]


def test_retry_guide_names_each_code_once():
    found = [
        {"code": "NUMBER_OUTSIDE_PLACEHOLDER"},
        {"code": "NUMBER_OUTSIDE_PLACEHOLDER"},
        {"code": "GUARDRAIL_PRICE_BELOW_FLOOR"},
        {"code": "SOMETHING_ELSE"},
    ]
    lines = retry_guide(found)
    assert lines[0].startswith("앞의 초안이 거부됐다")
    assert lines[1] == f"NUMBER_OUTSIDE_PLACEHOLDER: {RETRY_HINTS['NUMBER_OUTSIDE_PLACEHOLDER']}"
    assert lines[2].startswith("GUARDRAIL_PRICE_BELOW_FLOOR: 업체 가드레일")
    assert len(lines) == 3 and retry_guide([]) == []


def test_prompts_have_examples_without_digits_outside_placeholders():
    for module in (company, vendor):
        assert module.EXAMPLES and "형식 예시" in module.SYSTEM_PROMPT
        for example in module.EXAMPLES:
            texts = [example["text"], *(example.get("terms") or {}).get("conditions", [])]
            assert all(find_numbers(text) == [] for text in texts)
            assert json.dumps(example, ensure_ascii=False) in module.SYSTEM_PROMPT


# ── 가짜 transport를 단 실제 모델 클래스 ─────────────────────────────


def model_like(messages, schema):
    """규칙 정책의 답을 엄격 모드 모델의 답처럼 바꾼다(claimRefs 없음, 쓰지 않는 항목은 null)."""
    output = json.loads(RulePolicy().complete(messages)["content"])
    output.pop("claimRefs", None)
    for key in schema["properties"]:
        output.setdefault(key, None)
    output["toolCalls"] = [
        {
            "name": call["name"],
            "arguments": {
                key: call["arguments"].get(key)
                for key in ("streamId", "streamIds", "route", "transportPaidBy", "query")
            },
        }
        for call in output.get("toolCalls") or []
    ]
    return output


class FoundryFake:
    """Foundry chat completions 흉내. 요청을 남기고, 정해 둔 답이 없으면 model_like로 답한다."""

    def __init__(self, *scripted):
        self.scripted, self.requests = list(scripted), []

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body)
        self.requests.append(payload)
        schema = payload["response_format"]["json_schema"]["schema"]
        if self.scripted:
            answer = self.scripted.pop(0)
            if isinstance(answer, dict) and "choices" in answer:
                return 200, {}, json.dumps(answer, ensure_ascii=False).encode()
        else:
            answer = model_like(payload["messages"], schema)
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        reply = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        return 200, {}, json.dumps(reply, ensure_ascii=False).encode()


def briefing(request):
    return json.loads(request["messages"][-1]["content"])


def test_foundry_vendor_turns_use_the_strict_turn_schema_and_pass():
    fake = FoundryFake()
    llm = FoundryChat("https://aif.example.com", "gpt-5-mini", "k", transport=fake)
    llm.reasoning_effort = "low"
    app = make_app(only_for("vendor", SAEGIL, llm))
    view = to_approval(app)
    thread = thread_of(view, SAEGIL)
    assert thread["status"] == "agreed" and view["session"]["status"] == "awaiting_approval"
    replies = [m for m in thread["messages"] if m["from"] == SAEGIL]
    assert replies and all(m["model"]["provider"] == "foundry" for m in replies)
    assert all(m["validation"]["attempts"] == 1 for m in replies)  # 첫 초안이 통과했다
    for request in fake.requests:
        assert request["reasoning_effort"] == "low" and "tools" not in request
        sent = request["response_format"]["json_schema"]
        assert sent["strict"] is True and sent["schema"]["additionalProperties"] is False
        data = briefing(request)
        allowed = data["thread"]["allowedTypes"]
        assert sent["schema"]["properties"]["type"]["enum"] == allowed
        streams = sent["schema"]["properties"]["streamIds"]["items"]["enum"]
        assert streams == data["thread"]["streamIds"]
        assert data["guide"][0].startswith("이번 차례에 쓸 수 있는 type")
        assert all("evidence" not in entry for entry in data["pool"])
    # 단가는 풀의 kg당 금액 claim 가운데서만 고른다
    last = fake.requests[-1]["response_format"]["json_schema"]["schema"]
    terms = last["properties"]["terms"]["anyOf"][0]
    refs = terms["properties"]["items"]["items"]["properties"]["unitPriceRef"]["enum"]
    assert "route_sprue_unitPrice" in refs
    assert not [ref for ref in refs if ref.endswith(("monthlyKg", "PerMonth", "Pct"))]
    checked, passed = recheck(app, view)
    assert checked == passed


def test_rejected_draft_comes_back_with_reasons_and_a_retry_guide():
    price = "pc_saegil_pp_a_unitPrice"
    bad = {
        "type": "offer",
        "stage": 5,
        "streamIds": ["st_sprue"],
        "text": "스프루·런너는 kg당 320원에 받겠습니다.",
        "toolCalls": [],
        "terms": {"items": [{"streamId": "st_sprue", "unitPriceRef": price}]},
    }
    truncated = {
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {},
    }
    fake = FoundryFake(bad)
    app = make_app(
        only_for("vendor", SAEGIL, FoundryChat("https://x", "gpt-5-mini", "k", transport=fake))
    )
    view = to_approval(app)
    thread = thread_of(view, SAEGIL)
    first = next(m for m in thread["messages"] if m["from"] == SAEGIL)
    assert first["validation"]["attempts"] == 2 and "320원" not in first["text"]
    retry = briefing(fake.requests[1])
    assert retry["rejections"][0]["code"] == "NUMBER_OUTSIDE_PLACEHOLDER"
    assert "320" in retry["rejections"][0]["message"]  # 자세한 사유는 다시 쓰는 모델에게만 간다
    assert any(line.startswith("NUMBER_OUTSIDE_PLACEHOLDER:") for line in retry["guide"])
    assert "320" not in json.dumps(first["validation"], ensure_ascii=False)  # 화면에는 일반 문장만
    # 본문이 잘려 비어 온 답은 형식 오류가 아니라 모델 오류로 남고, 다시 쓴 답이 나간다
    fake = FoundryFake(truncated)
    app = make_app(
        only_for("vendor", SAEGIL, FoundryChat("https://x", "gpt-5-mini", "k", transport=fake))
    )
    first = next(m for m in thread_of(to_approval(app), SAEGIL)["messages"] if m["from"] == SAEGIL)
    assert [r["code"] for r in first["validation"]["rejections"]] == ["MODEL_ERROR"]


class OllamaFake:
    def __init__(self):
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body)
        self.requests.append(payload)
        output = model_like(payload["messages"], payload["format"])
        reply = {
            "message": {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)},
            "done": True,
            "done_reason": "stop",
        }
        return 200, {}, json.dumps(reply, ensure_ascii=False).encode()


def test_ollama_vendor_gets_the_same_turn_schema_as_format():
    fake = OllamaFake()
    llm = OllamaChat("http://10.60.2.4:11434", "qwen2.5:7b", transport=fake)
    app = make_app(only_for("vendor", NAMHAE, llm))
    view = to_approval(app)
    thread = thread_of(view, NAMHAE)
    replies = [m for m in thread["messages"] if m["from"] == NAMHAE]
    assert thread["status"] == "agreed" and replies
    assert all(m["model"] == {"provider": "ollama", "name": "qwen2.5:7b"} for m in replies)
    for request in fake.requests:
        assert request["options"]["num_ctx"] == 16384 and "tools" not in request
        allowed = briefing(request)["thread"]["allowedTypes"]
        assert request["format"]["properties"]["type"]["enum"] == allowed
        assert "claimRefs" not in request["format"]["properties"]
    assert recheck(app, view)[0] == recheck(app, view)[1]


def vendor_guide(results, **overrides):
    stream = {"id": "st_x", "name": "퍼지 덩어리", "contaminants": [{"name": "ABS 혼입"}]}
    options = {
        "types": ("question", "answer", "offer", "conditional", "decline"),
        "stream_ids": ("st_x",),
        "streams": {"st_x": stream},
        "auto_declined": {},
        "results": results,
        "criteria": {"pc_mixed": {"maxContaminationPct": "pc_mixed_maxContaminationPct"}},
        "offered": [],
        "asked": [],
        "days": ["월", "수"],
        **overrides,
    }
    return vendor.guide(**options)


def test_vendor_guide_follows_the_tool_results_step_by_step():
    one = {"streamId": "st_x"}
    lines = vendor_guide([])
    assert "match_permit과 judge_route" in lines[-1] and "st_x" in lines[-1]
    permit = {"tool": "match_permit", "arguments": one, "status": "calculated"}
    refused = [
        {**permit, "output": {"matched": False, "reason": "허가 품목이 아닙니다."}},
        {"tool": "judge_route", "arguments": one, "status": "calculated", "output": {}},
    ]
    assert vendor_guide(refused)[-1].startswith("받을 수 있는 흐름이 없다. decline")
    route = {
        "tool": "judge_route",
        "arguments": one,
        "status": "conditional",
        "output": {"acceptable": True, "criterionId": "pc_mixed"},
        "claims": {"unitPrice": "route_x_unitPrice"},
        "checks": [
            {"key": "contamination_basis", "status": "conditional"},
            {"key": "mixture", "status": "conditional"},
        ],
    }
    ready = [{**permit, "output": {"matched": True}}, route]
    assert "check_loading과 route_extra_cost" in vendor_guide(ready)[-1]
    many = {"streamIds": ["st_x"]}
    loading = {
        "tool": "check_loading",
        "arguments": many,
        "status": "calculated",
        "claims": {"pickupsPerMonth": "load_x_pickupsPerMonth"},
        "checks": [{"key": "min_volume", "status": "unmet"}],
    }
    extra = {"tool": "route_extra_cost", "arguments": many, "status": "calculated"}
    lines = vendor_guide([*ready, loading, extra], roles={"route_x_unitPrice": "treatmentFee"})
    text = "\n".join(lines)
    assert "처리비는 {{route_x_unitPrice}}입니다" in text and "'매입 단가'라고 쓰지 않는다" in text
    assert "오염도가 매입 기준 상한 {{pc_mixed_maxContaminationPct}} 이하로 확인될 것" in text
    assert "혼입물(ABS 혼입)은 분리해 내보내거나" in text
    assert "{{load_x_pickupsPerMonth}}" in text and "월·수" in text
    assert "기존 노선에 묶어 수거할 것" in text
    assert lines[-1] == "조건이 하나라도 있으니 type은 conditional이다."
    priced = vendor_guide([*ready, loading, extra], roles={"route_x_unitPrice": "unitPrice"})
    assert any("매입 단가는 {{route_x_unitPrice}}입니다" in line for line in priced)
    done = vendor_guide([*ready, loading, extra], offered=["st_x"])
    assert "모두 조건을 냈다" in done[-1]


def test_company_guide_says_what_this_turn_is_for():
    class Incoming:
        type = "conditional"

    results = [
        {
            "tool": "judge_route",
            "arguments": {"streamId": "st_a"},
            "checks": [{"ask": "혼입 상태로 받을 수 있는지 알려 주세요."}],
        }
    ]
    names = {"st_a": "스프루", "st_b": "필름"}
    common = {"asked": False, "cannot_ask_user": False, "names": names, "results": results}
    first = company.guide(task="inquiry", types=("inquiry",), incoming=None, uncovered=[], **common)
    assert first[0] == "이번 차례에 쓸 수 있는 type: inquiry."
    assert "스프루: 혼입 상태로 받을 수 있는지" in first[-1]
    types = ("question", "answer", "accept", "ask_user")
    pending = company.guide(
        task="reply", types=types, incoming=Incoming(), uncovered=["st_b"], **common
    )
    assert "아직 조건을 받지 못한 폐기물: 필름" in pending[-1]
    settled = company.guide(task="reply", types=types, incoming=Incoming(), uncovered=[], **common)
    assert "accept한다" in settled[-1]
