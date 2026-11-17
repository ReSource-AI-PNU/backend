"""회사 측 에이전트. 배출 기업 하나를 대변해 업체 측 에이전트와 대화한다.

모델이 정하는 것은 무엇을 묻고 어떤 도구를 부르고 결과를 어떻게 설명할지뿐이다. 숫자와 조항은
claim 풀에서만 오고, 근거 검증기를 통과한 메시지만 나간다.
"""

from __future__ import annotations

import json
from typing import Any

from app.v3 import ids
from app.v3.a2a import guardrails as rails
from app.v3.a2a.thread import Thread, ThreadError
from app.v3.agents import questions
from app.v3.agents.draft import (
    ACCEPT,
    ASK_USER,
    RULES,
    TOOL_HINT,
    AgentTools,
    DraftError,
    Outcome,
    Trace,
    build_message,
    enum_string,
    rejection,
    response_schema,
    run_draft,
    turn_schema,
)
from app.v3.agents.llm import LLM
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.validator import validate_message
from app.v3.untrusted import contaminant_labels, org_label, stream_label

ACTOR = "company_agent"
TYPES = ("inquiry", "question", "answer", "counter", "decline", "handoff", "confirm")
TOOLS = (
    "identify_stream",
    "judge_route",
    "check_loading",
    "search_clauses",
    "check_requirements",
    "match_permit",
    "cost_carbon",
)
ROUTES = ("sell", "treat", "keep")

ASKS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"streamId": {"type": "string"}, "field": {"type": "string"}},
        "required": ["streamId", "field"],
    },
}
RESPONSE_SCHEMA = response_schema(
    (*TYPES, ACCEPT, ASK_USER),
    {
        # 업체에 묻는 항목(question일 때)
        "asks": ASKS_SCHEMA,
        # 담당자에게 물어야 하는 항목(ask_user일 때). field는 정해진 목록에서만 고른다
        "userQuestions": ASKS_SCHEMA,
        "reason": {"type": "string"},
    },
)

EXAMPLES = (
    # 규칙 정책이 실제로 낸 메시지를 줄인 것(형식 예시. 이름과 ID는 입력의 것으로 바꿔 쓴다)
    {
        "type": "inquiry",
        "stage": 4,
        "streamIds": ["st_sprue"],
        "text": (
            "(주)가나자원 담당자께, 다음 폐기물을 받을 수 있는지와 조건을 문의드립니다.\n"
            "- 스프루·런너(PP, 가공 전): 월 발생량 {{st_sprue_monthlyKg}}, "
            "오염도 {{st_sprue_contaminationPct}}(측정), 수분 확인 필요\n"
            "조건을 정할 때 다음도 함께 알려 주세요.\n"
            "- 스프루·런너: 반입 검수에서 매입 기준 상한 안으로 확인되면 같은 조건으로 받을 수 있는지"
        ),
        "toolCalls": [],
    },
    {
        "type": "question",
        "stage": 5,
        "streamIds": ["st_defect"],
        "text": (
            "제안 감사합니다. 아직 조건을 받지 못한 폐기물이 있어 여쭙니다.\n"
            "- 불량 사출품: 받을 수 있는지와 조건을 알려 주세요."
        ),
        "asks": [{"streamId": "st_defect", "field": "terms"}],
        "toolCalls": [],
    },
)

