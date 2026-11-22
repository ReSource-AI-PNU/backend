"""업체 측 에이전트. 처리 업체 하나를 대변해 회사 측 에이전트의 메시지에 답한다.

근거 검증기에 더해 두 가지를 통과해야 메시지가 나간다.
- 가드레일(하한·상한 단가, 가능 요일, 월 최소 횟수, 자동 거절 조건): guardrails.enforce
- 다른 배출 기업의 이름·흐름·물량을 드러내지 않는지: policy.check_disclosure
업체는 자기 프로필의 model 설정(foundry | ollama)으로 모델을 고르고, 실제로 쓴 구현 이름이
답 메시지의 meta.model에 남는다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.v3 import ids
from app.v3 import names as naming
from app.v3.a2a import guardrails as rails
from app.v3.a2a.message import Claim, Message
from app.v3.a2a.policy import check_disclosure
from app.v3.a2a.thread import Thread, ThreadError
from app.v3.agents import past, questions
from app.v3.agents.draft import (
    PERMIT_EXCLUDED,
    RULES,
    TOOL_HINT,
    AgentTools,
    Outcome,
    Trace,
    build_message,
    enum_string,
    make_decline,
    make_handoff,
    make_notice,
    public_rejections,
    rejection,
    response_schema,
    run_draft,
    turn_schema,
)
from app.v3.agents.llm import LLM
from app.v3.agents.mandate import prompt_mandate
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.units import UnitError, convert
from app.v3.evidence.validator import validate_message
from app.v3.fleet import INFO_KEYS
from app.v3.judge import negotiate
from app.v3.judge import pickup as pickup_terms
from app.v3.judge.common import Recorder, haversine_km, vendor_distance, without_emitter_only
from app.v3.untrusted import counterparty_stream, org_label, stream_label

logger = logging.getLogger(__name__)

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

# 업체 측 AI가 근거 검사를 두 번 통과하지 못했을 때 담당자에게 넘기는 문장(두 조직이 함께 본다).
# 거부 코드는 문장에 넣지 않고 meta.validation.rejections와 실행 기록의 detail에만 둔다(DESIGN.md 제품 문구 원칙)
VENDOR_HANDOFF = "업체 측 AI가 근거를 갖춘 답을 만들지 못해 담당자 확인이 필요합니다."

EXAMPLES = (
    # 규칙 정책이 실제로 낸 메시지를 줄인 것(형식 예시. 이름과 ID는 입력의 것으로 바꿔 쓴다)
    {
        "type": "conditional",
        "stage": 5,
        "streamIds": ["st_defect"],
        "text": (
            "다음 폐기물은 조건부로 받을 수 있습니다.\n"
            "- 불량 사출품: 아래 조건이 확인되면 받습니다. 매입 단가는 {{route_defect_unitPrice}}입니다.\n"
            "  · 반입 검수에서 오염도가 매입 기준 상한 {{pc_x_maxContaminationPct}} 이하로 확인될 것\n"
            "수거는 화·목요일 가운데 정하고 수거 횟수는 {{load_defect_pickupsPerMonth}}입니다."
        ),
        "terms": {
            "items": [{"streamId": "st_defect", "unitPriceRef": "route_defect_unitPrice"}],
            "days": ["화", "목"],
            "pickupsPerMonthRef": "load_defect_pickupsPerMonth",
            "conditions": [
                "불량 사출품: 반입 검수에서 오염도가 매입 기준 상한 {{pc_x_maxContaminationPct}} 이하로 확인될 것"
            ],
        },
        "toolCalls": [],
    },
    {
        "type": "answer",
        "stage": 4,
        "streamIds": ["st_sprue", "st_defect"],
        "text": "",
        "toolCalls": [
            {"name": "match_permit", "arguments": {"streamId": "st_sprue"}},
            {"name": "judge_route", "arguments": {"streamId": "st_sprue"}},
            {"name": "match_permit", "arguments": {"streamId": "st_defect"}},
            {"name": "judge_route", "arguments": {"streamId": "st_defect"}},
        ],
    },
)

SYSTEM_PROMPT = "\n".join(
    [
        *prompt_mandate("vendor"),
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
        "- terms의 단가는 숫자로 적지 않고 unitPriceRef에 claim ID로 적는다. 그 단가는 본문에도 {{id}}로 쓴다.",
        "- 허가 품목이 맞지 않으면 근거와 함께 decline으로 답한다.",
        "- 다른 배출 기업의 이름, 폐기물, 물량을 알리지 않는다.",
        (
            "- 오염도나 수분이 측정값이 아니면(사진 추정, 담당자 답변) 반입 검수에서 매입 기준 상한 "
            "이내로 확인될 것을 조건으로 건 conditional로 답한다. 상한은 매입 기준의 claim으로 인용한다."
        ),
        (
            "- 혼입물이 섞인 폐기물은 '확인 필요'로 미루지 않는다. 혼입물은 분리해 내보내거나 반입 검수에서 "
            "오염도가 매입 기준 상한 이내로 확인될 것을 조건으로 건다. 이것이 업체의 기본 반입 조건이다."
        ),
        "- offer는 조건 없이 그대로 받을 수 있을 때만 쓴다. 조건이 하나라도 붙으면 conditional이다.",
        (
            "- pastInspections는 이 배출 기업과의 지난 반입 검수 요약이다(같은 두 회사 사이의 기록만). status가 below인 "
            "폐기물은 '반입 검수에서 매입 기준 등급을 다시 확인할 것'을 조건으로 건 conditional로 답한다. 지난 검수의 "
            "날짜·등급·단가 같은 숫자는 본문에 쓰지 않는다."
        ),
        "- 이미 낸 조건을 상대가 받아들였으면 같은 조건(terms)을 바꾸지 않는다. 새 조건을 덧붙이지 않는다.",
        "- 본문에서 폐기물과 매입 기준은 이름으로 부른다. st_, pc_ 같은 식별자는 본문에 쓰지 않는다.",
        "",
        TOOL_HINT,
        "응답 JSON: type, stage, streamIds, text, toolCalls (필요하면 terms, asks, excluded, reason).",
        "stage는 질문·답이면 4, 조건을 내면 5다.",
        "",
        "형식 예시(값은 모두 {{id}}로 쓰고, 숫자는 한 글자도 없다):",
        *[json.dumps(example, ensure_ascii=False) for example in EXAMPLES],
    ]
)


def public_profile(profile: dict[str, Any], *, fleet_cost: bool = True) -> dict[str, Any]:
    """판단 도구에 넘기는 업체 프로필. 하한·상한 단가와 자동 거절 조건, 모델 설정은 뺀다.

    노선·차량의 운영 정보(차량 수, 노선)는 계산에 쓰지 않으므로 싣지 않는다(fleet.INFO_KEYS).
    """
    guard = profile.get("guardrails") or {}
    fleet = {k: v for k, v in (profile.get("fleet") or {}).items() if k not in INFO_KEYS}
    if not fleet_cost:
        fleet.pop("costPerKm", None)
        fleet.pop("evidence", None)
    out = {
        "orgId": profile.get("orgId"),
        "permits": list(profile.get("permits") or []),
        "criteria": list(profile.get("criteria") or []),
        "fleet": fleet,
        "guardrails": {
            k: guard[k] for k in ("days", "minPickupsPerMonth") if guard.get(k) is not None
        },
    }
    # 운반 조건(단가에 포함인지, 1회 운반비), 처리 방식, 월 처리 용량은 업체가 밝히는 조건이다.
    # 없는 프로필은 예전처럼 키 자체를 싣지 않는다(계산 근거 ID가 바뀌지 않게)
    for key in ("transport", "process", "monthlyCapacityKg", "collectionPolicy"):
        if profile.get(key) is not None:
            out[key] = profile[key]
    if any(c.get("priceLow") is not None for c in profile.get("criteria") or []):
        from app.v3.survey import profile_basis

        out["_rangeBasis"] = profile_basis(profile)
    return out


def short(stream_id: str) -> str:
    return stream_id.removeprefix("st_")


def _tried(results: list[dict[str, Any]], tool: str, **arguments: Any) -> bool:
    """불렀던 호출인가(실패한 호출 포함. 같은 입력이면 다시 불러도 같은 이유로 실패한다)."""
    return any(
        item.get("tool") == tool
        and all((item.get("arguments") or {}).get(k) == v for k, v in arguments.items())
        for item in results
    )


def _result(results: list[dict[str, Any]], tool: str, **arguments: Any) -> dict[str, Any] | None:
    for item in results:
        given = item.get("arguments") or {}
        if (
            item.get("tool") == tool
            and item.get("status") != "error"
            and all(given.get(k) == v for k, v in arguments.items())
        ):
            return item
    return None


def condition_lines(
    route: dict[str, Any], criterion: dict[str, str], stream: dict[str, Any]
) -> list[str]:
    """judge_route의 조건부 검사를 업체가 내거는 조건 문장으로 바꾼다(값은 {{id}}).

    규칙 정책(policy_rule._Vendor._conditions)과 같은 문장이다. 모델에게 고를 수 있는 문장으로 준다.
    """
    out = []
    for check in route.get("checks") or []:
        if check.get("status") != "conditional":
            continue
        key = {"moisture_basis": "moisturePct", "contamination_basis": "contaminationPct"}.get(
            check.get("key"), check.get("key")
        )
        if key in ("moisturePct", "contaminationPct"):
            subject = "수분이" if key == "moisturePct" else "오염도가"
            limit = criterion.get(
                "maxMoisturePct" if key == "moisturePct" else "maxContaminationPct"
            )
            bound = f"매입 기준 상한 {{{{{limit}}}}}" if limit else "매입 기준 상한"
            out.append(f"반입 검수에서 {subject} {bound} 이하로 확인될 것")
        elif key == "mixture":
            mixed = ", ".join(
                str(c.get("name") or c.get("label"))
                for c in stream.get("contaminants") or []
                if c.get("name") or c.get("label")
            )
            out.append(f"혼입물({mixed or '다른 재질'})은 분리해 내보내거나 검수 기준 안일 것")
        elif key == "monthly_kg_basis":
            out.append("월 발생량이 추정값이므로 실제 계량한 물량으로 정산할 것")
        elif check.get("text"):
            out.append(str(check["text"]))
    return list(dict.fromkeys(out))


def guide(
    *,
    types: tuple[str, ...],
    stream_ids: tuple[str, ...],
    streams: dict[str, dict[str, Any]],
    auto_declined: dict[str, str],
    results: list[dict[str, Any]],
    criteria: dict[str, dict[str, str]],
    offered: list[str],
    asked: list[dict[str, Any]],
    days: list[str],
    roles: dict[str, str | None] | None = None,
    facts: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """이번 차례에 할 일(코드가 도구 결과와 대화 상태에서 쓴다). 판단은 도구 결과 그대로다.

    roles는 claim ID → role(names.py)이다. 단가가 처리비(treatmentFee, 배출 기업이 내는 돈)인지
    매입 단가인지에 따라 본문의 문장이 다르다(CONTRACT.md "처리비 문장").
    facts는 흐름 ID → {항목: claim ID}(pool.add_stream)이다. 담당자가 답한 값과 추정값을 조건 차례에
    짚어 준다(그 값을 숫자로 옮겨 적거나 조건 문장에만 {{id}}를 써서 근거 검사에 걸리지 않게).
    """
    roles = roles or {}
    lines = [f"이번 차례에 쓸 수 있는 type: {', '.join(types)}."]

    def name(sid: str) -> str:
        return str((streams.get(sid) or {}).get("name") or sid)

    live = [sid for sid in stream_ids if sid in streams and sid not in auto_declined]
    missing = [
        sid
        for sid in live
        if _result(results, "match_permit", streamId=sid) is None
        or _result(results, "judge_route", streamId=sid) is None
    ]
    if missing:
        lines.append(
            "먼저 판단 도구를 부른다. text는 비우고 toolCalls에 흐름마다 match_permit과 judge_route를 "
            "한 번에 넣는다: " + ", ".join(missing) + "."
        )
        return lines
    acceptable: list[str] = []
    refused = [f"{name(sid)}({reason})" for sid, reason in auto_declined.items()]
    for sid in live:
        permit = (_result(results, "match_permit", streamId=sid) or {}).get("output") or {}
        route = (_result(results, "judge_route", streamId=sid) or {}).get("output") or {}
        if permit.get("matched") is not True:
            refused.append(f"{name(sid)}({permit.get('reason') or '허가 품목이 맞지 않습니다.'})")
        elif route.get("acceptable") is not True:
            refused.append(f"{name(sid)}({route.get('reason') or '매입 기준에 맞지 않습니다.'})")
        else:
            acceptable.append(sid)
    if not acceptable:
        lines.append(
            "받을 수 있는 흐름이 없다. decline으로 답하고 excluded에 흐름마다 이유를 적는다: "
            + ", ".join(refused)
            + "."
        )
        return lines
    # 앞 차례에 입력을 만들 수 없어 실패한 도구(배출 기업 위치가 없을 때의 노선 비용 등)는 부른 것으로
    # 본다(draft.AgentTools가 기억한다). 아직 부르지 않은 도구만 부르게 한다
    pending = [
        tool
        for tool in ("check_loading", "route_extra_cost")
        if not _tried(results, tool, streamIds=acceptable)
    ]
    if pending:
        lines.append(
            "받을 수 있는 흐름을 모아 "
            + (
                "check_loading과 route_extra_cost를 한 번에"
                if len(pending) == 2
                else pending[0] + "을"
            )
            + f' 부른다(text는 비운다). arguments는 {{"streamIds": {json.dumps(acceptable)}}}이다.'
        )
        return lines
    told = {(a.get("streamId"), a.get("field")) for a in asked}
    need = []
    for sid in acceptable:
        unknown = (streams.get(sid) or {}).get("unknownFields") or []
        route = _result(results, "judge_route", streamId=sid) or {}
        for check in route.get("checks") or []:
            field = check.get("key")
            if (
                field in ("contaminationPct", "moisturePct")
                and check.get("status") == "conditional"
                and field not in unknown
                and (sid, field) not in told
            ):
                need.append(f"{name(sid)}의 {questions.ASK_LABELS[field]}({field})")
    if need:
        lines.append(
            "매입 기준과 대조할 값이 없다. question으로 묻고 asks에 {streamId, field}를 적는다. "
            "본문에도 묻는 값을 '- 폐기물 이름: 항목'으로 적는다: " + ", ".join(need) + "."
        )
        return lines
    todo = [sid for sid in acceptable if sid not in offered]
    if not todo:
        lines.append(
            "받을 흐름에는 모두 조건을 냈다(state.offered). 조건을 바꾸거나 덧붙이지 않는다. "
            "상대의 말에 answer로 짧게 답한다(새 값이 필요 없으면 {{id}} 없이)."
        )
        return lines
    loading = _result(results, "check_loading", streamIds=acceptable) or {}
    load_claims = loading.get("claims") or {}
    load_checks = {c.get("key"): c.get("status") for c in loading.get("checks") or []}
    lines.append(
        "조건을 낼 흐름: "
        + ", ".join(name(sid) for sid in todo)
        + ". 이 흐름을 모두 한 메시지에 담고, streamIds와 terms.items에 흐름마다 넣는다. "
        "상대에게 되묻지 않는다(확인할 것은 조건으로 건다)."
    )
    conditional = False
    for sid in todo:
        route = _result(results, "judge_route", streamId=sid) or {}
        price = (route.get("claims") or {}).get("unitPrice")
        criterion = criteria.get(str((route.get("output") or {}).get("criterionId"))) or {}
        found = condition_lines(route, criterion, streams.get(sid) or {})
        conditional = conditional or bool(found) or route.get("status") != "calculated"
        if not price:
            lines.append(f"- {name(sid)}: 단가 claim 없음")
        elif roles.get(price) == "treatmentFee":
            lines.append(
                f"- {name(sid)}: 배출 기업이 내는 처리비다. 본문에 '처리비는 {{{{{price}}}}}입니다'로 "
                "쓴다('매입 단가'라고 쓰지 않는다)"
            )
        else:
            lines.append(f"- {name(sid)}: 본문에 '매입 단가는 {{{{{price}}}}}입니다'로 쓴다")
        # 조건은 한 줄에 하나씩 준다(본문에는 '  · ' 목록으로, terms.conditions에는 '이름: 조건'으로)
        lines += [f"  · {name(sid)}의 조건: {condition}" for condition in found]
        soft = _soft_values(streams.get(sid) or {}, (facts or {}).get(sid) or {})
        if soft:
            lines.append(
                f"  · {name(sid)}의 {', '.join(soft)}. 측정값이 아니므로 위 조건으로 반입 검수에서 "
                "확인한다. 이 값을 본문에 적으려면 그 {{id}}로만 쓰고, terms.conditions에는 위 조건 "
                "문장만 쓴다"
            )
    tail = []
    if load_claims.get("pickupsPerMonth"):
        tail.append(
            f"수거 횟수를 밝히려면 본문에 '수거 횟수는 {{{{{load_claims['pickupsPerMonth']}}}}}입니다'를 "
            "쓰고 terms.pickupsPerMonthRef에도 그 id를 적는다(본문에 쓰지 않으면 적지 않는다)"
        )
    tail.append(f"수거 요일은 {'·'.join(days) or '가드레일의 요일'} 가운데에서 terms.days에 적는다")
    if load_checks.get("min_volume") not in (None, "met"):
        conditional = True
        tail.append(
            "합친 물량이 최소 물량에 못 미친다: '기존 노선에 묶어 수거할 것'을 조건으로 건다"
        )
    if load_checks.get("loading") not in (None, "met"):
        conditional = True
        tail.append(
            "한 번 수거량이 차량 용량을 넘는다: '수거 횟수를 늘려 나눠 실을 것'을 조건으로 건다"
        )
    if load_checks.get("loading_volume") not in (None, "met"):
        conditional = True
        tail.append(
            "부피 적재를 확인하지 못했다: '겉보기 밀도를 확인한 뒤 정할 것'을 조건으로 건다"
        )
    lines.append(". ".join(tail) + ".")
    lines.append(
        "terms.conditions에는 위 조건을 '폐기물 이름: 조건' 꼴로 그대로 옮긴다. 거기 쓴 {{id}}는 본문에도 "
        "같은 글자로 쓰고, 위에 없는 조건이나 숫자를 새로 만들지 않는다."
    )
    lines.append(
        "조건이 하나라도 있으니 type은 conditional이다."
        if conditional
        else "붙는 조건이 없으니 type은 offer다."
    )
    if refused and not offered:
        lines.append("받지 않는 흐름은 excluded에 이유와 함께 적는다: " + ", ".join(refused) + ".")
    return lines


def _soft_values(stream: dict[str, Any], facts: dict[str, str]) -> list[str]:
    """측정값이 아닌 흐름 값(담당자 답, 추정값)을 '오염도({{id}})는 담당자가 답한 값' 꼴로."""
    out = []
    for field, label in (("contaminationPct", "오염도"), ("moisturePct", "수분")):
        claim_id = facts.get(field)
        if claim_id and "answered" in claim_id:
            out.append(f"{label}({{{{{claim_id}}}}})는 배출 기업 담당자가 답한 값")
    if facts.get("monthlyKg") and stream.get("monthlyKgBasis") == "estimated":
        out.append(f"월 발생량({{{{{facts['monthlyKg']}}}}})은 추정값")
    return out


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


def _haul_claims(pool: ClaimPool, haul: dict[str, Any], vendor_org: str) -> dict[str, str | None]:
    """운반비 견적의 금액과 수거 횟수를 claim으로 풀에 넣는다(값과 같은 근거가 있을 때만). {"fee", "pickups"}."""
    fee = pickups = None
    if haul.get("basis") == "separate" and haul.get("amount") is not None:
        fee = pool.add_fact(
            "transport_fee",
            haul["amount"],
            pickup_terms.UNIT_OF[haul["unit"]],
            haul.get("feeEvidence") or haul.get("evidence"),
            label="운반비 견적",
            source="profile",
            vendorOrgId=vendor_org,
            field="transportFee",
        )
    if haul.get("pickups") is not None:
        pickups = pool.add_fact(
            "transport_pickups",
            haul["pickups"],
            "회/월",
            haul.get("pickupsEvidence") or haul.get("evidence"),
            label="운반 수거 횟수",
            source="profile",
            vendorOrgId=vendor_org,
            field="transportPickups",
        )
    return {"fee": fee, "pickups": pickups}


def _attach_past(
    reply: Message, terms: dict[str, Any], history: Any, by_id: dict[str, dict[str, Any]]
) -> None:
    """이 배출 기업과의 지난 검수가 합의보다 낮게 나온 적이 많은 폐기물을 조건으로 받으면, 검수 등급 확인을 조건과 본문 한
    줄로 붙인다(모델이 빠뜨려도 같은 조건이 나간다). 조건이 붙으므로 offer는 conditional이 된다."""
    offered = [i.get("streamId") for i in terms.get("items") or [] if isinstance(i, dict)]
    below = [sid for sid in past.below_ids(history, offered) if sid in by_id]
    if not below:
        return
    conditions = list(terms.get("conditions") or [])
    for sid in below:
        name = str(by_id[sid].get("name") or "폐기물")
        if not any(past.MARK in c and c.startswith(f"{name}:") for c in conditions):
            conditions.append(past.vendor_condition(name))
            reply.text = f"{reply.text.rstrip()}\n{past.vendor_line(name)}"
    terms["conditions"] = conditions
    if reply.type == "offer":
        reply.type = "conditional"


def _attach_haul(
    reply: Message,
    output: dict[str, Any],
    haul: dict[str, Any],
    refs: dict[str, str | None],
    pool: ClaimPool,
) -> None:
    """조건을 내는 답에 수거·운반 견적을 붙인다. 모델이 쓰지 않아도 코드가 같은 꼴로 붙인다.

    회당 운반비인데 견적에 월 횟수가 없으면, 노선 수거에서는 이 답에서 업체가 밝힌 수거 횟수(terms.pickupsPerMonthRef로
    본문에 쓴 claim)를 쓴다. 노선 밖 방문 견적은 견적에 적힌 횟수만 쓴다. 횟수가 없으면 횟수 없이 보낸다(회사 측은
    월 운반비를 정하지 않고 "월 수거 횟수 확인 필요"로 둔다).
    숫자는 claim으로만 쓴다(본문에 {{id}}).
    """
    if "수거·운반:" in (reply.text or ""):
        return
    fee_ref, pickups_ref = refs.get("fee"), refs.get("pickups")
    # 노선 밖 방문 견적(visit)은 그 견적에 적힌 횟수만 쓴다. 업체가 횟수를 비워 두었으면 정기 수거 횟수로 채우지 않는다
    if haul.get("unit") == "per_pickup" and pickups_ref is None and haul.get("status") != "visit":
        terms = output.get("terms") if isinstance(output.get("terms"), dict) else {}
        cited = terms.get("pickupsPerMonthRef")
        said = {c.id for c in reply.claims}
        if isinstance(cited, str) and cited in said and f"{{{{{cited}}}}}" in (reply.text or ""):
            pickups_ref = cited
    line = pickup_terms.answer_line(haul, fee_ref, pickups_ref)
    used = [r for r in (fee_ref, pickups_ref) if r and f"{{{{{r}}}}}" in line]
    have = {c.id for c in reply.claims}
    reply.claims = (*reply.claims, *pool.resolve([r for r in used if r not in have]))
    reply.text = f"{reply.text.rstrip()}\n{line}" if (reply.text or "").strip() else line
    reply.meta["terms"]["pickup"] = {
        "status": haul.get("status"),
        "basis": haul.get("basis"),
        "paidBy": haul.get("paidBy"),
        "unit": haul.get("unit"),
        "feeRef": fee_ref if fee_ref in used else None,
        "pickupsRef": pickups_ref if pickups_ref in used else None,
        "days": list(haul.get("days") or []),
        # 노선 수거의 운반비 조건(노선 밖이라 이 공장에 그대로 쓰지 않았을 때 참고로 보인다)
        "routeBasis": haul.get("routeBasis"),
        # 무엇으로 정했는가(route, area, distance, offRoute)와 수거 가능 지역(밝혔을 때)
        "reason": haul.get("reason"),
        "area": haul.get("area"),
        "areas": list(haul.get("areas") or []),
    }


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

    def _pickup_km(
        self, vendor_org: str, fleet: dict[str, Any], emitter: dict[str, Any]
    ) -> float | None:
        """차고지 → 공장 거리(최대 수거 거리를 밝혔을 때만). 회사 측 최적 배정과 같은 계산이다."""
        if fleet.get("maxPickupKm") is None:
            return None
        rec = Recorder("pickup_distance", self.registry, vendor_org, self.today, {})
        found = vendor_distance(
            rec, {"fleet": fleet}, emitter.get("location"), self.distance_provider
        )
        return found["km"] if found["known"] else None

    # ── 도구 입력: ID → 레코드 ─────────────────────────────────────────
    def _other_stops(self, vendor_org: str, emitter_org: str) -> list[dict[str, Any]]:
        """이 업체가 이미 맡은 다른 배출 기업의 수거 지점. 노선 계산에만 쓰고 밖으로 내지 않는다."""
        if self.store.get_org(emitter_org).get("simulation"):
            return []
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
        from app.v3.survey import stream_hash

        for sid, value in streams.items():
            raw = self.store.get_stream(sid)
            if raw.get("orgId") == emitter.get("id"):
                deal = (raw.get("rangeDeals") or {}).get(vendor_id)
                if deal:
                    value["rangeDeals"] = {vendor_id: deal}
                    value["_rangeSourceHash"] = stream_hash(raw)
        tool_profile = public_profile(profile)
        origin = emitter.get("location")
        pickups = (profile.get("guardrails") or {}).get("minPickupsPerMonth")

        def pick(arguments: dict[str, Any]) -> list[dict[str, Any]]:
            wanted = arguments.get("streamIds") or []
            chosen = [streams[s] for s in wanted if s in streams]
            if not chosen or len(chosen) != len(wanted):
                raise ValueError("streamIds에 이 대화의 폐기물 ID를 넣으세요.")
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
                raise ValueError("이 대화에 없는 폐기물입니다. streamId를 확인하세요.")
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
                    "조건을 내는 메시지에는 terms.items가 있어야 합니다. 폐기물마다 streamId와 "
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
            if sid not in message.stream_ids:
                found.append(
                    rejection(
                        "GUARDRAIL_TERMS_REF",
                        f"terms.items의 streamId({sid})는 이 메시지의 폐기물"
                        f"({', '.join(message.stream_ids)}) 가운데 하나여야 합니다.",
                    )
                )
                continue
            if claim is None:
                found.append(
                    rejection(
                        "GUARDRAIL_TERMS_REF",
                        f"terms.items의 unitPriceRef({ref})를 본문에 쓰지 않았습니다. 본문의 그 "
                        f"폐기물 줄에 {{{{{ref}}}}}로 단가를 적거나, 본문에 쓴 단가 claim ID로 "
                        "바꾸세요.",
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
            if claim is not None and _is_number(claim.value):
                pickups = float(claim.value)
            elif claim is None and str(ref) in pool:
                # 풀에 있는 수거 횟수를 가리켰지만 본문에 쓰지 않았다: 상대에게 밝히지 않은 횟수는
                # 조건이 아니므로 참조만 뺀다(본문에 쓴 횟수 claim은 아래 가드레일 검사가 따로 본다)
                pickups = None
            else:
                found.append(
                    rejection(
                        "GUARDRAIL_TERMS_REF",
                        f"pickupsPerMonthRef({ref})는 본문에 쓴 수거 횟수 claim ID여야 합니다. "
                        "본문에 그 {{id}}로 수거 횟수를 적거나 pickupsPerMonthRef를 빼세요.",
                    )
                )
        days = [str(d) for d in (raw.get("days") if isinstance(raw.get("days"), list) else [])]
        if any(d not in rails.WEEKDAYS for d in days):
            wrong = [d for d in days if d not in rails.WEEKDAYS]
            found.append(
                rejection(
                    "GUARDRAIL_TERMS_REF",
                    f"terms.days에는 요일 한 글자({', '.join(rails.WEEKDAYS)})만 적을 수 있습니다"
                    f"(받지 못한 값: {', '.join(wrong)}).",
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
                    f"{reason} 이 폐기물에는 조건을 낼 수 없습니다.",
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
                f"허가 품목이 맞지 않는 폐기물({', '.join(bad)})에는 조건을 낼 수 없습니다. "
                "그 폐기물은 excluded에 이유와 함께 넣고, 받을 폐기물이 없으면 decline으로 답하세요.",
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
                reason="허가 품목이 맞지 않는 폐기물이 있어 이 대화를 닫습니다.",
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
            text=f"허가 품목이 맞지 않는 폐기물은 받지 않습니다: {names}. "
            "나머지 폐기물은 이 대화에서 이어서 답하겠습니다.",
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
    def _price_reply(
        self,
        vendor_org: str,
        profile: dict[str, Any],
        thread: Thread,
        message: dict[str, Any],
        streams: dict[str, dict],
        names: list[str],
    ) -> dict[str, Any] | None:
        """⑥ 회사 측의 단가 조정 요청(meta.priceAsks)에 업체가 정한 범위 안에서 코드로 답한다(모델을 거치지 않는다).

        앞서 낸 조건 메시지를 그대로 두고 요청받은 폐기물의 단가만 협상 단가로 바꿔 같은 종류로 다시 낸다.
        답할 수 없으면(앞선 조건이 없거나 근거 검사에 걸리면) None을 돌려 평소처럼 답하게 한다.
        """
        asks = (message.get("meta") or {}).get("priceAsks") or []
        # 폐기물마다 업체가 마지막으로 낸 조건(여러 메시지에 나눠 냈을 수 있다)
        latest: dict[str, tuple[Message, dict[str, Any]]] = {}
        for m in thread.messages:
            if m.sender == vendor_org and m.type in rails.ENFORCED_TYPES and m.meta.get("terms"):
                for item in m.meta["terms"].get("items") or []:
                    latest[str(item.get("streamId"))] = (m, dict(item))
        if not asks or not latest:
            return None
        asked_claims = {c["id"]: c for c in message.get("claims") or [] if isinstance(c, dict)}
        claims: dict[str, Any] = {}
        items: dict[str, dict[str, Any]] = {}
        conditions: list[str] = []
        sources: list[Message] = []
        for sid, (m, item) in latest.items():
            found = {c.id: c for c in m.claims}.get(item["claimId"])
            if found is None or sid not in streams:
                continue
            claims[item["claimId"]] = found
            items[sid] = item
            if m not in sources:
                sources.append(m)
        for m in sources:
            for text in m.meta["terms"].get("conditions") or []:
                if text in conditions:
                    continue
                conditions.append(text)
                own = {c.id: c for c in m.claims}
                for cid in re.findall(r"\{\{([^{}]+)\}\}", text):
                    if cid in own:
                        claims.setdefault(cid, own[cid])
        guard = profile.get("guardrails") or {}
        lines: list[str] = []
        asked_ids: set[str] = set()
        for ask in asks:
            sid, asked = str(ask.get("streamId")), asked_claims.get(str(ask.get("claimId")))
            item = items.get(sid)
            offered = claims.get(item["claimId"]) if item else None
            if asked is None or offered is None:
                continue
            ceiling = guard.get("ceilingPriceKrwPerKg")
            premium = profile.get("simulationCounterPremium")
            first = _price(offered)
            if profile.get("synthetic") is True and _is_number(premium) and first is not None:
                ceiling = first + float(premium)
            result = negotiate.reply_price(
                self.registry,
                org_id=vendor_org,
                today=self.today,
                stream_id=sid,
                asked=asked,
                offered=offered.to_dict(),
                ceiling=ceiling,
                emitter_org=thread.emitter_org,
            )
            found = next((c for c in result["claims"] if c["id"] == "replyPrice"), None)
            if found is None:
                continue
            key = (
                f"nego_{short(sid)}_unitPrice"  # 받는 쪽이 이름(매입 단가·처리비)을 다시 정하는 꼴
            )
            role = "treatmentFee" if found["value"] < 0 else "unitPrice"
            claims[key] = Claim.from_dict(naming.annotate({**found, "id": key}, role, "협상 단가"))
            item.update(claimId=key, unitPriceKrwPerKg=float(found["value"]))
            asked_ids.add(sid)
            label = stream_label(streams[sid])
            word = "처리비" if found["value"] < 0 else "매입 단가"
            output = result["output"]
            if output["accepted"]:
                lines.append(f"{label}의 {word}는 요청하신 대로 {{{{{key}}}}}에 맞추겠습니다.")
            elif output["improved"]:
                lines.append(
                    f"{label}의 {word}는 정해 둔 범위 안에서 {{{{{key}}}}}까지 맞출 수 있습니다."
                )
            else:
                lines.append(
                    f"{label}의 {word}는 처음 드린 {{{{{key}}}}}에서 더 조정하기 어렵습니다."
                )
        if not lines:
            return None
        for sid, item in items.items():
            if sid not in asked_ids:
                lines.append(
                    f"{stream_label(streams[sid])}의 단가는 앞서 드린 {{{{{item['claimId']}}}}} 그대로입니다."
                )
        used = {item["claimId"] for item in items.values()}
        cited = set(re.findall(r"\{\{([^{}]+)\}\}", " ".join(conditions)))
        kept = [c for cid, c in claims.items() if cid in used or cid in cited]
        last = sources[-1]
        terms = json.loads(json.dumps(last.meta["terms"]))
        # 수거·운반 견적은 앞선 조건 답에 있다(회사 측이 그 답을 기억한다). 이 답의 claim에 없으니 싣지 않는다
        terms.pop("pickup", None)
        kind = "conditional" if any(m.type == "conditional" for m in sources) else "offer"
        body = "단가 조정 요청을 검토했습니다.\n" + "\n".join(f"- {line}" for line in lines)
        if conditions:
            body += "\n조건은 앞서 드린 그대로입니다.\n" + "\n".join(f"  · {c}" for c in conditions)
        reply = Message(
            thread_id=thread.id,
            sender=vendor_org,
            recipient=thread.emitter_org,
            type=kind,
            stage=5,
            text=body,
            stream_ids=tuple(s for s in thread.stream_ids if s in items),
            claims=tuple(kept),
        )
        reply.meta["terms"] = {
            "days": list(terms.get("days") or []),
            "pickupsPerMonth": terms.get("pickupsPerMonth"),
            **terms,
            "items": list(items.values()),
            "conditions": conditions,
        }
        problems = [
            r.to_dict()
            for r in validate_message(
                reply, self.registry, today=self.today, names=names
            ).rejections
        ]
        problems += self._guard(guard, reply, reply.meta["terms"], streams)
        if problems:
            logger.info(
                "단가 협상 답을 만들지 못해 평소처럼 답한다: %s", [p["code"] for p in problems]
            )
            return None
        reply.meta["validation"] = {"ok": True, "attempts": 1, "rejections": []}
        reply.meta["model"] = {"provider": "code", "name": "단가 협상(업체 범위 계산)"}
        data = reply.to_dict()
        data["meta"]["trace"] = []
        return data

    def handle(self, vendor_org: str, message: dict[str, Any], context: dict[str, Any]) -> dict:
        """받은 메시지 하나에 답 메시지(dict) 하나. meta에 validation, model, trace가 붙는다."""
        profile = self.store.get_vendor_profile(vendor_org)
        org = self.store.get_org(vendor_org)
        thread = Thread.from_dict(context["thread"])
        if thread.vendor_org != vendor_org:
            raise ValueError("이 업체의 대화가 아니다")
        emitter = dict(context.get("emitter") or {"id": thread.emitter_org})
        # 배출 기업만의 값은 업체 쪽에서 쓰지 않는다
        streams = [
            without_emitter_only(s)
            for s in context.get("streams") or []
            if s["id"] in thread.stream_ids
        ]
        # 담당자가 넣은 이름과 혼입물은 자동 거절 조건 대조에만 쓴다. 모델 입력, 도구, 메시지에는
        # 상대에게 보이는 이름(untrusted.counterparty_stream)을 쓴다
        raw = {s["id"]: s for s in streams}
        streams = [counterparty_stream(s) for s in streams]
        by_id = {s["id"]: s for s in streams}
        if (message.get("meta") or {}).get("priceAsks") and message.get(
            "from"
        ) == thread.emitter_org:
            names = [
                org_label(org.get("name")),
                org_label(emitter.get("name")),
                *[x.get("name") for x in streams],
            ]
            try:
                priced = self._price_reply(vendor_org, profile, thread, message, by_id, names)
            except (KeyError, TypeError, ValueError):  # 협상 답을 만들 수 없으면 평소처럼 답한다
                logger.warning("단가 협상 답을 만들지 못했다", exc_info=True)
                priced = None
            if priced is not None:
                return priced
        guard = profile.get("guardrails") or {}
        trace = Trace()
        pool = ClaimPool(self.registry, vendor_org)
        facts = {s["id"]: pool.add_stream(s) for s in streams}
        criteria = pool.add_vendor_profile(profile)
        # 이 공장에 적용할 수거 상태와 운반비 조건(노선·운반 조건과 공장 지역). 조건을 내는 답에 코드가 붙인다
        fleet = profile.get("fleet") or {}
        haul = pickup_terms.effective(
            profile.get("transport"),
            fleet.get("routes"),
            emitter.get("region"),
            fleet,
            self._pickup_km(vendor_org, fleet, emitter),
            haversine_km(emitter["location"], fleet["depot"])
            if fleet.get("maxPickupKm") is not None
            and emitter.get("location")
            and fleet.get("depot")
            else None,
        )
        haul_refs = _haul_claims(pool, haul, vendor_org)
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

        days = list(guard.get("days") or [])
        auto_declined = {
            s["id"]: reason for s in raw.values() if (reason := rails.auto_decline_reason(guard, s))
        }

        def schema() -> dict[str, Any]:
            prices = [e.claim.id for e in pool.entries() if _price(e.claim) is not None]
            pickups = [e.claim.id for e in pool.entries() if e.claim.unit == "회/월"]
            terms = {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "streamId": enum_string(thread.stream_ids),
                                "unitPriceRef": enum_string(prices),
                            },
                            "required": ["streamId", "unitPriceRef"],
                        },
                    },
                    "days": {"type": "array", "items": enum_string(days or rails.WEEKDAYS)},
                    "pickupsPerMonthRef": enum_string(pickups),
                    "conditions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["items", "days", "conditions"],
            }
            asks = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "streamId": enum_string(thread.stream_ids),
                        "field": enum_string(f for f in questions.ASK_FIELDS if f != "terms"),
                    },
                    "required": ["streamId", "field"],
                },
            }
            excluded = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "streamId": enum_string(thread.stream_ids),
                        "reason": {"type": "string"},
                    },
                    "required": ["streamId", "reason"],
                },
            }
            return turn_schema(
                types=allowed,
                stream_ids=thread.stream_ids,
                tools=TOOLS,
                extra={
                    "terms": terms,
                    "asks": asks,
                    "excluded": excluded,
                    "reason": {"type": "string"},
                },
            )

        def briefing(rejections: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "role": "vendor",
                "task": "reply",
                "guide": guide(
                    types=allowed,
                    stream_ids=thread.stream_ids,
                    streams=by_id,
                    auto_declined=auto_declined,
                    results=tools.results,
                    criteria=criteria,
                    offered=offered,
                    asked=asked,
                    days=days,
                    roles={e.claim.id: e.claim.role for e in pool.entries()},
                    facts=facts,
                ),
                "today": self.today,
                "org": {"id": vendor_org, "name": org_label(org.get("name"))},
                "counterpart": {"id": emitter.get("id"), "name": org_label(emitter.get("name"))},
                "thread": {
                    "id": thread.id,
                    "streamIds": list(thread.stream_ids),
                    "turns": thread.turns,
                    "maxTurns": thread.max_turns,
                    "allowedTypes": list(allowed),
                },
                "guardrails": {
                    "days": days,
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
                        "autoDecline": rails.auto_decline_reason(guard, raw[s["id"]]),
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
                # 이 배출 기업과의 지난 반입 검수 요약(회사 기록). 합의보다 낮게 나온 기록이 많으면 검수 등급 확인을 조건으로 건다
                "pastInspections": context.get("pastInspections"),
                "tools": tools.catalog(),
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
                if reply.type == "question":
                    names = {sid: str(s.get("name") or "") for sid, s in by_id.items()}
                    reply.text = questions.with_ask_lines(reply.text, asks, names)
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
            # 이름에 든 숫자를 수치로 세지 않는 예외는 보이는 이름에만 준다(담당자가 넣은 원래 이름은
            # 이 글에 나올 일이 없다)
            names = [
                org_label(org.get("name")),
                org_label(emitter.get("name")),
                *[x.get("name") for x in streams],
            ]
            # 조건(terms)을 먼저 붙인다. 조건 문장과 요일도 본문과 같은 근거 검증을 받는다
            terms, problems = self._terms(output, reply, pool)
            if terms is not None:
                reply.meta["terms"] = terms
                # 수거·운반 견적(상태, 운반비 방식, 금액·횟수의 claim)을 본문 한 줄과 terms.pickup으로 붙인다
                _attach_haul(reply, output, haul, haul_refs, pool)
                # 회사 기록: 지난 검수가 합의보다 낮게 나온 폐기물에는 검수 등급 확인 조건을 코드가 붙인다
                _attach_past(reply, terms, context.get("pastInspections"), by_id)
            result = validate_message(reply, self.registry, today=self.today, names=names)
            found += [r.to_dict() for r in result.rejections]
            found += problems
            if terms is not None:
                found += self._guard(guard, reply, terms, raw)
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
                        f"{problem['message']} 다른 배출 기업의 이름, 폐기물, 물량은 알릴 수 없습니다.",
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
            schema=schema,
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
                reason=VENDOR_HANDOFF,
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
