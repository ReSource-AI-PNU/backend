"""업체 측 에이전트. 처리 업체 하나를 대변해 회사 측 에이전트의 메시지에 답한다.

근거 검증기에 더해 두 가지를 통과해야 메시지가 나간다.
- 가드레일(하한·상한 단가, 가능 요일, 월 최소 횟수, 자동 거절 조건): guardrails.enforce
- 다른 배출 기업의 이름·흐름·물량을 드러내지 않는지: policy.check_disclosure
업체는 자기 프로필의 model 설정(foundry | ollama)으로 모델을 고르고, 실제로 쓴 구현 이름이
답 메시지의 meta.model에 남는다.
"""

from __future__ import annotations

from typing import Any

from app.v3 import ids
from app.v3.a2a import guardrails as rails
from app.v3.a2a.message import Message
from app.v3.a2a.policy import check_disclosure
from app.v3.a2a.thread import Thread, ThreadError
from app.v3.agents.draft import (
    RULES,
    TOOL_HINT,
    AgentTools,
    Outcome,
    Trace,
    build_message,
    make_handoff,
    model_label,
    rejection,
    response_schema,
    run_draft,
)
from app.v3.agents.llm import LLM
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.units import UnitError, convert
from app.v3.evidence.validator import validate_message

ACTOR = "vendor_agent"
TYPES = ("question", "answer", "offer", "counter", "conditional", "decline", "handoff")
TOOLS = (
    "identify_stream",
    "judge_route",
    "check_loading",
    "search_clauses",
    "match_permit",
    "cost_carbon",
    "route_extra_cost",
)
SHARED_THREAD_STATUSES = ("agreed", "confirmed")

TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "streamId": {"type": "string"},
                    # 단가는 숫자로 적지 않고 claim ID로 가리킨다
                    "unitPriceRef": {"type": "string"},
                },
                "required": ["streamId", "unitPriceRef"],
            },
        },
        "days": {"type": "array", "items": {"type": "string", "enum": list(rails.WEEKDAYS)}},
        "pickupsPerMonthRef": {"type": "string"},
        "conditions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items"],
}
ASKS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"streamId": {"type": "string"}, "field": {"type": "string"}},
        "required": ["streamId", "field"],
    },
}
RESPONSE_SCHEMA = response_schema(
    TYPES,
    {
        "terms": TERMS_SCHEMA,  # offer, counter, conditional일 때 반드시
        "asks": ASKS_SCHEMA,  # question일 때 배출 기업에 묻는 항목
        "excluded": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"streamId": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["streamId", "reason"],
            },
        },
        "reason": {"type": "string"},
    },
)

SYSTEM_PROMPT = "\n".join(
    [
        "당신은 폐기물 처리 업체를 대변하는 업체 측 에이전트다.",
        "회사 측 에이전트의 문의에 question, answer, offer, conditional, counter, decline으로 답한다.",
        "",
        "반드시 지킬 규칙:",
        *[f"- {rule}" for rule in RULES],
        "- 판단은 판단 도구가 한다. 단가와 물량을 스스로 계산하지 않는다.",
        "- 업체가 정한 가드레일(하한·상한 단가, 가능 요일, 월 최소 횟수, 자동 거절 조건)을 지킨다.",
        "- 조건을 내는 메시지(offer, counter, conditional)에는 terms를 넣는다.",
        "- terms의 단가는 숫자로 적지 않고 unitPriceRef에 claim ID로 적는다.",
        "- 허가 품목이 맞지 않으면 근거와 함께 decline으로 답한다.",
        "- 다른 배출 기업의 이름, 흐름, 물량을 알리지 않는다.",
        "",
        TOOL_HINT,
        "응답 JSON: type, stage, streamIds, text, claimRefs, toolCalls, terms, asks, excluded, reason.",
    ]
)

