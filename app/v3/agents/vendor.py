"""업체 측 에이전트. 처리 업체 하나를 대변해 회사 측 에이전트의 메시지에 답한다.

근거 검증기에 더해 두 가지를 통과해야 메시지가 나간다.
- 가드레일(하한·상한 단가, 가능 요일, 월 최소 횟수, 자동 거절 조건): guardrails.enforce
- 다른 배출 기업의 이름·흐름·물량을 드러내지 않는지: policy.check_disclosure
업체는 자기 프로필의 model 설정(foundry | ollama)으로 모델을 고르고, 실제로 쓴 구현 이름이
답 메시지의 meta.model에 남는다.
"""

from __future__ import annotations

import re
from typing import Any

from app.v3 import ids
from app.v3.a2a import guardrails as rails
from app.v3.a2a.message import Message
from app.v3.a2a.policy import check_disclosure
from app.v3.a2a.thread import Thread, ThreadError
from app.v3.agents import questions
from app.v3.agents.draft import (
    PERMIT_EXCLUDED,
    RULES,
    TOOL_HINT,
    AgentTools,
    Outcome,
    Trace,
    build_message,
    make_decline,
    make_handoff,
    make_notice,
    public_rejections,
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
# 글에 적힌 요일("월요일", "화·목요일"의 마지막 글자). 가드레일 밖 요일을 본문으로 약속하지 못하게 한다
_WEEKDAY_WORD = re.compile(r"([월화수목금토일])\s?요일")

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
        (
            "- terms.conditions의 각 항목은 '흐름 이름: 조건' 꼴로 쓴다. 조건이 없는 흐름은 적지 않는다. "
            "수거 요일은 conditions가 아니라 days에 적는다."
        ),
        "- terms의 단가는 숫자로 적지 않고 unitPriceRef에 claim ID로 적는다.",
        "- 허가 품목이 맞지 않으면 근거와 함께 decline으로 답한다.",
        "- 다른 배출 기업의 이름, 흐름, 물량을 알리지 않는다.",
        (
            "- 오염도나 수분이 측정값이 아니면(사진 추정, 담당자 답변) 반입 검수에서 매입 기준 상한 "
            "이내로 확인될 것을 조건으로 건 conditional로 답한다. 상한은 매입 기준의 claim으로 인용한다."
        ),
        (
            "- 혼입물이 섞인 흐름은 '확인 필요'로 미루지 않는다. 혼입물은 분리해 내보내거나 반입 검수에서 "
            "오염도가 매입 기준 상한 이내로 확인될 것을 조건으로 건다. 이것이 업체의 기본 반입 조건이다."
        ),
        "- 이미 낸 조건을 상대가 받아들였으면 같은 조건(terms)을 바꾸지 않는다. 새 조건을 덧붙이지 않는다.",
        "- 본문에서 흐름과 매입 기준은 이름으로 부른다. st_, pc_ 같은 식별자는 본문에 쓰지 않는다.",
        "",
        TOOL_HINT,
        "응답 JSON: type, stage, streamIds, text, claimRefs, toolCalls, terms, asks, excluded, reason.",
    ]
)


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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _price(claim: Any) -> float | None:
    """claim이 kg당 금액이면 원/kg 값, 아니면 None."""
    if not _is_number(claim.value):
        return None
    try:
        return convert(float(claim.value), claim.unit, "원/kg")
    except (UnitError, TypeError, ValueError):
        return None


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
        # 풀의 표시와 관계없이 kg당 금액으로 읽히는 claim은 모두 단가로 본다(다른 이름의 claim으로
        # 가드레일 검사를 비켜 가지 못하게 한다)
        loose = [c.id for c in message.claims if c.id not in declared and _price(c) is not None]
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
        days = [str(d) for d in (raw.get("days") if isinstance(raw.get("days"), list) else [])]
        if any(d not in rails.WEEKDAYS for d in days):
            found.append(
                rejection(
                    "GUARDRAIL_TERMS_REF",
                    f"terms.days에는 요일({', '.join(rails.WEEKDAYS)})만 적을 수 있습니다.",
                )
            )
            days = [d for d in days if d in rails.WEEKDAYS]
        raw_conditions = raw.get("conditions")
        conditions = [
            str(c) for c in (raw_conditions if isinstance(raw_conditions, list) else []) if c
        ]
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

    def _permits(
        self,
        message: Message,
        terms: dict[str, Any] | None,
        tools: AgentTools,
        streams: dict[str, dict],
    ) -> list[dict[str, Any]]:
        """허가 품목이 맞지 않는 흐름에는 조건을 낼 수 없다(모델이 대조를 건너뛰어도 코드가 한다)."""
        if terms is None:
            return []
        bad = []
        for sid in dict.fromkeys(item["streamId"] for item in terms["items"]):
            if sid not in streams:
                continue
            done = tools.run("match_permit", {"streamId": sid}, "code")
            if ((done.get("output") or {}).get("matched")) is not True:
                bad.append(sid)
        if not bad:
            return []
        return [
            rejection(
                "PERMIT_MISMATCH",
                f"허가 품목이 맞지 않는 흐름({', '.join(bad)})에는 조건을 낼 수 없습니다. "
                "그 흐름은 excluded에 이유와 함께 넣고, 받을 흐름이 없으면 decline으로 답하세요.",
                streamIds=bad,
            )
        ]

    def _permit_fallback(
        self,
        thread: Thread,
        profile: dict[str, Any],
        tools: AgentTools,
        streams: dict[str, dict],
        allowed: tuple[str, ...],
        outcome: Outcome,
    ) -> Message | None:
        """모델이 허가 없는 흐름에 조건을 내려다 두 번 거부된 뒤에 코드가 쓰는 답.

        - 이 대화에 허가가 맞는 흐름이 하나도 없으면 근거와 함께 decline으로 닫는다.
        - 일부만 맞지 않으면 대화를 닫지 않는다. 그 흐름만 받지 않는다고 알리고(excluded) 나머지는
          이어 간다. 모델이 무엇을 내든 코드가 그렇게 한다.
        - 이미 그렇게 알린 뒤에도 같은 초안이 거부되면 None(담당자에게 넘긴다).
        """
        vendor_org = thread.vendor_org
        unmatched = [
            sid
            for sid in thread.stream_ids
            if sid in streams
            and (tools.run("match_permit", {"streamId": sid}, "code").get("output") or {}).get(
                "matched"
            )
            is not True
        ]
        if not unmatched:
            return None
        evidence = [e for p in profile.get("permits") or [] for e in p.get("evidence") or []]
        excluded = [{"streamId": sid, "reason": PERMIT_EXCLUDED} for sid in unmatched]
        kept = [sid for sid in thread.stream_ids if sid in streams and sid not in unmatched]
        if not kept:
            return make_decline(
                thread_id=thread.id,
                sender=vendor_org,
                recipient=thread.emitter_org,
                reason="허가 품목이 맞지 않는 흐름이 있어 이 대화를 닫습니다.",
                stage=4,
                stream_ids=tuple(unmatched),
                evidence=evidence,
                excluded=excluded,
                rejections=outcome.rejections,
                attempts=outcome.attempts,
            )
        told = {
            x.get("streamId")
            for m in thread.messages
            if m.sender == vendor_org
            for x in m.meta.get("excluded") or []
            if isinstance(x, dict) and x.get("reason") == PERMIT_EXCLUDED
        }
        if set(unmatched) <= told or "answer" not in allowed:
            return None
        names = ", ".join(str(streams[sid].get("name") or sid) for sid in unmatched)
        return make_notice(
            kind="answer",
            thread_id=thread.id,
            sender=vendor_org,
            recipient=thread.emitter_org,
            text=f"허가 품목이 맞지 않는 흐름은 받지 않습니다: {names}. "
            "나머지 흐름은 이 대화에서 이어서 답하겠습니다.",
            stage=4,
            stream_ids=tuple(unmatched),
            evidence=evidence,
            excluded=excluded,
            rejections=outcome.rejections,
            attempts=outcome.attempts,
        )

    def _guard_any(self, guard: dict[str, Any], message: Message) -> list[dict[str, Any]]:
        """메시지 종류와 관계없이 거는 가드레일.

        조건(terms)이 없는 answer·question이라도 가드레일 밖 단가나 수거 횟수를 인용하거나,
        수거할 수 없는 요일을 글로 약속하면 내보내지 않는다.
        """
        found: dict[str, dict[str, Any]] = {}

        def add(violations: list[rails.Violation]) -> None:
            for violation in violations:
                found[violation.code] = rejection(
                    f"GUARDRAIL_{violation.code}",
                    f"{violation.message} 가드레일 안의 조건으로 다시 쓰거나 다른 종류로 답하세요.",
                )

        for claim in message.claims:
            price = _price(claim)
            if price is not None:
                add(rails.check_terms(guard, {"unitPriceKrwPerKg": price}))
            elif claim.unit == "회/월" and _is_number(claim.value):
                add(rails.check_terms(guard, {"pickupsPerMonth": float(claim.value)}))
        texts = [message.text, *[text for _, text in message.free_texts()]]
        named = [d for text in texts for d in _WEEKDAY_WORD.findall(text or "")]
        if named:
            add(rails.check_terms(guard, {"days": list(dict.fromkeys(named))}))
            declared = (message.meta.get("terms") or {}).get("days") or []
            if declared and any(d not in declared for d in named):
                found["DAY_NOT_DECLARED"] = rejection(
                    "GUARDRAIL_DAY_NOT_DECLARED",
                    "글에 적은 요일이 terms.days에 없습니다. 조건의 요일은 terms.days와 같게 쓰세요.",
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
        # 회사 측이 counter로 대화를 다시 열었으면(합의 뒤 조건 변경) 그 앞의 제안은 지난 조건이다
        reopened = max(
            (m.seq for m in thread.messages if m.sender != vendor_org and m.type == "counter"),
            default=0,
        )
        offered = sorted(
            {
                item.get("streamId")
                for m in thread.messages
                if m.sender == vendor_org and m.seq > reopened
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
            asks = questions.clean_asks(output.get("asks"))
            if asks:
                reply.meta["asks"] = asks
            # 받지 않는 흐름은 이 대화의 흐름 ID와 이유 문장만 남긴다(다른 항목은 싣지 않는다)
            excluded = [
                {"streamId": x["streamId"], "reason": str(x.get("reason") or "").strip()}
                for x in (
                    output.get("excluded") if isinstance(output.get("excluded"), list) else []
                )
                if isinstance(x, dict) and x.get("streamId") in thread.stream_ids
            ]
            if excluded:
                reply.meta["excluded"] = excluded
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
            # 조건(terms)을 먼저 붙인다. 조건 문장과 요일도 본문과 같은 근거 검증을 받는다
            terms, problems = self._terms(output, reply, pool)
            if terms is not None:
                reply.meta["terms"] = terms
            result = validate_message(reply, self.registry, today=self.today, names=names)
            found += [r.to_dict() for r in result.rejections]
            found += problems
            if terms is not None:
                found += self._guard(guard, reply, terms, by_id)
            known = {item["code"] for item in found}
            if "GUARDRAIL_DAY_NOT_ALLOWED" in known:
                known.add("GUARDRAIL_DAY_NOT_DECLARED")
            found += [g for g in self._guard_any(guard, reply) if g["code"] not in known]
            # 숫자 없이 조건을 뜻하는 낱말("무상", "주말", "전량" 등)에는 terms 항목이나 claim이 있어야 한다
            found += [
                rejection(v.code, v.message, **v.detail)
                for v in rails.check_term_words(reply, terms or {}, names)
            ]
            found += self._permits(reply, terms, tools, by_id)
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
            reply = None
            if any(r.get("code") == "PERMIT_MISMATCH" for r in outcome.rejections):
                # 허가 품목이 맞지 않는 흐름에 조건을 내려다 두 번 거부됐다
                reply = self._permit_fallback(thread, profile, tools, by_id, allowed, outcome)
        if reply is None:
            reply = make_handoff(
                thread_id=thread.id,
                sender=vendor_org,
                recipient=thread.emitter_org,
                reason="업체 측 에이전트의 초안이 두 번 거부되어 담당자에게 넘깁니다.",
                stage=int(message.get("stage") or 4),
                rejections=public_rejections(outcome.rejections),
                attempts=outcome.attempts,
                # 이 문장은 모델이 아니라 코드가 썼다. 모델 이름을 붙이지 않는다(시도는 trace에 남는다)
            )
        data = reply.to_dict()
        for event in trace.events:
            if "rejections" in event["detail"]:
                event["detail"]["rejections"] = public_rejections(event["detail"]["rejections"])
        data["meta"]["trace"] = trace.events
        return data
