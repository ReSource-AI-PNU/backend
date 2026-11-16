"""모델 형식 준수 시험. 모델이 에이전트의 응답 형식(RUNTIME.md 1장)을 얼마나 지키는지 잰다.

    python3 -m app.v3.eval.format_compliance --provider rule|scripted|foundry|ollama [--out 결과.json]

고정 상황은 평가 사례를 규칙 정책으로 돌릴 때 두 에이전트가 실제로 모델에게 준 입력(시스템 지시,
JSON 입력, 응답 스키마)이다. 메시지를 써야 하는 차례만 고르고, 정해 둔 사례 순서대로 앞에서
SITUATION_COUNT개를 쓴다. 상황마다 모델에게 한 번 묻고, 거부되면 사유를 돌려주고 한 번 다시
쓰게 한다(진행기와 같은 규칙).

보는 것:
- JSON 객체 하나인가, 필수 키와 형식이 맞는가(스키마)
- type이 지금 보낼 수 있는 종류인가, streamIds가 이 대화의 흐름인가
- claimRefs가 모두 풀 안에 있는가, 본문의 자리표시자와 claimRefs가 서로 맞는가
- 자리표시자 밖에 숫자가 없는가
- 근거 검증기를 통과하는가
- 조건을 내는 업체 메시지에 terms가 있고 단가를 claim ID로 가리키는가

실제 모델(foundry, ollama) 결과는 이 저장소에서 재지 않았다. 리포트에는 "미측정"으로 적는다.
rule은 규칙 정책(모델이 아님), scripted는 일부러 틀리게 답하는 스크립트로 시험 자체를 확인한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.v3.a2a import guardrails as rails
from app.v3.a2a.message import MESSAGE_TYPES, PLACEHOLDER, STAGES
from app.v3.agents.draft import ACCEPT, ASK_USER, DraftError, model_label, parse_output
from app.v3.agents.llm import LLM, FoundryChat, LLMError, OllamaChat, ScriptedLLM
from app.v3.agents.policy_rule import RulePolicy
from app.v3.eval import runner
from app.v3.eval.loader import load_cases
from app.v3.evidence.validator import (
    ALLOWED_TOKENS,
    NUMBER_OUTSIDE_PLACEHOLDER,
    find_numbers,
    name_tokens,
    validate_text,
)

SITUATION_COUNT = 20
MAX_TOOL_ROUNDS = 2
# 상황을 뽑는 사례(이 순서대로). 문의, 조건 제안, 조건부, 거절, 질문과 답, 확정이 고루 들어간다
SOURCE_CASES = ("C11", "C01", "C13", "C21", "C19", "C27", "C34", "C12", "C29", "C04")
NOT_MEASURED = "미측정"
INVALID_JSON = "INVALID_JSON"
SCHEMA = "SCHEMA"
TYPE_NOT_ALLOWED = "TYPE_NOT_ALLOWED"
UNKNOWN_STREAM = "UNKNOWN_STREAM"
UNKNOWN_CLAIM_REF = "UNKNOWN_CLAIM_REF"
REF_MISMATCH = "PLACEHOLDER_REF_MISMATCH"
TERMS = "TERMS"
USER_QUESTION = "USER_QUESTION"
TOOL_CALL = "TOOL_CALL"
TOO_MANY_TOOL_ROUNDS = "TOO_MANY_TOOL_ROUNDS"
MODEL_ERROR = "MODEL_ERROR"
CHECKS = (
    ("json", "JSON 객체 하나", (INVALID_JSON,)),
    (
        "schema",
        "스키마(필수 키, 형식, 보낼 수 있는 종류)",
        (SCHEMA, TYPE_NOT_ALLOWED, UNKNOWN_STREAM),
    ),
    ("claimRefs", "claimRefs가 풀 안에 있고 자리표시자와 맞음", (UNKNOWN_CLAIM_REF, REF_MISMATCH)),
    ("numbers", "자리표시자 밖 숫자 없음", (NUMBER_OUTSIDE_PLACEHOLDER,)),
)


@dataclass
class Situation:
    id: str
    case_id: str
    role: str  # company | vendor
    task: str
    org_id: str
    system: str
    briefing: dict[str, Any]
    schema: dict[str, Any]
    tools: list[dict[str, Any]]
    registry: Any
    today: str
    names: list[str] = field(default_factory=list)

    @property
    def allowed_types(self) -> list[str]:
        return list((self.briefing.get("thread") or {}).get("allowedTypes") or [])

    @property
    def pool(self) -> dict[str, dict[str, Any]]:
        return {str(c["id"]): c for c in self.briefing.get("pool") or []}


class _Recorder:
    """규칙 정책으로 답하면서, 에이전트가 모델에게 준 입력을 그대로 적어 둔다."""

    provider, model = RulePolicy.provider, RulePolicy.model

    def __init__(self, sink: list[dict[str, Any]], role: str, org_id: str) -> None:
        self._rule = RulePolicy()
        self._sink, self._role, self._org = sink, role, org_id

    def complete(self, messages, *, tools=None, response_schema=None):
        response = self._rule.complete(messages, tools=tools, response_schema=response_schema)
        output = json.loads(response["content"])
        self._sink.append(
            {
                "role": self._role,
                "org": self._org,
                "system": messages[0]["content"],
                "briefing": json.loads(messages[-1]["content"]),
                "schema": response_schema,
                "tools": list(tools or []),
                "wroteMessage": not output.get("toolCalls"),
            }
        )
        return response


def build_situations(
    count: int = SITUATION_COUNT, cases: list[dict[str, Any]] | None = None
) -> list[Situation]:
    """고정 상황 세트. 같은 사례 세트면 언제 만들어도 같다."""
    by_id = {c["id"]: c for c in (cases if cases is not None else load_cases())}
    found: list[Situation] = []
    for case_id in SOURCE_CASES:
        case = by_id.get(case_id)
        if case is None or len(found) >= count:
            continue
        calls: list[dict[str, Any]] = []
        session = runner._Session(
            case,
            {"llm_factory": lambda role, org, _model, calls=calls: _Recorder(calls, role, org)},
        )
        session.run()
        store = session.app.store
        names = [o.get("name") for o in store.list_orgs()]
        names += [s.get("name") for s in store.list_streams(session.emitter)]
        for call in calls:
            briefing = call["briefing"]
            if not call["wroteMessage"] or briefing.get("rejections") or len(found) >= count:
                continue
            org_id = str((briefing.get("org") or {}).get("id") or call["org"])
            found.append(
                Situation(
                    id=f"F{len(found) + 1:02d}",
                    case_id=case_id,
                    role=call["role"],
                    task=str(briefing.get("task")),
                    org_id=org_id,
                    system=call["system"],
                    briefing=briefing,
                    schema=call["schema"],
                    tools=call["tools"],
                    registry=session.app.registry,
                    today=session.app.today,
                    names=[n for n in names if n],
                )
            )
    return found


# ── 응답 하나 검사 ───────────────────────────────────────────────────


def _problem(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _is_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def check_response(situation: Situation, content: Any) -> dict[str, Any]:
    """모델 응답 하나를 검사한다. {"ok", "kind", "problems": [{"code", "message"}]}.

    kind는 message(메시지), decision(accept·ask_user), tool_calls(도구를 부르겠다는 응답)다.
    """
    try:
        output = parse_output(content)
    except DraftError as error:
        return {"ok": False, "kind": None, "problems": [_problem(INVALID_JSON, str(error))]}
    problems: list[dict[str, str]] = []
    required = list((situation.schema or {}).get("required") or [])
    missing = [key for key in required if key not in output]
    if missing:
        problems.append(_problem(SCHEMA, f"필수 키가 없습니다: {', '.join(missing)}."))
    kind = output.get("type")
    stage = output.get("stage")
    if "stage" in output and (isinstance(stage, bool) or stage not in STAGES):
        problems.append(_problem(SCHEMA, "stage는 단계 번호(정수)여야 합니다."))
    for key in ("streamIds", "claimRefs"):
        if key in output and not _is_strings(output[key]):
            problems.append(_problem(SCHEMA, f"{key}는 문자열 배열이어야 합니다."))
    if "text" in output and not isinstance(output["text"], str):
        problems.append(_problem(SCHEMA, "text는 문자열이어야 합니다."))
    calls = output.get("toolCalls")
    if "toolCalls" in output and not isinstance(calls, list):
        problems.append(_problem(SCHEMA, "toolCalls는 배열이어야 합니다."))
        calls = []
    known_streams = set((situation.briefing.get("thread") or {}).get("streamIds") or [])
    unknown = [
        s for s in output.get("streamIds") or [] if isinstance(s, str) and s not in known_streams
    ]
    if unknown:
        problems.append(
            _problem(UNKNOWN_STREAM, f"이 대화에 없는 흐름입니다: {', '.join(unknown)}.")
        )
    if calls:
        allowed_tools = {t["name"] for t in situation.tools}
        for call in calls:
            name = call.get("name") if isinstance(call, dict) else None
            arguments = call.get("arguments", {}) if isinstance(call, dict) else None
            if name not in allowed_tools or not isinstance(arguments, dict):
                problems.append(_problem(TOOL_CALL, f"부를 수 없는 도구 호출입니다: {name}."))
            elif any(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in arguments.values()
            ):
                problems.append(
                    _problem(TOOL_CALL, "도구 입력은 ID로만 줍니다(숫자를 넣지 않는다).")
                )
        return {"ok": not problems, "kind": "tool_calls", "problems": problems}
    if kind not in situation.allowed_types:
        shown = ", ".join(situation.allowed_types)
        problems.append(
            _problem(
                TYPE_NOT_ALLOWED,
                f"지금은 '{kind}'을(를) 보낼 수 없습니다. 보낼 수 있는 종류: {shown}.",
            )
        )
        return {"ok": False, "kind": "message", "problems": problems}
    if kind in (ACCEPT, ASK_USER):
        if kind == ASK_USER:
            fields = set(situation.briefing.get("userQuestionFields") or [])
            asked = output.get("userQuestions")
            valid = (
                isinstance(asked, list)
                and asked
                and all(
                    isinstance(a, dict)
                    and a.get("streamId") in known_streams
                    and a.get("field") in fields
                    for a in asked
                )
            )
            if not valid:
                problems.append(
                    _problem(
                        USER_QUESTION, "userQuestions에는 이 대화의 흐름과 정해진 항목만 넣습니다."
                    )
                )
        return {"ok": not problems, "kind": "decision", "problems": problems}
    if kind not in MESSAGE_TYPES:
        problems.append(_problem(TYPE_NOT_ALLOWED, f"모르는 메시지 종류입니다: {kind}."))
    text = output.get("text") if isinstance(output.get("text"), str) else ""
    if not text.strip():
        problems.append(_problem(SCHEMA, "본문(text)이 비어 있습니다."))
    refs = [r for r in output.get("claimRefs") or [] if isinstance(r, str)]
    pool = situation.pool
    outside = [r for r in dict.fromkeys(refs) if r not in pool]
    if outside:
        problems.append(
            _problem(UNKNOWN_CLAIM_REF, f"claim 풀에 없는 ID입니다: {', '.join(outside)}.")
        )
    used = list(dict.fromkeys(m.group(1) for m in PLACEHOLDER.finditer(text)))
    if set(used) != set(refs):
        problems.append(
            _problem(REF_MISMATCH, "본문의 자리표시자와 claimRefs가 서로 맞지 않습니다.")
        )
    tokens = (*ALLOWED_TOKENS, *name_tokens(situation.names))
    numbers = find_numbers(text, allowed_tokens=tokens)
    if numbers:
        problems.append(
            _problem(
                NUMBER_OUTSIDE_PLACEHOLDER,
                f"자리표시자 밖 숫자가 있습니다: {', '.join(numbers[:8])}.",
            )
        )
    if not outside:
        claims = [
            {k: pool[r][k] for k in ("id", "kind", "value", "unit", "evidence")}
            for r in dict.fromkeys(refs)
        ]
        checked = validate_text(
            text,
            claims,
            situation.registry,
            org_id=situation.org_id,
            today=situation.today,
            allowed_tokens=tokens,
        )
        seen = {p["code"] for p in problems}
        for rejection in checked.rejections:
            # 자리표시자와 claimRefs의 어긋남은 위에서 한 번 셌다
            if rejection.code in ("UNKNOWN_PLACEHOLDER", "UNUSED_CLAIM") and REF_MISMATCH in seen:
                continue
            if rejection.code not in seen:
                problems.append(_problem(rejection.code, rejection.message))
                seen.add(rejection.code)
    if situation.role == "vendor" and kind in rails.ENFORCED_TYPES:
        terms = output.get("terms")
        items = terms.get("items") if isinstance(terms, dict) else None
        valid = (
            isinstance(items, list)
            and items
            and all(
                isinstance(i, dict)
                and i.get("streamId") in known_streams
                and i.get("unitPriceRef") in refs
                for i in items
            )
        )
        if not valid:
            problems.append(
                _problem(
                    TERMS,
                    "조건을 내는 메시지에는 terms.items가 있고 단가를 claim ID로 가리켜야 합니다.",
                )
            )
    return {"ok": not problems, "kind": "message", "problems": problems}


# ── 시험 ─────────────────────────────────────────────────────────────


def _ask(llm: LLM, situation: Situation, messages: list[dict[str, Any]]) -> tuple[Any, dict | None]:
    try:
        response = llm.complete(messages, tools=situation.tools, response_schema=situation.schema)
    except LLMError as error:
        return None, _problem(MODEL_ERROR, f"모델 호출에 실패했습니다: {error}")
    native = response.get("tool_calls") or []
    if native:  # 도구 호출 기능으로 답한 경우도 같은 모양으로 바꿔 검사한다
        declared = [{"name": c["name"], "arguments": c.get("arguments") or {}} for c in native]
        return json.dumps({"toolCalls": declared}, ensure_ascii=False), None
    return response.get("content"), None


def _attempt(
    llm: LLM, situation: Situation, messages: list[dict[str, Any]], briefing: dict
) -> dict:
    """초안 하나. 도구를 부르겠다는 응답이 오면 결과가 이미 입력에 있다고 알리고 다시 묻는다."""
    for _ in range(MAX_TOOL_ROUNDS + 1):
        messages.append({"role": "user", "content": json.dumps(briefing, ensure_ascii=False)})
        content, failure = _ask(llm, situation, messages)
        if failure:
            return {"ok": False, "kind": None, "problems": [failure], "content": None}
        result = {**check_response(situation, content), "content": content}
        messages.append(
            {"role": "assistant", "content": content if isinstance(content, str) else ""}
        )
        if result["kind"] != "tool_calls" or not result["ok"]:
            return result
        briefing = {
            **briefing,
            "note": "필요한 도구 결과는 toolResults에 이미 있습니다. 도구를 더 부르지 말고 메시지를 쓰세요.",
        }
    return {
        "ok": False,
        "kind": "tool_calls",
        "problems": [
            _problem(TOO_MANY_TOOL_ROUNDS, "도구만 거듭 불렀고 메시지를 쓰지 않았습니다.")
        ],
        "content": None,
    }


def run_situation(llm: LLM, situation: Situation) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": situation.system}]
    first = _attempt(llm, situation, messages, {**situation.briefing, "rejections": []})
    final = first
    if not first["ok"]:
        rejections = [{**p, "claimId": None, "detail": {}} for p in first["problems"]]
        final = _attempt(llm, situation, messages, {**situation.briefing, "rejections": rejections})
    return {
        "id": situation.id,
        "caseId": situation.case_id,
        "role": situation.role,
        "task": situation.task,
        "allowedTypes": situation.allowed_types,
        "firstPass": first["ok"],
        "finalPass": final["ok"],
        "rewritten": not first["ok"],
        "firstCodes": [p["code"] for p in first["problems"]],
        "finalCodes": [] if final is first else [p["code"] for p in final["problems"]],
        "kind": final["kind"],
    }


def _rate(passed: int, total: int) -> float | None:
    return passed / total if total else None


def run_compliance(llm: LLM, situations: list[Situation] | None = None) -> dict[str, Any]:
    """상황 세트 전체. 첫 시도 통과율, 재작성 뒤 통과율, 검사 항목별 첫 시도 통과율을 낸다."""
    situations = build_situations() if situations is None else situations
    results = [run_situation(llm, s) for s in situations]
    total = len(results)
    first = sum(1 for r in results if r["firstPass"])
    final = sum(1 for r in results if r["finalPass"])
    codes: dict[str, int] = {}
    for result in results:
        for code in result["firstCodes"]:
            codes[code] = codes.get(code, 0) + 1
    by_check = []
    for key, label, wanted in CHECKS:
        passed = sum(1 for r in results if not set(wanted) & set(r["firstCodes"]))
        by_check.append(
            {"key": key, "label": label, "passed": passed, "rate": _rate(passed, total)}
        )
    return {
        "model": model_label(llm),
        "measured": True,
        "situationCount": total,
        "roles": {
            role: sum(1 for r in results if r["role"] == role) for role in ("company", "vendor")
        },
        "firstPassed": first,
        "firstPassRate": _rate(first, total),
        "finalPassed": final,
        "finalPassRate": _rate(final, total),
        "rewritten": sum(1 for r in results if r["rewritten"]),
        "firstFailureCodes": dict(sorted(codes.items())),
        "byCheck": by_check,
        "results": results,
    }


def not_measured(provider: str, reason: str) -> dict[str, Any]:
    """재지 않은 모델의 자리. 숫자를 채우지 않는다."""
    return {
        "model": {"provider": provider, "name": ""},
        "measured": False,
        "status": NOT_MEASURED,
        "reason": reason,
    }


# ── 일부러 틀리는 스크립트 ───────────────────────────────────────────


def _rule(messages, tools, schema) -> dict[str, Any]:
    return json.loads(
        RulePolicy().complete(messages, tools=tools, response_schema=schema)["content"]
    )


def _mistake(kind: str) -> Callable[..., Any]:
    def respond(messages, tools, schema):
        output = _rule(messages, tools, schema)
        if kind == "not_json":
            return "알겠습니다. 조건을 정리해서 다시 말씀드리겠습니다."
        if kind == "number":
            output["text"] = (
                f"{output.get('text') or '확인했습니다.'}\n참고로 단가는 300원 안팎입니다."
            )
        elif kind == "unknown_ref":
            output["text"] = (
                f"{output.get('text') or '확인했습니다.'}\n예상 금액은 {{{{madeUpTotal}}}}입니다."
            )
            output["claimRefs"] = [*output.get("claimRefs", []), "madeUpTotal"]
        elif kind == "missing_key":
            output.pop("stage", None)
        elif kind == "wrong_type":
            output["type"] = "confirm"
        elif kind == "ref_mismatch":
            output["claimRefs"] = []
            output["text"] = (output.get("text") or "") + " {{monthlyKg}}"
        return output

    return respond


def bad_script(situations: list[Situation]) -> ScriptedLLM:
    """일부러 틀리게 답하는 스크립트. 시험이 잘못을 잡는지 확인하는 데만 쓴다(모델이 아니다).

    메시지를 쓰는 상황 가운데 앞의 넷은 한 번 틀렸다가 고치고, 다음 둘은 두 번 다 틀리고,
    나머지는 바로 맞힌다.
    """
    recover = ["not_json", "number", "unknown_ref", "missing_key"]
    stay_wrong = [("wrong_type", "wrong_type"), ("ref_mismatch", "number")]
    rule = RulePolicy()
    responses: list[Any] = []
    for situation in situations:
        messages = [{"role": "user", "content": json.dumps(situation.briefing, ensure_ascii=False)}]
        answer = json.loads(rule.complete(messages)["content"])
        writes = answer.get("type") not in (ACCEPT, ASK_USER) and not answer.get("toolCalls")
        if writes and recover:  # 틀리게 할 것은 메시지를 쓰는 상황에만 건다
            responses += [_mistake(recover.pop(0)), _rule]
        elif writes and stay_wrong:
            first, second = stay_wrong.pop(0)
            responses += [_mistake(first), _mistake(second)]
        else:
            responses.append(_rule)
    return ScriptedLLM(responses, provider="scripted", model="일부러 틀리는 스크립트")


# ── CLI ──────────────────────────────────────────────────────────────


def make_llm(provider: str, args: argparse.Namespace, env: Mapping[str, str]) -> LLM | None:
    """실제 모델은 설정이 있을 때만 만든다. 없으면 None(미측정)."""
    if provider == "rule":
        return RulePolicy()
    if provider == "foundry":
        endpoint, deployment = env.get("FOUNDRY_ENDPOINT"), env.get("FOUNDRY_DEPLOYMENT")
        key = env.get("FOUNDRY_API_KEY")
        if not (endpoint and deployment and key):
            return None
        return FoundryChat(endpoint, args.model or deployment, key)
    if provider == "ollama":
        base = args.base_url or env.get("OLLAMA_BASE_URL")
        if not (base and args.model):
            return None
        return OllamaChat(base, args.model)
    raise ValueError(f"모르는 provider: {provider}")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="모델 형식 준수 시험")
    parser.add_argument(
        "--provider", choices=("rule", "scripted", "foundry", "ollama"), required=True
    )
    parser.add_argument("--model", default="", help="foundry 배포 이름 또는 ollama 모델 이름")
    parser.add_argument("--base-url", default="", help="ollama 주소(없으면 OLLAMA_BASE_URL)")
    parser.add_argument("--limit", type=int, default=SITUATION_COUNT)
    parser.add_argument("--out", type=Path, default=None, help="결과를 쓸 JSON 파일")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    situations = build_situations(args.limit)
    if args.provider == "scripted":
        result = run_compliance(bad_script(situations), situations)
    else:
        llm = make_llm(args.provider, args, env)
        if llm is None:
            result = not_measured(
                args.provider, "모델 연결 설정(주소, 배포·모델 이름, 키)이 없어 재지 않았습니다."
            )
        else:
            result = run_compliance(llm, situations)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    if result.get("measured"):
        sys.stdout.write(
            f"{result['model']['provider']} · {result['model']['name']}: 상황 {result['situationCount']}개, "
            f"첫 시도 통과 {result['firstPassed']}, 재작성 뒤 통과 {result['finalPassed']}\n"
        )
    else:
        sys.stdout.write(f"{args.provider}: {NOT_MEASURED} ({result['reason']})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