# 상대(배출 기업)에게 보이는 기록에는 거부 사유를 코드와 일반 문장으로만 남긴다.
# 가드레일 수치나 다른 배출 기업의 근거 ID가 실행 기록으로 새지 않게 한다.
_PUBLIC_MESSAGES = {
    "NUMBER_OUTSIDE_PLACEHOLDER": "본문에 자리표시자 밖 숫자가 있었습니다.",
    "UNKNOWN_CLAIM_REF": "claim 풀에 없는 ID를 썼습니다.",
    "UNKNOWN_PLACEHOLDER": "본문의 자리표시자에 맞는 claim이 없었습니다.",
    "UNUSED_CLAIM": "본문에 쓰이지 않은 claim이 있었습니다.",
    "VALUE_MISMATCH": "claim의 값이 근거와 달랐습니다.",
    "QUOTE_MISMATCH": "조항 인용이 원문과 달랐습니다.",
    "EVIDENCE_FORBIDDEN": "보내는 조직이 볼 수 없는 근거를 썼습니다.",
    "EVIDENCE_NOT_FOUND": "근거 저장소에 없는 근거를 썼습니다.",
    "EVIDENCE_EXPIRED": "유효 기간이 지난 근거를 썼습니다.",
    "TYPE_NOT_ALLOWED": "지금 보낼 수 없는 종류의 메시지였습니다.",
    "INVALID_OUTPUT": "응답 형식이 맞지 않았습니다.",
    "MODEL_ERROR": "모델 호출에 실패했습니다.",
}


def public_rejections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        code = str(item.get("code"))
        if code.startswith("GUARDRAIL_"):
            message = "업체 가드레일 밖의 조건이었습니다."
        elif code.startswith("OTHER_EMITTER"):
            message = "다른 배출 기업의 정보를 드러내는 내용이었습니다."
        else:
            message = _PUBLIC_MESSAGES.get(code, "초안이 검사를 통과하지 못했습니다.")
        entry = {"code": code, "message": message, "claimId": None, "detail": {}}
        if "attempt" in item:
            entry["attempt"] = item["attempt"]
        out.append(entry)
    return out


def public_profile(profile: dict[str, Any], *, fleet_cost: bool = True) -> dict[str, Any]:
    """판단 도구에 넘기는 업체 프로필. 하한·상한 단가와 자동 거절 조건, 모델 설정은 뺀다."""
    guard = profile.get("guardrails") or {}
    fleet = dict(profile.get("fleet") or {})
    if not fleet_cost:
        fleet.pop("costPerKm", None)
        fleet.pop("evidence", None)
    return {
        "orgId": profile.get("orgId"),
        "permits": list(profile.get("permits") or []),
        "criteria": list(profile.get("criteria") or []),
        "fleet": fleet,
        "guardrails": {
            k: guard[k] for k in ("days", "minPickupsPerMonth") if guard.get(k) is not None
        },
    }


def short(stream_id: str) -> str:
    return stream_id.removeprefix("st_")