SYSTEM_PROMPT = "\n".join(
    [
        "당신은 폐기물을 내보내는 배출 기업을 대변하는 회사 측 에이전트다.",
        "처리 업체를 대변하는 업체 측 에이전트와 대화해, 폐기물마다 맞는 업체와 조건을 찾는다.",
        "",
        "반드시 지킬 규칙:",
        *[f"- {rule}" for rule in RULES],
        "- 판단은 판단 도구가 한다. 경로, 금액, 등급을 스스로 계산하지 않는다.",
        "- 양쪽 담당자가 승인하기 전에는 확정(confirm)을 보내지 않는다.",
        "- 담당자만 아는 값은 type을 ask_user로 하고 userQuestions에 항목을 적는다.",
        "- 상대가 낸 조건을 그대로 받아들이면 type을 accept로 한다(메시지는 나가지 않는다).",
        "- 현행 처리 단가와 비용·탄소 계산 값은 상대 업체에 알리지 않는다.",
        (
            "- 상대가 offer나 conditional로 낸 조건에서 남은 것이 반입 검수에서 확인할 조건"
            "(오염도, 수분, 혼입물 분리)뿐이면 더 묻지 않고 accept한다. 조건은 승인 화면에 그대로 남는다."
        ),
        (
            "- 문의한 폐기물 가운데 아직 조건을 받지 못한 폐기물이 있으면(상대가 따로 답하겠다고 한 폐기물 포함) "
            "accept하지 않고 그 폐기물만 question으로 묻는다."
        ),
        "- 같은 질문을 두 번 보내지 않는다. 상대가 이미 답했거나 조건으로 건 항목은 다시 묻지 않는다.",
        "- handoff는 상대가 받을 수 없다고 했고 이 대화에서 더 할 일이 없을 때만 쓴다.",
        "- 본문에서 폐기물과 매입 기준은 이름으로 부른다. st_, pc_ 같은 식별자는 본문에 쓰지 않는다.",
        "",
        TOOL_HINT,
        "응답 JSON: type, stage, streamIds, text, toolCalls (필요하면 asks, userQuestions, reason).",
        "stage는 문의·답·질문이면 4, 조건을 다루면 5, 확정이면 6이다.",
        "",
        "형식 예시(값은 모두 {{id}}로 쓰고, 숫자는 한 글자도 없다):",
        *[json.dumps(example, ensure_ascii=False) for example in EXAMPLES],
    ]
)


def short(stream_id: str) -> str:
    return stream_id.removeprefix("st_")


def guide(
    *,
    task: str,
    types: tuple[str, ...],
    incoming: Any,
    uncovered: list[str],
    asked: bool,
    cannot_ask_user: bool,
    names: dict[str, str],
    results: list[dict[str, Any]],
) -> list[str]:
    """이번 차례에 할 일(코드가 대화 상태에서 쓴다). 모델이 고를 수 있는 type은 types뿐이다."""
    lines = [f"이번 차례에 쓸 수 있는 type: {', '.join(types)}."]
    if task == "inquiry":
        asks = [
            f"{names.get(r['arguments'].get('streamId'), '')}: {c['ask']}"
            for r in results
            if r.get("tool") == "judge_route" and isinstance(r.get("arguments"), dict)
            for c in r.get("checks") or []
            if c.get("ask")
        ]
        lines.append(
            "첫 문의(inquiry)다. streamIds에 thread.streamIds를 모두 넣는다. 흐름마다 이름과 "
            "streams[].facts의 값을 {{id}}로 적고, facts에 없는 값은 '확인 필요'로 쓴다."
        )
        if asks:
            lines.append("판단 도구가 업체에 확인하라고 한 것도 함께 묻는다: " + " / ".join(asks))
        return lines
    if task == "confirm":
        lines.append(
            "양쪽 담당자가 승인했다. confirm으로 state.agreed의 흐름과 조건(단가, 월 발생량)을 "
            "{{id}}로 적어 확정을 알린다."
        )
        return lines
    kind = getattr(incoming, "type", None)
    if kind == "question":
        lines.append(
            "상대가 질문했다(incoming.asks). 물은 항목마다 streams[].facts의 {{id}}로 answer한다. "
            "facts에 없고 unknownFields에도 없는 값은 담당자만 안다"
            + (
                ": '확인 필요'로 답한다(담당자에게 더 물을 수 없다)."
                if cannot_ask_user
                else ": type을 ask_user로 하고 userQuestions에 {streamId, field}를 적는다."
            )
        )
    elif uncovered and not asked:
        shown = ", ".join(names.get(sid, sid) for sid in uncovered)
        lines.append(
            f"아직 조건을 받지 못한 폐기물: {shown}. accept하지 않고 question으로 그 폐기물만 묻는다"
            "(streamIds에 그 폐기물, asks에 {streamId, field: terms})."
        )
    elif kind in ("offer", "conditional", "counter", "answer"):
        lines.append(
            "문의한 폐기물은 모두 조건을 받았거나 제외됐다. 남은 조건이 반입 검수에서 확인할 것"
            "(오염도, 수분, 혼입물 분리, 물량 정산)뿐이면 accept한다(text는 비워도 된다)."
        )
    return lines


