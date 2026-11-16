"""회사 측 에이전트. 배출 기업 하나를 대변해 업체 측 에이전트와 대화한다.

모델이 정하는 것은 무엇을 묻고 어떤 도구를 부르고 결과를 어떻게 설명할지뿐이다. 숫자와 조항은
claim 풀에서만 오고, 근거 검증기를 통과한 메시지만 나간다.
"""

from __future__ import annotations

from typing import Any

from app.v3 import ids
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
    rejection,
    response_schema,
    run_draft,
)
from app.v3.agents.llm import LLM
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.validator import validate_message

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

SYSTEM_PROMPT = "\n".join(
    [
        "당신은 폐기물을 내보내는 배출 기업을 대변하는 회사 측 에이전트다.",
        "처리 업체를 대변하는 업체 측 에이전트와 대화해, 흐름마다 맞는 업체와 조건을 찾는다.",
        "",
        "반드시 지킬 규칙:",
        *[f"- {rule}" for rule in RULES],
        "- 판단은 판단 도구가 한다. 경로, 금액, 등급을 스스로 계산하지 않는다.",
        "- 양쪽 담당자가 승인하기 전에는 확정(confirm)을 보내지 않는다.",
        "- 담당자만 아는 값은 type을 ask_user로 하고 userQuestions에 항목을 적는다.",
        "- 상대가 낸 조건을 그대로 받아들이면 type을 accept로 한다(메시지는 나가지 않는다).",
        "- 현행 처리 단가는 상대 업체에 알리지 않는다.",
        "",
        TOOL_HINT,
        "응답 JSON: type, stage, streamIds, text, claimRefs, toolCalls (필요하면 asks, userQuestions).",
    ]
)


def short(stream_id: str) -> str:
    return stream_id.removeprefix("st_")


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
                raise ValueError("이 대화에 없는 흐름입니다. streamId를 확인하세요.")
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
                    raise ValueError("streamIds에 이 대화의 흐름 ID를 넣으세요.")
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

        def briefing(rejections: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "role": "company",
                "task": task,
                "today": self.today,
                "org": {"id": self.org_id, "name": self.org.get("name")},
                "counterpart": {"id": vendor_org.get("id"), "name": vendor_org.get("name")},
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
                        "name": s.get("name"),
                        "wasteItem": s.get("wasteItem"),
                        "material": s.get("material"),
                        "origin": s.get("origin"),
                        "form": s.get("form"),
                        "contaminants": [
                            c.get("name") or c.get("label") for c in s.get("contaminants") or []
                        ],
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
                    "asked": bool(uncovered) and set(uncovered) <= asked_streams,
                    "agreed": list(state.get("agreed") or []),
                    "cannotAskUser": bool(state.get("cannotAskUser")),
                },
                "userQuestionFields": sorted(questions.FIELDS),
                "tools": list(TOOLS),
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
                self.org.get("name"),
                vendor_org.get("name"),
                *[x.get("name") for x in streams],
            ]
            result = validate_message(message, self.registry, today=self.today, names=names)
            found += [r.to_dict() for r in result.rejections]
            return found

        outcome = run_draft(
            llm=self.llm,
            system=SYSTEM_PROMPT,
            briefing=briefing,
            schema=RESPONSE_SCHEMA,
            pool=pool,
            tools=tools,
            make=make,
            check=check,
            pseudo_types=pseudo,
            trace=trace,
            actor=ACTOR,
        )
        return outcome, trace