class VendorAgent:
    def __init__(
        self,
        *,
        store: Any,
        registry: Any,
        today: str,
        llm: LLM,
        distance_provider: Any = None,
        memo: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.today = today
        self.llm = llm
        self.distance_provider = distance_provider
        self.memo = memo if memo is not None else {}

    # ── 도구 입력: ID → 레코드 ─────────────────────────────────────────
    def _other_stops(self, vendor_org: str, emitter_org: str) -> list[dict[str, Any]]:
        """이 업체가 이미 맡은 다른 배출 기업의 수거 지점. 노선 계산에만 쓰고 밖으로 내지 않는다."""
        stops = []
        for other in self.store.list_threads(org_id=vendor_org):
            if (
                other.get("vendorOrgId") != vendor_org
                or other.get("emitterOrgId") == emitter_org
                or other.get("status") not in SHARED_THREAD_STATUSES
            ):
                continue
            org = self.store.get_org(other["emitterOrgId"])
            total = 0.0
            for stream_id in other.get("streamIds") or []:
                total += float(self.store.get_stream(stream_id).get("monthlyKg") or 0)
            if org.get("location") and total > 0:
                stops.append({"id": other["id"], "location": org["location"], "kg": total})
        return stops

    def _resolver(self, streams: dict[str, dict], profile: dict[str, Any], emitter: dict):
        vendor_id = profile["orgId"]
        tool_profile = public_profile(profile)
        origin = emitter.get("location")
        pickups = (profile.get("guardrails") or {}).get("minPickupsPerMonth")

        def pick(arguments: dict[str, Any]) -> list[dict[str, Any]]:
            wanted = arguments.get("streamIds") or []
            chosen = [streams[s] for s in wanted if s in streams]
            if not chosen or len(chosen) != len(wanted):
                raise ValueError("streamIds에 이 대화의 흐름 ID를 넣으세요.")
            return chosen

        def resolve(name: str, arguments: dict[str, Any]):
            tags: dict[str, Any] = {"vendorOrgId": vendor_id}
            if name == "search_clauses":
                real = {"query": str(arguments.get("query") or "")}
                return real, f"clause_{ids.digest(real)[:6]}", {}, False
            if name == "check_loading":
                chosen = pick(arguments)
                tags["streamIds"] = [s["id"] for s in chosen]
                prefix = "load_" + "_".join(short(s["id"]) for s in chosen)
                return {"streams": chosen, "vendor": tool_profile}, prefix, tags, False
            if name == "route_extra_cost":
                chosen = pick(arguments)
                if not origin:
                    raise ValueError("배출 기업의 위치가 없어 노선을 계산할 수 없습니다.")
                divisor = float(pickups) if pickups else None
                total = sum(float(s.get("monthlyKg") or 0) for s in chosen)
                new_stop: dict[str, Any] = {
                    "id": emitter.get("id"),
                    "location": list(origin),
                    "kg": total / divisor if divisor else total,
                }
                densities = [s.get("bulkDensityKgPerM3") for s in chosen]
                if all(isinstance(d, (int, float)) and d > 0 for d in densities):
                    volume = sum(
                        float(s.get("monthlyKg") or 0) / float(d)
                        for s, d in zip(chosen, densities, strict=True)
                    )
                    new_stop["m3"] = volume / divisor if divisor else volume
                stops = self._other_stops(vendor_id, str(emitter.get("id")))
                tags["streamIds"] = [s["id"] for s in chosen]
                # 다른 배출 기업의 지점이 들어간 계산은 풀에 넣지 않는다
                tags["internal"] = bool(stops)
                real = {"vendor": tool_profile, "stops": stops, "newStop": new_stop}
                prefix = "routecost_" + "_".join(short(s["id"]) for s in chosen)
                return real, prefix, tags, False
            stream = streams.get(str(arguments.get("streamId")))
            if stream is None:
                raise ValueError("이 대화에 없는 흐름입니다. streamId를 확인하세요.")
            sid = stream["id"]
            tags["streamId"] = sid
            if name == "identify_stream":
                return {"stream": stream}, f"id_{short(sid)}", {"streamId": sid}, False
            if name == "cost_carbon":
                real = {"stream": stream, "vendor": tool_profile}
                if origin:
                    real["origin"] = list(origin)
                return real, f"cost_{short(sid)}", tags, True
            prefix = {"judge_route": "route", "match_permit": "permit"}[name]
            # 현행 처리 단가는 업체가 모르는 값이라 도구가 가정값을 쓴다. 그 값은 풀에 넣지 않는다
            return {"stream": stream, "vendor": tool_profile}, f"{prefix}_{short(sid)}", tags, True

        return resolve

    # ── 조건(terms)과 가드레일 ─────────────────────────────────────────
    def _terms(
        self, output: dict[str, Any], message: Message, pool: ClaimPool
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """모델이 claim ID로 가리킨 조건을 값으로 바꾼다. (terms, 거부 사유)."""
        if message.type not in rails.ENFORCED_TYPES:
            return None, []
        raw = output.get("terms")
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list) or not raw["items"]:
            return None, [
                rejection(
                    "GUARDRAIL_TERMS_MISSING",
                    "조건을 내는 메시지에는 terms.items가 있어야 합니다. 흐름마다 streamId와 "
                    "unitPriceRef(claim ID)를 넣으세요.",
                )
            ]
        claims = {c.id: c for c in message.claims}
        found: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        for item in raw["items"]:
            sid = str((item or {}).get("streamId"))
            ref = str((item or {}).get("unitPriceRef"))
            claim = claims.get(ref)
            if sid not in message.stream_ids or claim is None:
                found.append(
                    rejection(
                        "GUARDRAIL_TERMS_REF",
                        "terms.items의 streamId는 메시지의 흐름이어야 하고, unitPriceRef는 "
                        "본문에 쓴 claim ID여야 합니다.",
                    )
                )
                continue
            try:
                price = convert(float(claim.value), claim.unit, "원/kg")
            except (UnitError, TypeError, ValueError):
                found.append(
                    rejection("GUARDRAIL_TERMS_REF", "unitPriceRef는 kg당 단가 claim이어야 합니다.")
                )
                continue
            items.append({"streamId": sid, "unitPriceKrwPerKg": price, "claimId": ref})
        declared = {item["claimId"] for item in items}
        loose = [
            c.id
            for c in message.claims
            if c.id not in declared
            and (
                (pool.get(c.id) and pool.get(c.id).tags.get("key")) == "unitPrice"
                or (pool.get(c.id) and pool.get(c.id).tags.get("field")) == "unitPrice"
            )
        ]
        if loose:
            found.append(
                rejection(
                    "GUARDRAIL_TERMS_INCOMPLETE",
                    "본문에 쓴 단가 claim은 모두 terms.items에 넣어야 합니다.",
                )
            )
        pickups = None
        ref = raw.get("pickupsPerMonthRef")
        if ref:
            claim = claims.get(str(ref))
            if claim is None or not isinstance(claim.value, (int, float)):
                found.append(
                    rejection(
                        "GUARDRAIL_TERMS_REF",
                        "pickupsPerMonthRef는 본문에 쓴 수거 횟수 claim ID여야 합니다.",
                    )
                )
            else:
                pickups = float(claim.value)
        days = [str(d) for d in raw.get("days") or []]
        conditions = [str(c) for c in raw.get("conditions") or []]
        terms = {
            "items": items,
            "days": days,
            "pickupsPerMonth": pickups,
            "conditions": conditions,
        }
        return terms, found

    def _guard(
        self,
        guard: dict[str, Any],
        message: Message,
        terms: dict[str, Any] | None,
        streams: dict[str, dict],
    ) -> list[dict[str, Any]]:
        if terms is None:
            return []
        found: dict[str, dict[str, Any]] = {}
        for item in terms["items"]:
            single = {
                "unitPriceKrwPerKg": item["unitPriceKrwPerKg"],
                "days": terms["days"],
                "pickupsPerMonth": terms["pickupsPerMonth"],
                "conditions": terms["conditions"],
            }
            try:
                rails.enforce(guard, message, single)
            except rails.GuardrailViolation as error:
                for violation in error.violations:
                    found[violation.code] = rejection(
                        f"GUARDRAIL_{violation.code}",
                        f"{violation.message} 가드레일 안의 조건으로 다시 쓰거나 다른 종류로 답하세요.",
                    )
            reason = rails.auto_decline_reason(guard, streams.get(item["streamId"]) or {})
            if reason:
                found["AUTO_DECLINE_STREAM"] = rejection(
                    "GUARDRAIL_AUTO_DECLINE_STREAM",
                    f"{reason} 이 흐름에는 조건을 낼 수 없습니다.",
                )
        return list(found.values())

    # ── 답하기 ────────────────────────────────────────────────────────
    def handle(self, vendor_org: str, message: dict[str, Any], context: dict[str, Any]) -> dict:
        """받은 메시지 하나에 답 메시지(dict) 하나. meta에 validation, model, trace가 붙는다."""
        profile = self.store.get_vendor_profile(vendor_org)
        org = self.store.get_org(vendor_org)
        thread = Thread.from_dict(context["thread"])
        if thread.vendor_org != vendor_org:
            raise ValueError("이 업체의 대화가 아니다")
        emitter = dict(context.get("emitter") or {"id": thread.emitter_org})
        streams = [dict(s) for s in context.get("streams") or [] if s["id"] in thread.stream_ids]
        for stream in streams:
            stream.pop("currentTreatment", None)
        by_id = {s["id"]: s for s in streams}
        guard = profile.get("guardrails") or {}
        trace = Trace()
        pool = ClaimPool(self.registry, vendor_org)
        facts = {s["id"]: pool.add_stream(s) for s in streams}
        criteria = pool.add_vendor_profile(profile)
        if not any(m.seq == message.get("seq") for m in thread.messages):
            pool.add_message(message)
        from_messages = {m.seq: pool.add_message(m) for m in thread.messages}
        tools = AgentTools(
            registry=self.registry,
            org_id=vendor_org,
            today=self.today,
            actor=ACTOR,
            pool=pool,
            trace=trace,
            resolver=self._resolver(by_id, profile, emitter),
            allowed=TOOLS,
            memo=self.memo.setdefault(thread.id, []),
            distance_provider=self.distance_provider,
        )
        allowed = thread.allowed_types(vendor_org)
        offered = sorted(
            {
                item.get("streamId")
                for m in thread.messages
                if m.sender == vendor_org
                for item in (m.meta.get("terms") or {}).get("items") or []
            }
        )
        asked = [
            ask
            for m in thread.messages
            if m.sender == vendor_org and m.type == "question"
            for ask in m.meta.get("asks") or []
        ]
        holder: dict[str, Any] = {}

        def briefing(rejections: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "role": "vendor",
                "task": "reply",
                "today": self.today,
                "org": {"id": vendor_org, "name": org.get("name")},
                "counterpart": {"id": emitter.get("id"), "name": emitter.get("name")},
                "thread": {
                    "id": thread.id,
                    "streamIds": list(thread.stream_ids),
                    "turns": thread.turns,
                    "maxTurns": thread.max_turns,
                    "allowedTypes": list(allowed),
                },
                "guardrails": {
                    "days": list(guard.get("days") or []),
                    "autoDecline": list(guard.get("autoDecline") or []),
                },
                "streams": [
                    {
                        "id": s["id"],
                        "name": s.get("name"),
                        "wasteItem": s.get("wasteItem"),
                        "material": s.get("material"),
                        "form": s.get("form"),
                        "contaminants": [
                            c.get("name") or c.get("label") for c in s.get("contaminants") or []
                        ],
                        "facts": facts[s["id"]],
                        "unknownFields": list(s.get("unknownFields") or []),
                        "autoDecline": rails.auto_decline_reason(guard, s),
                    }
                    for s in streams
                ],
                "criteria": {k: v for k, v in criteria.items() if k != "fleet"},
                "incoming": {
                    "seq": message.get("seq"),
                    "type": message.get("type"),
                    "streamIds": list(message.get("streamIds") or []),
                    "text": message.get("text"),
                    "asks": list((message.get("meta") or {}).get("asks") or []),
                    "claims": from_messages.get(message.get("seq"), {}),
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
                "state": {"offered": offered, "asked": asked},
                "tools": list(TOOLS),
                "toolResults": tools.results,
                "pool": pool.describe(),
                "rejections": rejections,
            }

        def make(output: dict[str, Any]) -> Message:
            reply = build_message(
                output,
                pool,
                thread_id=thread.id,
                sender=vendor_org,
                recipient=thread.emitter_org,
                allowed_types=allowed,
                stream_ids=thread.stream_ids,
            )
            for key in ("asks", "excluded"):
                items = [dict(x) for x in output.get(key) or [] if isinstance(x, dict)]
                if items:
                    reply.meta[key] = items
            if isinstance(output.get("reason"), str) and output["reason"].strip():
                reply.meta["reason"] = output["reason"].strip()
            return reply

        def check(reply: Message, output: dict[str, Any]) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            try:
                thread.check(reply)
            except ThreadError as error:
                found.append(rejection("THREAD_RULE", f"대화 규칙에 맞지 않습니다: {error}"))
            names = [org.get("name"), emitter.get("name"), *[x.get("name") for x in streams]]
            result = validate_message(reply, self.registry, today=self.today, names=names)
            found += [r.to_dict() for r in result.rejections]
            terms, problems = self._terms(output, reply, pool)
            found += problems
            if terms is not None:
                reply.meta["terms"] = terms
                found += self._guard(guard, reply, terms, by_id)
            for problem in check_disclosure(reply, self.registry, self.store):
                found.append(
                    rejection(
                        str(problem["code"]),
                        f"{problem['message']} 다른 배출 기업의 이름, 흐름, 물량은 알릴 수 없습니다.",
                    )
                )
            if reply.type == "decline":
                # 허가증 근거를 닫는 이유의 근거로 남긴다
                reply.meta["reasonEvidence"] = [
                    e for p in profile.get("permits") or [] for e in p.get("evidence") or []
                ]
            holder["terms"] = terms
            return found

        outcome: Outcome = run_draft(
            llm=self.llm,
            system=SYSTEM_PROMPT,
            briefing=briefing,
            schema=RESPONSE_SCHEMA,
            pool=pool,
            tools=tools,
            make=make,
            check=check,
            trace=trace,
            actor=ACTOR,
        )
        if outcome.kind == "message" and outcome.message is not None:
            reply = outcome.message
            reply.meta["validation"]["rejections"] = public_rejections(
                reply.meta["validation"]["rejections"]
            )
        else:
            reply = make_handoff(
                thread_id=thread.id,
                sender=vendor_org,
                recipient=thread.emitter_org,
                reason="업체 측 에이전트의 초안이 두 번 거부되어 담당자에게 넘깁니다.",
                stage=int(message.get("stage") or 4),
                rejections=public_rejections(outcome.rejections),
                attempts=outcome.attempts,
                model=model_label(self.llm),
            )
        data = reply.to_dict()
        for event in trace.events:
            if "rejections" in event["detail"]:
                event["detail"]["rejections"] = public_rejections(event["detail"]["rejections"])
        data["meta"]["trace"] = trace.events
        return data