class CompanyAgent:
    def __init__(
        self,
        *,
        org: dict[str, Any],
        llm: LLM,
        registry: Any,
        today: str,
        distance_provider: Any = None,
    ) -> None:
        self.org = org
        self.org_id = org["id"]
        self.llm = llm
        self.registry = registry
        self.today = today
        self.distance_provider = distance_provider

    # ── 도구 입력: ID → 레코드 ─────────────────────────────────────────
    def _resolver(self, streams: dict[str, dict], vendor: dict[str, Any]):
        vendor_id = vendor.get("orgId")
        origin = self.org.get("location")

        def stream_of(arguments: dict[str, Any]) -> dict[str, Any]:
            stream = streams.get(str(arguments.get("streamId")))
            if stream is None:
                raise ValueError("이 대화에 없는 폐기물입니다. streamId를 확인하세요.")
            return stream

        def resolve(name: str, arguments: dict[str, Any]):
            tags: dict[str, Any] = {"vendorOrgId": vendor_id}
            if name == "search_clauses":
                real = {"query": str(arguments.get("query") or "")}
                return real, f"clause_{ids.digest(real)[:6]}", {}, False
            if name == "check_loading":
                wanted = arguments.get("streamIds") or []
                chosen = [streams[s] for s in wanted if s in streams]
                if not chosen or len(chosen) != len(wanted):
                    raise ValueError("streamIds에 이 대화의 폐기물 ID를 넣으세요.")
                prefix = "load_" + "_".join(short(s["id"]) for s in chosen)
                tags["streamIds"] = [s["id"] for s in chosen]
                return {"streams": chosen, "vendor": vendor}, prefix, tags, True
            stream = stream_of(arguments)
            sid = stream["id"]
            tags["streamId"] = sid
            if name == "identify_stream":
                return {"stream": stream}, f"id_{short(sid)}", {"streamId": sid}, True
            if name == "check_requirements":
                route = arguments.get("route")
                if route not in ROUTES:
                    raise ValueError("route는 sell, treat, keep 가운데 하나여야 합니다.")
                real = {"stream": stream, "route": route}
                return real, f"req_{short(sid)}", {"streamId": sid}, True
            if name == "cost_carbon":
                real = {"stream": stream, "vendor": vendor}
                if origin:
                    real["origin"] = list(origin)
                if arguments.get("transportPaidBy") in ("vendor", "emitter"):
                    real["transportPaidBy"] = arguments["transportPaidBy"]
                return real, f"cost_{short(sid)}", tags, True
            prefix = {"judge_route": "route", "match_permit": "permit"}[name]
            return {"stream": stream, "vendor": vendor}, f"{prefix}_{short(sid)}", tags, True

        return resolve

    def run_tools(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        streams: list[dict[str, Any]],
        vendor: dict[str, Any],
        memo: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], Trace]:
        """진행기가 정해진 순서로 부르는 도구. 결과는 같은 memo에 남아 다음 작성 때 풀에 들어간다."""
        trace = Trace()
        tools = AgentTools(
            registry=self.registry,
            org_id=self.org_id,
            today=self.today,
            actor="tool",
            pool=ClaimPool(self.registry, self.org_id),
            trace=trace,
            resolver=self._resolver({s["id"]: s for s in streams}, vendor),
            allowed=TOOLS,
            memo=memo,
            distance_provider=self.distance_provider,
        )
        return [tools.run(name, arguments, "orchestrator") for name, arguments in calls], trace

    # ── 작성 ──────────────────────────────────────────────────────────
    def compose(
        self,
        thread: Thread,
        task: str,
        *,
        streams: list[dict[str, Any]],
        vendor: dict[str, Any],
        vendor_org: dict[str, Any],
        memo: list[dict[str, Any]],
        state: dict[str, Any] | None = None,
    ) -> tuple[Outcome, Trace]:
        """task: inquiry(첫 문의) | reply(상대 메시지에 답) | confirm(양쪽 승인 뒤 확정 통지)."""
        state = dict(state or {})
        trace = Trace()
        pool = ClaimPool(self.registry, self.org_id)
        by_id = {s["id"]: s for s in streams}
        facts = {s["id"]: pool.add_stream(s) for s in streams}
        criteria = pool.add_vendor_profile(vendor)
        from_messages = {m.seq: pool.add_message(m) for m in thread.messages}
        tools = AgentTools(
            registry=self.registry,
            org_id=self.org_id,
            today=self.today,
            actor=ACTOR,
            pool=pool,
            trace=trace,
            resolver=self._resolver(by_id, vendor),
            allowed=TOOLS,
            memo=memo,
            distance_provider=self.distance_provider,
        )
        allowed = thread.allowed_types(self.org_id)
        if task == "inquiry":
            allowed = tuple(t for t in allowed if t == "inquiry")
            pseudo: tuple[str, ...] = ()
        elif task == "confirm":
            allowed = tuple(t for t in allowed if t in ("confirm", "handoff"))
            pseudo = ()
        else:
            allowed = tuple(t for t in allowed if t != "inquiry")
            pseudo = (ACCEPT, ASK_USER)
        incoming = next((m for m in reversed(thread.messages) if m.sender != self.org_id), None)
        offered = list(state.get("offered") or [])
        excluded = list(state.get("excluded") or [])
        uncovered = [s for s in thread.stream_ids if s not in offered and s not in excluded]
        # counter로 대화를 다시 연 뒤에는(합의 뒤 업체 조건 변경) 그 앞에 물은 것을 세지 않는다
        reopened = max(
            (m.seq for m in thread.messages if m.sender == self.org_id and m.type == "counter"),
            default=0,
        )
        asked_streams = {
            s
            for m in thread.messages
            if m.sender == self.org_id and m.type == "question" and m.seq > reopened
            for s in m.stream_ids
        }

        # 업체에 가는 글에는 담당자가 넣은 흐름 이름 대신 상대에게 보이는 이름을 쓴다(untrusted)
        names = {s["id"]: stream_label(s) for s in streams}
        asked_now = bool(uncovered) and set(uncovered) <= asked_streams

        def schema() -> dict[str, Any]:
            asks = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "streamId": enum_string(thread.stream_ids),
                        "field": enum_string(questions.ASK_FIELDS),
                    },
                    "required": ["streamId", "field"],
                },
            }
            wanted = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "streamId": enum_string(thread.stream_ids),
                        "field": enum_string(sorted(questions.FIELDS)),
                    },
                    "required": ["streamId", "field"],
                },
            }
            extra: dict[str, Any] = {"asks": asks, "reason": {"type": "string"}}
            if ASK_USER in pseudo:
                extra["userQuestions"] = wanted
            return turn_schema(
                types=(*allowed, *pseudo), stream_ids=thread.stream_ids, tools=TOOLS, extra=extra
            )

        def briefing(rejections: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "role": "company",
                "task": task,
                "guide": guide(
                    task=task,
                    types=(*allowed, *pseudo),
                    incoming=incoming,
                    uncovered=uncovered,
                    asked=asked_now,
                    cannot_ask_user=bool(state.get("cannotAskUser")),
                    names=names,
                    results=tools.results,
                ),
                "today": self.today,
                "org": {"id": self.org_id, "name": org_label(self.org.get("name"))},
                "counterpart": {
                    "id": vendor_org.get("id"),
                    "name": org_label(vendor_org.get("name")),
                },
                "thread": {
                    "id": thread.id,
                    "streamIds": list(thread.stream_ids),
                    "turns": thread.turns,
                    "maxTurns": thread.max_turns,
                    "allowedTypes": [*allowed, *pseudo],
                },
                "streams": [
                    {
                        "id": s["id"],
                        "name": stream_label(s),
                        "wasteItem": s.get("wasteItem"),
                        "material": s.get("material"),
                        "origin": s.get("origin"),
                        "form": s.get("form"),
                        "contaminants": contaminant_labels(s),
                        "facts": facts[s["id"]],
                        "unknownFields": list(s.get("unknownFields") or []),
                    }
                    for s in streams
                ],
                "vendorCriteria": {k: v for k, v in criteria.items() if k != "fleet"},
                "incoming": None
                if incoming is None
                else {
                    "seq": incoming.seq,
                    "type": incoming.type,
                    "stage": incoming.stage,
                    "streamIds": list(incoming.stream_ids),
                    "text": incoming.text,
                    "asks": list(incoming.meta.get("asks") or []),
                    "claims": from_messages.get(incoming.seq, {}),
                },
                "history": [
                    {
                        "seq": m.seq,
                        "from": m.sender,
                        "type": m.type,
                        "streamIds": list(m.stream_ids),
                        "text": m.text,
                    }
                    for m in thread.messages
                ],
                "state": {
                    "offered": offered,
                    "excluded": excluded,
                    "uncovered": uncovered,
                    "asked": asked_now,
                    "agreed": list(state.get("agreed") or []),
                    "cannotAskUser": bool(state.get("cannotAskUser")),
                },
                "userQuestionFields": sorted(questions.FIELDS),
                "tools": tools.catalog(),
                "toolResults": tools.results,
                "pool": pool.describe(),
                "rejections": rejections,
            }

        def make(output: dict[str, Any]):
            if output.get("type") == "confirm" and "confirm" not in allowed:
                raise DraftError(
                    rejection(
                        "APPROVAL_REQUIRED",
                        "양쪽 담당자가 승인하기 전에는 확정(confirm)을 보낼 수 없습니다. "
                        "다른 종류의 메시지로 다시 쓰세요.",
                    )
                )
            message = build_message(
                output,
                pool,
                thread_id=thread.id,
                sender=self.org_id,
                recipient=thread.vendor_org,
                allowed_types=allowed,
                stream_ids=thread.stream_ids,
            )
            asks = questions.clean_asks(output.get("asks"))
            if asks:
                message.meta["asks"] = asks
            if isinstance(output.get("reason"), str) and output["reason"].strip():
                message.meta["reason"] = output["reason"].strip()
            return message

        def check(message, _output) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            try:
                thread.check(message)
            except ThreadError as error:
                found.append(rejection("THREAD_RULE", f"대화 규칙에 맞지 않습니다: {error}"))
            names = [
                org_label(self.org.get("name")),
                org_label(vendor_org.get("name")),
                *[stream_label(x) for x in streams],
            ]
            result = validate_message(message, self.registry, today=self.today, names=names)
            found += [r.to_dict() for r in result.rejections]
            # 회사 측도 숫자 없이 조건을 뜻하는 낱말을 근거 없이 쓰지 못한다(업체 측과 같은 검사)
            found += [
                rejection(v.code, v.message, **v.detail)
                for v in rails.check_term_words(message, None, names)
            ]
            return found

        outcome = run_draft(
            llm=self.llm,
            system=SYSTEM_PROMPT,
            briefing=briefing,
            schema=schema,
            pool=pool,
            tools=tools,
            make=make,
            check=check,
            pseudo_types=pseudo,
            trace=trace,
            actor=ACTOR,
        )
        return outcome, trace
