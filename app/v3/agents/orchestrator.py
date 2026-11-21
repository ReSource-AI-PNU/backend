"""진행기. 진단 한 건을 멈춤 지점까지 진행한다(docs/v3/RUNTIME.md 3~4장).

멈춤 지점은 awaiting_answers(담당자 답 대기), awaiting_approval(양쪽 승인 대기), confirmed다.
다시 부르면 이어서 진행하고, 이미 한 일은 되풀이하지 않는다. 진행 상태는 세션 레코드의
phase와 work에 남기고, 모든 메시지·도구 호출·검증 결과·handoff·승인은 실행 기록(events)에 남긴다.
화면에 바로 보여 줄 진행(작성 중, 근거 확인, 거부, 다시 쓰기, 보냄)은 self.feed(live.Recorder)로 따로 남긴다.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.v3 import ids, improve, outreach
from app.v3 import names as naming
from app.v3.a2a import guardrails as rails
from app.v3.a2a.guardrails import WEEKDAYS
from app.v3.a2a.message import MESSAGE_TYPES, PLACEHOLDER, Claim, Message
from app.v3.a2a.policy import (
    QuestionPolicy,
    can_open_thread,
    check_disclosure,
    permit_evidence,
)
from app.v3.a2a.thread import Thread, ThreadError
from app.v3.agents import questions, reviewer
from app.v3.agents.company import CompanyAgent
from app.v3.agents.draft import (
    ACCEPT,
    ASK_USER,
    PERMIT_EXCLUDED,
    Outcome,
    Trace,
    id_arguments,
    make_decline,
    make_handoff,
    make_notice,
    model_label,
    public_rejections,
)
from app.v3.agents.pool import safe_id
from app.v3.agents.vendor import public_profile
from app.v3.demo.seed import share_evidence, share_stream, share_vendor_terms
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.evidence.render import render_text
from app.v3.evidence.units import UnitError, convert
from app.v3.evidence.validator import (
    ALLOWED_TOKENS,
    find_numbers,
    name_tokens,
    validate_message,
    validate_text,
)
from app.v3.intake.grade_model import GradeModelError, grade_stream
from app.v3.judge import basis, circular, negotiate, permits
from app.v3.judge import pickup as pickup_terms
from app.v3.judge.common import Recorder as JudgeRecorder
from app.v3.judge.common import (
    best_criterion,
    haversine_km,
    permit_match,
    quality,
    vendor_distance,
    without_emitter_only,
)
from app.v3.judge.toolbox import TOOLS, call_tool
from app.v3.live import STATUS_NOTES, Recorder
from app.v3.store import NotFound
from app.v3.textutil import josa
from app.v3.untrusted import org_label, stream_label

logger = logging.getLogger(__name__)

PHASES = ("identify", "discover", "converse", "propose", "approval", "done")
TERMS_TYPES = ("offer", "counter", "conditional")
MAX_STEPS = 400  # 대화 고리의 안전 상한. 스레드마다 12턴 상한이 먼저 걸린다
STAGE_KEYS = ("1", "2", "3", "4", "5", "6")
STAGE_NUMBERS = (1, 2, 3, 4, 5, 6)
WORST = {"calculated": 0, "conditional": 1, "needs_information": 2}
# 배출 기업의 현행 처리에 관한 검사(judge_route). 업체와 함께 보는 문서와 업체 뷰에는 싣지 않는다
CURRENT_CHECKS = ("current_cost", "current_assumed", "current_method")
# check_loading의 검사 키 → 함께 보여 줄 값(claim 키)
LOADING_CLAIMS = {
    "min_volume": ("totalKg", "minMonthlyKg"),
    "loading": ("pickupsPerMonth", "perPickupKg", "capacityKg", "perPickupM3", "capacityM3"),
    "loading_volume": ("pickupsPerMonth", "perPickupKg", "capacityKg"),
    "monthly_kg": (),
}


# 승인을 막는 사유 코드(approvals[].blockedCodes). 근거 검증기의 거부 코드도 올 수 있다
BLOCK_CODES = ("EVIDENCE_CHANGED", "STREAM_CHANGED", "VENDOR_TERMS_CHANGED")
_BLOCK_CODE = re.compile(r": ([A-Z][A-Z_]+)(?=,|\.|$)")


# 업체 쪽 조건이 바뀌어 막힌 대화. 진단을 다시 진행하면 새 조건으로 대화를 다시 연다
REOPEN_CODES = {"VENDOR_TERMS_CHANGED", "PICKUP_TERMS_CHANGED"}


def block_codes(reason: str | None) -> list[str]:
    """blockedReason 문장에서 사유 코드를 처음 나온 순서대로 뽑는다."""
    return list(dict.fromkeys(_BLOCK_CODE.findall(reason or "")))


def discoverable_profiles(store: Any, emitter: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """배출 기업이 업체 탐색에서 견줄 업체 프로필 {업체 ID: 프로필}.

    - 허가 품목이나 매입 기준이 하나도 없는 프로필(가입만 하고 아직 채우지 않은 업체)은 뺀다.
    - 시연 배출 기업(demo: true)은 시연 업체만 본다. 체험하기로 열리는 시연 진단에 실제 업체가 섞이지
      않게 하려는 것이다. 실제 배출 기업은 시연 업체와 가입한 업체를 모두 본다.
    - 탈퇴한 업체(departed: true. 다른 회사와 확정한 거래 기록 때문에 프로필만 남았다)는 뺀다.
    """
    demo_only = bool(emitter.get("demo"))
    demo_ids = {o["id"] for o in store.list_orgs() if o.get("demo")} if demo_only else set()
    return {
        p["orgId"]: p
        for p in store.list_vendor_profiles()
        if (p.get("permits") or p.get("criteria"))
        and not p.get("departed")
        and (not demo_only or p["orgId"] in demo_ids)
    }


# 대화의 끝 상태(threadOutcome) → 화면 문장. 실행이 멈출 때 실시간 기록과 진단 뷰(threads[].outcome)에 쓴다
THREAD_OUTCOMES = {
    "agreed": "조건이 맞아 양쪽 담당자의 승인을 기다립니다.",
    "confirmed": "확정했습니다.",
    "declined": "업체가 받을 수 없다고 답해 대화를 마쳤습니다.",
    "handoff": "담당자에게 넘겨 대화를 마쳤습니다.",
    "not_selected": "조건을 받았지만 이번 최적안에는 들지 않아 대화를 마쳤습니다.",
    "ended": "조건을 다 받지 못한 채 대화를 마쳤습니다.",
    "waiting": "배출 기업 담당자의 답을 기다리는 동안 멈췄습니다.",
}


def thread_outcome(session: dict[str, Any], thread_id: str, status: str) -> tuple[str | None, str]:
    """(끝 상태, 문장). 열지 않은 대화와 아직 진행 중인 대화는 (None, "")다."""
    if status in ("agreed", "confirmed", "declined", "handoff"):
        return status, THREAD_OUTCOMES[status]
    if status != "open":
        return None, ""
    if session.get("status") == "awaiting_answers":
        return "waiting", THREAD_OUTCOMES["waiting"]
    if session.get("phase") in ("approval", "done") and session.get("status") != "running":
        offered = bool(((session.get("work") or {}).get("offers") or {}).get(thread_id))
        outcome = "not_selected" if offered else "ended"
        return outcome, THREAD_OUTCOMES[outcome]
    return None, ""


class OrchestratorError(RuntimeError):
    """진행기 규칙 위반. code는 서비스 오류 코드로 그대로 쓴다."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def new_work() -> dict[str, Any]:
    return {
        "identify": {},
        "find": {},
        "permits": [],
        "requirements": {},
        "baseline": {},
        "reuse": {},
        "excluded": [],
        "threadTools": {},
        "threadState": {},
        "offers": {},
        "assigned": {},
        "vendorModels": {},
        "cannotAsk": {},
        "handedOff": [],
        "permitChecks": {},
        "agreedBasis": {},
        "agreedTerms": {},
        "reopened": {},
        "renegotiating": {},
        "unresolved": [],
        "optimize": None,
        "outside": None,
        "result": None,
        "stages": dict.fromkeys(STAGE_KEYS, "waiting"),
    }


def memo_result(memo: list[dict[str, Any]], name: str, **arguments: Any) -> dict[str, Any] | None:
    """같은 도구·인자로 부른 결과 가운데 가장 최근 것. 업체 조건이 바뀌어 다시 부른 결과가 앞선 결과를 덮는다."""
    found = None
    for entry in memo or []:
        if entry.get("failed"):
            continue  # 입력을 만들 수 없어 실패한 호출(draft.AgentTools). 결과가 없다
        if entry["name"] == name and all(
            entry["arguments"].get(k) == v for k, v in arguments.items()
        ):
            found = entry["result"]
    return found


def clean_haul(raw: Any) -> dict[str, Any] | None:
    """업체 측이 조건 답에 붙인 수거·운반 견적(terms.pickup)을 정해 둔 모양으로만 남긴다. 상대가 보낸 값은 믿지 않는다."""
    if not isinstance(raw, dict) or raw.get("status") not in pickup_terms.STATUSES:
        return None

    def ref(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    return {
        "status": raw["status"],
        "basis": raw.get("basis") if raw.get("basis") in pickup_terms.BASES else "undecided",
        "paidBy": raw.get("paidBy") if raw.get("paidBy") in ("vendor", "emitter") else None,
        "unit": raw.get("unit") if raw.get("unit") in pickup_terms.UNIT_OF else None,
        "feeRef": ref("feeRef"),
        "pickupsRef": ref("pickupsRef"),
        "days": [d for d in raw.get("days") or [] if d in WEEKDAYS],
        "routeBasis": raw.get("routeBasis")
        if raw.get("routeBasis") in pickup_terms.BASES
        else None,
        "reason": raw.get("reason") if raw.get("reason") in HAUL_REASONS else None,
        # 업체가 적은 수거 가능 지역 이름(짧은 글만)
        "area": _short_text(raw.get("area")),
        "areas": [a for a in map(_short_text, list(raw.get("areas") or [])[:10]) if a],
    }


HAUL_REASONS = (
    "route",
    "area",
    "distance",
    "offRoute",
    "conflict",
    *pickup_terms.ROUTE_CONFLICT_TEXT,
)


def _short_text(value: Any) -> str | None:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text[:50] or None


def _claim_number(claim: Any, unit: str) -> float | None:
    if claim is None:
        return None
    try:
        return convert(float(claim.value), claim.unit, unit)
    except (UnitError, TypeError, ValueError):
        return None


def haul_answer(raw: Any, claims: dict[str, Claim], seq: int) -> dict[str, Any] | None:
    """업체 조건 답의 수거·운반 견적. 금액·횟수는 검증된 claim에서 읽는다(단위가 맞지 않으면 없는 값).

    따로 받는 운반비인데 금액 claim이 없으면 금액은 None이다(0원으로 정하지 않는다).
    """
    haul = clean_haul(raw)
    if haul is None:
        return None
    fee = claims.get(haul["feeRef"] or "")
    count = claims.get(haul["pickupsRef"] or "")
    want = pickup_terms.UNIT_OF.get(haul["unit"] or "")
    amount = _claim_number(fee, want) if (want and haul["basis"] == "separate") else None
    pickups = _claim_number(count, "회/월")
    fee_ev = list(fee.evidence) if fee is not None and amount is not None else []
    count_ev = list(count.evidence) if count is not None and pickups is not None else []
    return {
        "status": haul["status"],
        "days": haul["days"],
        "routeBasis": haul["routeBasis"],
        "reason": haul["reason"],
        "area": haul["area"],
        "areas": haul["areas"],
        "basis": haul["basis"],
        "paidBy": haul["paidBy"],
        "amount": amount,
        "unit": haul["unit"] if amount is not None else None,
        "pickups": pickups,
        "evidence": list(dict.fromkeys([*fee_ev, *count_ev])),
        "feeEvidence": fee_ev,
        "pickupsEvidence": count_ev,
        "fee": fee.to_dict() if fee is not None and amount is not None else None,
        "seq": seq,
        "source": "answer",
    }


def haul_words(shown: dict[str, Any]) -> str:
    """수거·운반 조건을 숫자 없이 적은 말. 예: "별도 수거 확인 필요 · 운반비 미정"."""
    status = pickup_terms.STATUS_LABEL.get(str(shown.get("status")), "수거 방식 미상")
    if shown.get("status") == "unavailable":
        return status
    basis = pickup_terms.BASIS_LABEL.get(str(shown.get("basis")), "미정")
    payer = " (공장 부담)" if shown.get("basis") == "separate" else ""
    return f"{status} · 운반비 {basis}{payer}"


def haul_check(haul: dict[str, Any]) -> dict[str, Any]:
    """결과 한 줄의 "수거 노선" 확인 항목. 문장에는 숫자를 쓰지 않는다."""
    status, reason = haul.get("status"), haul.get("reason")
    days = list(haul.get("days") or [])
    area = haul.get("area")
    areas = [str(a) for a in haul.get("areas") or []]
    listed = ", ".join(areas[:3]) + (" 등" if len(areas) > 3 else "")
    text = pickup_terms.route_conflict_detail(haul) or {
        "route": "업체가 밝힌 수거 노선이 이 공장 지역을 지나갑니다"
        + (f"(운행 요일 {', '.join(days)})." if days else "."),
        "visit": (
            f"공장 지역이 업체가 밝힌 수거 가능 지역({area}) 안에 있습니다. 정기 노선 밖이라 수거 요일은 따로 정합니다."
            if reason == "area" and area
            else "공장까지 거리가 업체가 밝힌 최대 수거 거리 안입니다. 정기 노선 밖이라 수거 요일은 따로 정합니다."
            if reason == "distance"
            else "공장 지역이 업체의 수거 노선 밖이지만 업체가 따로 수거하러 온다고 밝혔습니다."
        ),
        "check": (
            "공장까지 거리를 알 수 없어 업체가 밝힌 최대 수거 거리와 대조하지 못했습니다. 수거하러 올 수 있는지와 "
            "운반비를 확인해야 합니다."
            if reason == "distance"
            else "공장 지역이 업체가 밝힌 수거 가능 지역에는 들지만 업체가 노선 밖 공장은 수거하지 않는다고 밝혔습니다. "
            "수거하러 올 수 있는지와 운반비를 확인해야 합니다."
            if reason == "conflict"
            else "업체가 밝힌 수거 노선에 이 공장 지역이 없습니다. 따로 수거하러 올 수 있는지와 운반비를 확인해야 합니다."
        ),
        "unavailable": (
            f"공장 지역이 업체가 밝힌 수거 가능 지역({listed}) 밖에 있습니다. 업체가 따로 수거하러 오기로 "
            "확인하기 전에는 맡길 수 없습니다."
            if reason == "area" and listed
            else "공장 지역이 업체가 밝힌 수거 가능 지역 밖에 있습니다. 업체가 따로 수거하러 오기로 확인하기 "
            "전에는 맡길 수 없습니다."
            if reason == "area"
            else "공장까지 거리가 업체가 밝힌 최대 수거 거리를 넘습니다. 업체가 따로 수거하러 오기로 확인하기 "
            "전에는 맡길 수 없습니다."
            if reason == "distance"
            else "업체가 이 공장까지 수거하러 올 수 없다고 밝혔습니다."
        ),
        "unknown": "업체가 수거 노선을 밝히지 않아 이 공장까지 수거하러 오는지 확인이 필요합니다.",
    }.get(str(status), "수거 방식을 확인해야 합니다.")
    return {
        "key": "pickup_route",
        "status": "met"
        if status in ("route", "visit")
        else "unmet"
        if status == "unavailable"
        else "conditional",
        "text": text,
        "evidence": list(haul.get("evidence") or []),
    }


def comparison_scope(assignments: list[dict[str, Any]]) -> list[dict[str, str]]:
    """비용·탄소 비교의 계산 범위. 지금 처리 쪽 운송은 자료가 없어 넣지 않았다는 것을 밝힌다(숫자 없음)."""
    moved = [a for a in assignments if a.get("vendorOrgId")]
    if not moved:
        return []
    out = [
        {
            "key": "cost",
            "text": "비용: 지금 처리 쪽은 입력한 처리비(또는 판매 단가)만 넣었고, 지금 따로 내는 운반비는 "
            "자료가 없어 넣지 않았습니다. 옮긴 뒤 쪽은 업체가 밝힌 운반비를 넣었습니다"
            + (
                ". 운반비가 정해지지 않은 곳은 별도 운반비 반영 전 값입니다."
                if any(a.get("transportPending") for a in moved)
                else "."
            ),
        }
    ]
    if any(a.get("carbonTransport") for a in moved):
        out.append(
            {
                "key": "carbon",
                "text": "탄소: 옮긴 뒤 쪽에는 업체까지 실어 가는 운송 배출을 더했고, 지금 처리 쪽 운송 배출은 "
                "자료가 없어 넣지 않았습니다. 그래서 줄어드는 탄소는 실제보다 작게 잡혔을 수 있습니다.",
            }
        )
    return out


def claim_in(result: dict[str, Any] | None, key: str | None, new_id: str) -> dict[str, Any] | None:
    """도구 결과에서 claim 하나를 꺼내 화면용 ID를 붙인다. 없으면 None(확인 필요)."""
    if not result or not key:
        return None
    for claim in result.get("claims") or []:
        if claim["id"] == key:
            # 사람이 읽는 이름과 구실은 도구의 claim 키에서 정한다
            return naming.annotate({**claim, "id": new_id}, key)
    return None


def reuse_view(result: dict[str, Any] | None, tag: str) -> dict[str, Any] | None:
    """결과 한 장에 싣는 공정 재투입 후보. judge_reuse를 부르지 않았으면 None."""
    if not result:
        return None
    output = result.get("output") or {}
    return {
        "eligible": output.get("eligible"),
        "status": result.get("status"),
        "reason": output.get("reason") or "",
        "monthlyAfter": claim_in(result, "monthlyAfter", f"{tag}_reuse_monthlyAfter"),
        "difference": claim_in(result, "difference", f"{tag}_reuse_difference"),
        "checks": [
            {k: check.get(k) for k in ("key", "status", "text", "evidence")}
            for check in result.get("checks") or []
            if check["key"].startswith("reuse_")
        ],
    }


def permit_digest(stream: dict[str, Any], vendor: dict[str, Any]) -> str:
    """허가 품목 대조에 들어가는 값의 지문. 같으면 대조 결과도 같다."""
    return ids.digest([stream.get("wasteItem"), stream.get("material"), vendor.get("permits")])


_CALLED_BY = re.compile(r"^(orchestrator|code|model:[A-Za-z0-9_.\-]{1,40})$")
_TOOL_STATUSES = ("calculated", "conditional", "needs_information", "error")
_CHECK_STATUSES = ("met", "conditional", "unmet")


def reported_trace(trace: Any, registry: Any = None) -> list[dict[str, Any]]:
    """업체 측 에이전트가 답과 함께 보낸 기록(도구 호출, 검증)을 정해 둔 모양으로만 옮긴다.

    별도 서비스로 뜬 업체 측의 기록은 회사 측이 확인할 수 없는 자기 보고다. 글은 옮기지 않고
    도구 이름·상태·코드처럼 정해 둔 값만 남겨 코드가 요약을 다시 쓴다(지어낸 숫자나 없는 도구가
    실행 기록에 실리지 않게 한다). 실행됐다는 도구 호출은 그 도구가 남긴 calc 근거가 근거 저장소에
    실제로 있을 때만 옮긴다(판단 도구는 호출마다 호출 기록을 calc 근거로 남긴다).
    """

    def left_by(tool: str, evidence_id: Any) -> bool:
        if not isinstance(evidence_id, str):
            return False
        if registry is None:
            return evidence_id.startswith(("calc:", "assumption:"))
        try:
            found = registry.get(evidence_id, None)
        except EvidenceNotFound:
            return False
        return found.kind == "assumption" or (found.meta or {}).get("tool") == tool

    out: list[dict[str, Any]] = []
    for event in trace if isinstance(trace, list) else []:
        detail = event.get("detail") if isinstance(event, dict) else None
        if not isinstance(detail, dict):
            continue
        called = detail.get("calledBy")
        if event.get("kind") == "tool_call":
            tool, status = detail.get("tool"), detail.get("status")
            if tool is not None and tool not in TOOLS:
                tool = None
            if status not in _TOOL_STATUSES:
                continue
            kept: dict[str, Any] = {
                "tool": tool,
                "arguments": id_arguments(detail.get("arguments")),
                "status": status,
                "calledBy": called
                if isinstance(called, str) and _CALLED_BY.match(called)
                else None,
            }
            if tool is not None:
                kept["stage"] = TOOLS[tool].stage
            if status == "error":
                kept["error"] = "도구 호출에 실패했습니다."
                summary = f"{tool or '알 수 없는 도구'} · 실패"
            else:
                if tool is None:
                    continue  # 실행된 호출이라면 판단 도구 이름이 있어야 한다
                kept["checks"] = [
                    {"key": str(c.get("key"))[:40], "status": c.get("status")}
                    for c in (
                        detail.get("checks") if isinstance(detail.get("checks"), list) else []
                    )
                    if isinstance(c, dict)
                    and c.get("status") in _CHECK_STATUSES
                    and not find_numbers(str(c.get("key")))
                ]
                kept["evidence"] = [
                    e
                    for e in (
                        detail.get("evidence") if isinstance(detail.get("evidence"), list) else []
                    )
                    if left_by(tool, e)
                ]
                if registry is not None and not kept["evidence"]:
                    continue  # 그 도구가 남긴 근거가 없다. 일어났다고 확인할 수 없는 호출이다
                summary = f"{tool} · {status}"
            out.append(
                {"actor": "vendor_agent", "kind": "tool_call", "summary": summary, "detail": kept}
            )
        elif event.get("kind") == "validation" and isinstance(detail.get("ok"), bool):
            attempt = detail.get("attempt")
            attempt = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0
            model = detail.get("model") if isinstance(detail.get("model"), dict) else {}
            kept = {
                "ok": detail["ok"],
                "attempt": attempt,
                "model": {"provider": str(model.get("provider")), "name": str(model.get("name"))},
            }
            if detail["ok"]:
                if detail.get("type") in MESSAGE_TYPES:
                    kept["type"] = detail["type"]
                summary = f"근거 확인 통과({attempt}번째 작성)"
            else:
                kept["rejections"] = public_rejections(
                    detail.get("rejections") if isinstance(detail.get("rejections"), list) else []
                )
                if detail.get("draftType") in MESSAGE_TYPES:
                    kept["draftType"] = detail["draftType"]
                kept["codes"] = list(dict.fromkeys(r["code"] for r in kept["rejections"]))
                summary = f"근거 확인에서 돌려보냄({attempt}번째 작성)"
            out.append(
                {"actor": "validator", "kind": "validation", "summary": summary, "detail": kept}
            )
    for event in out:
        event["detail"]["reportedBy"] = "vendor"
    return out


def thread_names(store: Any, thread: Any) -> list[str]:
    """대화에 나오는 조직과 흐름의 이름. 검증기가 이름에 든 숫자를 수치로 세지 않게 넘긴다.

    대화 글에 나오는 이름은 보이는 이름(untrusted)이므로 그것만 넘긴다. 담당자가 넣은 원래 이름을
    넘기면 이름에 실어 넣은 숫자·조항("제99조")이 검사를 비켜 간다.
    """
    raw = thread.to_dict() if hasattr(thread, "to_dict") else thread
    names: list[str] = []
    for org_id in (raw.get("emitterOrgId"), raw.get("vendorOrgId")):
        try:
            names.append(org_label(store.get_org(org_id).get("name")))
        except KeyError:
            continue
    for stream_id in raw.get("streamIds") or []:
        try:
            names.append(stream_label(store.get_stream(stream_id)))
        except KeyError:
            continue
    return [n for n in names if n]


class Orchestrator:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.store = app.store
        self.registry = app.registry
        self.policy = QuestionPolicy()
        self.feed = Recorder(app.store)  # 실시간 진행 기록(live.py). 실패해도 진단은 멈추지 않는다

    @property
    def today(self) -> str:
        return self.app.today

    # ── 공통 ──────────────────────────────────────────────────────────
    def _event(self, session_id, actor, kind, summary, thread_id=None, **detail) -> None:
        self.store.add_event(session_id, actor, kind, summary, thread_id=thread_id, detail=detail)

    def _flush(
        self,
        session_id: str,
        trace: Trace | list[dict],
        thread_id: str | None,
        org_id: str | None = None,
    ) -> None:
        """작성 과정의 기록을 실행 기록으로 옮긴다. org_id는 그 일을 한 조직이다."""
        events = trace.events if isinstance(trace, Trace) else trace
        if (
            isinstance(trace, Trace)
            and trace.live is None
            and any(isinstance(e, dict) and e.get("kind") == "tool_call" for e in events)
        ):
            # 차례 알림 없이 만든 기록(진행기가 정해진 순서로 부른 도구)은 끝난 뒤 진행 기록으로 옮긴다
            self.feed.from_trace(session_id, events, self._thread_ref(thread_id), org_id)
        for event in events:
            if not isinstance(event, dict) or not event.get("actor"):
                continue
            detail = dict(event.get("detail") or {})
            if org_id:
                detail.setdefault("orgId", org_id)
            self.store.add_event(
                session_id,
                str(event["actor"]),
                str(event.get("kind") or "validation"),
                str(event.get("summary") or ""),
                thread_id=thread_id,
                detail=detail,
            )

    def _save(self, session: dict[str, Any]) -> None:
        saved = self.store.put_session(session)
        # work 같은 안쪽 객체는 그대로 둔다(진행 중인 참조가 끊기지 않게 한다)
        for key in ("version", "createdAt", "updatedAt"):
            session[key] = saved.get(key)
        self.feed.status(session)  # 상태·단계가 바뀌었을 때만 남는다

    def _thread_ref(self, thread_id: str | None) -> Thread | None:
        """진행 기록에 붙일 대화(ID와 두 당사자). 없으면 None."""
        if not thread_id:
            return None
        try:
            return Thread.from_dict(self.store.get_thread(thread_id))
        except KeyError:
            return None

    def _tool(self, session, name, arguments, *, thread_id=None, summary_ids=None) -> dict:
        """진행기가 정해진 순서로 부르는 판단 도구. 호출은 실행 기록에 남는다."""
        org = session["emitterOrgId"]
        result = call_tool(
            name,
            arguments,
            registry=self.registry,
            org_id=org,
            today=self.today,
            distance_provider=getattr(self.app, "distance_provider", None),
        )
        self._event(
            session["id"],
            "tool",
            "tool_call",
            f"{name} · {result.get('status')}",
            thread_id,
            tool=name,
            stage=TOOLS[name].stage,
            arguments=summary_ids or {},
            status=result.get("status"),
            checks=[{"key": c["key"], "status": c["status"]} for c in result.get("checks") or []],
            evidence=list(result.get("evidence") or []),
            orgId=org,
            calledBy="orchestrator",
        )
        thread = self._thread_ref(thread_id)
        self.feed.tool(
            session["id"],
            name,
            result.get("status"),
            thread=thread,
            side="company" if thread is not None else None,
            org_id=org,
        )
        return result

    def _company(self, session: dict[str, Any]) -> CompanyAgent:
        org = self.store.get_org(session["emitterOrgId"])
        return CompanyAgent(
            org=org,
            llm=self.app.llm_for("company", org["id"], None),
            registry=self.registry,
            today=self.today,
            distance_provider=getattr(self.app, "distance_provider", None),
        )

    def _threads(self, session_id: str) -> list[Thread]:
        return [Thread.from_dict(t) for t in self.store.list_threads(session_id=session_id)]

    def _share_claims(self, message: Message, org_id: str) -> None:
        """메시지에 실린 claim의 근거를 받는 쪽에도 연다."""
        for claim in message.claims:
            for evidence_id in claim.evidence:
                try:
                    share_evidence(self.registry, evidence_id, org_id)
                except (EvidenceNotFound, EvidenceForbidden):
                    continue

    def _share_stream(self, stream: dict[str, Any], vendor_org: str) -> None:
        share_stream(self.registry, stream, vendor_org)
        for evidence_id in (stream.get("answers") or {}).get("evidence") or []:
            share_evidence(self.registry, evidence_id, vendor_org)

    # ── 세션 ──────────────────────────────────────────────────────────
    def create(self, emitter_org_id: str) -> dict[str, Any]:
        session = {
            "id": self.store.next_id("ses"),
            "emitterOrgId": emitter_org_id,
            "status": "idle",
            "phase": "identify",
            "today": self.today,
            "work": new_work(),
        }
        self._save(session)
        self._event(session["id"], "system", "stage", "진단을 만들었습니다.", orgId=emitter_org_id)
        return session

    def _open_questions(self, session_id: str) -> list[dict[str, Any]]:
        return [q for q in self.store.list_questions(session_id) if not q.get("answered")]

    def run(self, session_id: str) -> dict[str, Any]:
        """다음 멈춤 지점까지 진행한다. 멈춰 있는 이유가 그대로면 아무것도 하지 않는다."""
        session = self.store.get_session(session_id)
        if session["status"] == "confirmed":
            return session
        if session["status"] == "awaiting_approval" and not self._renegotiate(session):
            return self.store.get_session(session_id)
        if self._open_questions(session_id):
            if session["status"] != "awaiting_answers":
                session["status"] = "awaiting_answers"
                self._save(session)
            return session
        session["status"] = "running"
        self._save(session)
        steps = {
            "identify": self._identify,
            "discover": self._discover,
            "converse": self._converse,
            "propose": self._propose,
        }
        while session["phase"] in steps:
            self.feed.phase(session)
            paused = steps[session["phase"]](session)
            if paused:
                session["status"] = "awaiting_answers"
                self._save(session)
                self._note_stop(session)
                return session
            session["phase"] = PHASES[PHASES.index(session["phase"]) + 1]
            self._save(session)
        if session["phase"] == "approval":
            session["status"] = "awaiting_approval"
        elif session["status"] == "running":
            session["status"] = "idle"
        self._save(session)
        self._note_stop(session)
        return session

    def _note_stop(self, session: dict[str, Any]) -> None:
        text = STATUS_NOTES.get(str(session.get("status")))
        if text:
            self.feed.note(session["id"], text, org_id=session["emitterOrgId"])
        self._note_threads(session)

    def _note_threads(self, session: dict[str, Any]) -> None:
        """실행이 멈출 때 대화마다 끝 상태를 한 줄씩 남긴다(두 당사자가 함께 본다).

        실시간 화면은 대화마다 마지막 기록으로 상태를 보인다. 실행이 끝났는데 조건을 받고 배정되지 않은
        대화가 "조건을 묻는 중"으로 남지 않게 한다. 지난번에 남긴 끝 상태와 같으면 다시 남기지 않는다.
        """
        last: dict[str, str] = {}
        for entry in self.store.list_activity(session["id"], 0):
            if entry.get("kind") == "status" and entry.get("threadOutcome"):
                last[str(entry.get("threadId"))] = str(entry["threadOutcome"])
        for thread in self._threads(session["id"]):
            outcome, text = thread_outcome(session, thread.id, thread.status)
            if outcome is None or last.get(thread.id) == outcome:
                continue
            self.feed.note(
                session["id"],
                text,
                thread=thread,
                org_id=thread.emitter_org,
                shared=True,
                threadStatus=thread.status,
                threadOutcome=outcome,
            )

    # ── 합의 뒤 업체 조건 변경: 새 조건으로 대화를 이어 간다 ───────────
    def _renegotiate(self, session: dict[str, Any]) -> bool:
        """업체 조건이 바뀌어 승인이 막힌 합의 대화를 다시 연다. 하나라도 열었으면 True.

        다시 여는 것은 VENDOR_TERMS_CHANGED·PICKUP_TERMS_CHANGED로 막힌 대화뿐이다. 흐름 자료가 바뀐 대화
        (STREAM_CHANGED)는 자료를 되돌리거나 진단을 새로 해야 한다.
        """
        opened = False
        for thread in self._threads(session["id"]):
            if thread.status != "agreed":
                continue
            codes = {item.rsplit(": ", 1)[-1] for item in self._verify(session, thread)}
            if codes & REOPEN_CODES and "STREAM_CHANGED" not in codes:
                opened = self._reopen(session, thread, codes) or opened
        if opened:
            session["phase"], session["status"] = "converse", "running"
            for stage in ("4", "5", "6"):
                session["work"]["stages"][stage] = "running"
        self._save(session)
        return opened

    def _reopen(self, session, thread: Thread, codes: set[str] | None = None) -> bool:
        """합의한 대화를 회사 측의 counter로 다시 연다. 앞선 제안과 승인은 지난 조건의 것이다."""
        work = session["work"]
        assigned = sorted(work["assigned"].get(thread.id) or thread.stream_ids)
        failed, evidence = self._permit_failures(session, thread, assigned)
        if failed:
            # 허가 품목이 빠진 흐름이 있다. 맞는 흐름이 남지 않으면 닫고, 일부면 그 흐름만 뺀다
            if self._drop_for_permit(session, thread, failed, evidence) == "closed":
                return False
            if thread.status == "agreed":
                left = {item.rsplit(": ", 1)[-1] for item in self._verify(session, thread)}
                if not left & REOPEN_CODES:
                    return False  # 남은 흐름의 조건은 그대로다. 다시 승인만 받으면 된다
        gone = {x["streamId"] for x in work["excluded"] if x["vendorOrgId"] == thread.vendor_org}
        left_streams = [sid for sid in thread.stream_ids if sid not in gone]
        message = make_notice(
            kind="counter",
            thread_id=thread.id,
            sender=thread.emitter_org,
            recipient=thread.vendor_org,
            text=(
                "합의한 뒤에 업체의 수거·운반 조건이 바뀌었습니다. "
                if codes and "PICKUP_TERMS_CHANGED" in codes and "VENDOR_TERMS_CHANGED" not in codes
                else "합의한 뒤에 업체의 조건(매입 기준표·허가 품목·가드레일·수거·운반)이 바뀌었습니다. "
            )
            + "지금 조건으로 받을 수 있는지와 조건을 다시 알려 주세요.",
            stage=5,
            stream_ids=tuple(left_streams),
            asks=[{"streamId": sid, "field": "terms"} for sid in left_streams],
        )
        try:
            thread.check(message)
        except ThreadError:  # 턴 상한에 닿았다. 더 협상하지 않고 담당자에게 넘긴다
            self._handoff(
                session,
                thread,
                thread.emitter_org,
                "업체의 조건이 바뀌었지만 주고받을 수 있는 횟수를 다 써서 담당자 확인이 필요합니다.",
                stage=5,
            )
            return False
        was_agreed = thread.status == "agreed"
        self._append(session, thread, message, "company_agent")  # 합의와 승인이 풀린다
        for side in ("emitter", "vendor"):
            self.store.set_approval(session["id"], thread.id, side, False)
        self.store.block_approval(session["id"], thread.id, None)
        before = work["assigned"].pop(thread.id, None)
        if before:
            # 다시 합의하지 못하고 닫히면 이 흐름들은 결과 한 장에서 "확인 필요"가 된다
            work.setdefault("renegotiating", {})[thread.id] = list(before)
        work["offers"].pop(thread.id, None)  # 지난 조건으로 받은 제안은 더 쓰지 않는다
        # 지난 조건으로 협상한 단가도 더 쓰지 않는다(남겨 두면 업체가 다시 낸 기준표 단가와 계산에 쓰는 협상 단가가
        # 어긋난다). 수거·운반 조건만 바뀌었으면 단가 합의는 그대로여야 하므로, 업체가 새 조건으로 답한 뒤 합의 전
        # 단가 협상을 다시 한다. 업체가 단가 조건을 바꿨으면 업체가 새로 낸 단가를 그대로 쓴다
        work.setdefault("negotiated", {}).pop(thread.vendor_org, None)
        work.setdefault("negotiation", {}).pop(thread.id, None)
        pickup_only = bool(codes) and codes & REOPEN_CODES == {"PICKUP_TERMS_CHANGED"}
        work.setdefault("renegotiatePrice", {})[thread.id] = pickup_only
        work.setdefault("agreedBasis", {}).pop(thread.id, None)
        work.setdefault("agreedTerms", {}).pop(thread.id, None)
        work.setdefault("pickups", {}).pop(thread.vendor_org, None)  # 새 수거·운반 답을 받는다
        work.setdefault("reopened", {})[thread.id] = message.seq
        work["cannotAsk"].pop(thread.id, None)
        # 업체가 고친 기준표의 새 근거를 배출 기업에 연다(대화를 열 때와 같다)
        share_vendor_terms(
            self.registry, self.store.get_vendor_profile(thread.vendor_org), thread.emitter_org
        )
        self._event(
            session["id"],
            "system",
            "thread",
            "합의한 뒤에 업체의 조건이 바뀌어 대화를 다시 열었습니다. 새 조건으로 이어 갑니다."
            if was_agreed
            else "업체의 조건이 바뀌어 남은 폐기물의 조건을 다시 묻습니다.",
            thread.id,
            status=thread.status,
            vendorOrgId=thread.vendor_org,
            streamIds=left_streams,
            reopenedSeq=message.seq,
        )
        return True

    # ── ① 식별·분류 ───────────────────────────────────────────────────
    def _identify(self, session: dict[str, Any]) -> bool:
        work, org = session["work"], session["emitterOrgId"]
        work["stages"]["1"] = "running"
        pending: list[dict[str, Any]] = []
        missing = False
        for stream in self.store.list_streams(org):
            sid, digest = stream["id"], ids.digest(stream)
            done = work["identify"].get(sid)
            if not done or done["digest"] != digest:
                result = self._tool(
                    session, "identify_stream", {"stream": stream}, summary_ids={"streamId": sid}
                )
                answers = stream.get("answers") or {}
                try:
                    grade = grade_stream(
                        self.registry,
                        org,
                        stream,
                        getattr(self.app, "grade_client", None),
                        self.today,
                        asked_moisture_pct=answers.get("moisturePct"),
                    )
                except GradeModelError as error:
                    grade = {"grade": None, "basis": None, "evidence": [], "error": str(error)}
                    self._event(
                        session["id"],
                        "system",
                        "tool_call",
                        "등급 엔드포인트 호출 실패",
                        error=str(error),
                    )
                done = {
                    "digest": digest,
                    "result": result,
                    "grade": {
                        "value": grade.get("grade"),
                        "basis": grade.get("basis"),
                        "label": grade.get("label"),
                        "modelBasis": grade.get("modelBasis"),
                        "evidence": list(grade.get("evidence") or []),
                        "assumptions": list(grade.get("assumptions") or []),
                    },
                }
                work["identify"][sid] = done
            pending += questions.from_identify(stream, done["result"])
            missing = missing or done["grade"]["value"] is None
        history = self.store.list_questions(session["id"])
        batch = self.policy.next_batch(questions.ordered(pending), history)
        if batch:
            self._ask(session, batch, stage=1)
            return True
        work["unresolved"] = [
            {"streamId": q["streamId"], "field": q["field"]}
            for q in self.policy.unresolved(pending, history)
            # 현행 처리비는 몰라도 혼합 소각 가정값으로 견준다. 업체 배정을 막는 값이 아니다
            if q["field"] not in questions.PRIVATE_FIELDS
        ]
        work["stages"]["1"] = "needs_check" if missing or work["unresolved"] else "done"
        return False

    def _ask(
        self, session: dict[str, Any], batch: list[dict[str, Any]], stage: int | None = None
    ) -> None:
        asked = []
        for question in batch:
            question = {"id": self.store.next_id("q"), **question}
            self.store.put_question(session["id"], question)
            asked.append(question)
            self._event(
                session["id"],
                "system",
                "question",
                f"담당자 질문({question['batch']}번째 묶음): {question['text']}",
                questionId=question["id"],
                streamId=question["streamId"],
                field=question["field"],
                batch=question["batch"],
            )
        self.feed.questions(session, asked, stage)

    def answer(self, session_id: str, question_id: str, value: Any) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        question = next(
            (q for q in self.store.list_questions(session_id) if q["id"] == question_id), None
        )
        if question is None:
            raise OrchestratorError("QUESTION_NOT_FOUND", "그런 질문이 없습니다.", 404)
        if question.get("answered"):
            return session  # 같은 답을 두 번 보내도 한 번만 적는다
        stream = self.store.get_stream(question["streamId"], session["emitterOrgId"])
        try:
            parsed = questions.parse_answer(question["field"], value)
            # 계약 꼴 답(회당·월 정액)은 질문 기록에 사람이 읽는 글로 남긴다
            shown = parsed["text"] if isinstance(parsed, dict) else parsed
            recorded = self.policy.record_answer(question, shown)
            stream = questions.apply_answer(
                self.registry, stream, question["field"], parsed, self.today
            )
        except ValueError as error:
            raise OrchestratorError("INVALID_ANSWER", str(error), 422) from error
        self.store.put_stream(stream)
        self.store.put_question(session_id, recorded)
        self._event(
            session_id,
            "system",
            "question",
            "담당자가 '모름'으로 답했습니다."
            if recorded["unknown"]
            else "담당자가 질문에 답했습니다.",
            questionId=question_id,
            streamId=question["streamId"],
            field=question["field"],
            unknown=recorded["unknown"],
        )
        if not self._open_questions(session_id) and session["status"] == "awaiting_answers":
            session["status"] = "idle"
            self._save(session)
        return self.store.get_session(session_id)

    # ── ②③④ 가능성, 규정, 업체 탐색과 문의 ────────────────────────────
    def _find_outside(
        self, session, emitter: dict[str, Any], streams: list[dict[str, Any]]
    ) -> None:
        """가입하지 않은 업체 후보(공공데이터 허가 명부). 대화는 열지 않고 결과 한 장에만 싣는다.

        찾을 때 본 흐름 칸은 work.outsideBasis에 남긴다. 견적 요청서를 만들 때 지금 자료와 견준다.
        """
        try:
            session["work"]["outside"] = outreach.find_outside(
                self.registry, self.store, emitter, streams, self.today
            )
        except Exception:  # 명부 문제로 진단이 멈추면 안 된다(후보 없이 진행)
            logger.warning("가입하지 않은 업체 후보를 찾지 못했다", exc_info=True)
            session["work"]["outside"] = None
            session["work"]["outsideBasis"] = None
            return
        session["work"]["outsideBasis"] = {s["id"]: outreach.stream_basis(s) for s in streams}
        found = sum(len(s["candidates"]) for s in session["work"]["outside"]["streams"])
        self.feed.activity(
            session["id"],
            "tool",
            "판단 도구 · 가입하지 않은 업체 찾기: "
            + ("허가 명부에서 후보를 찾았습니다" if found else "허가 명부에 맞는 곳이 없습니다"),
            stage=4,
            actor="tool",
            org_id=session["emitterOrgId"],
            tool=outreach.TOOL,
            toolStatus="calculated",
        )

    def _discover(self, session: dict[str, Any]) -> bool:
        work, org_id = session["work"], session["emitterOrgId"]
        emitter = self.store.get_org(org_id)
        streams = self.store.list_streams(org_id)
        profiles = discoverable_profiles(self.store, emitter)
        for profile in profiles.values():  # 허가증은 대조하기 전에 연다(올린 허가증의 AI 판독도)
            for item in [*(profile.get("permits") or []), *(profile.get("permitDocuments") or [])]:
                for evidence_id in item.get("evidence") or []:
                    share_evidence(self.registry, evidence_id, org_id)
        publics = {v: public_profile(p, fleet_cost=False) for v, p in profiles.items()}
        work["stages"].update({"3": "running", "4": "running"})
        self._find_outside(session, emitter, streams)

        matched: dict[str, list[dict]] = {v: [] for v in publics}
        refused: dict[str, list[dict]] = {v: [] for v in publics}
        for stream in streams:
            sid = stream["id"]
            arguments = {"stream": stream, "vendors": [publics[v] for v in sorted(publics)]}
            if emitter.get("location"):
                arguments["origin"] = list(emitter["location"])
            found = self._tool(session, "find_vendors", arguments, summary_ids={"streamId": sid})
            work["find"][sid] = found
            candidates = [c["vendorOrgId"] for c in found["output"]["candidates"]]
            reasons = {x["vendorOrgId"]: x["reason"] for x in found["output"]["excluded"]}
            for vendor_id in sorted(publics):
                permit = self._tool(
                    session,
                    "match_permit",
                    {"stream": stream, "vendor": publics[vendor_id]},
                    summary_ids={"streamId": sid, "vendorOrgId": vendor_id},
                )
                ok, reason = can_open_thread(permit)
                evidence = permit_evidence(permit)
                work.setdefault("permitChecks", {}).setdefault(vendor_id, {})[sid] = {
                    "digest": permit_digest(stream, publics[vendor_id]),
                    "ok": ok,
                    "evidence": evidence,
                }
                work["permits"].append(
                    {
                        "streamId": sid,
                        "vendorOrgId": vendor_id,
                        "matched": ok,
                        "reason": reason,
                        "evidence": evidence,
                    }
                )
                if not ok:
                    refused[vendor_id].append(
                        {
                            "stream": stream,
                            "evidence": evidence,
                            "unknown": permit["output"].get("matched") is None,
                        }
                    )
                elif vendor_id in candidates:
                    matched[vendor_id].append(stream)
                else:
                    work["excluded"].append(
                        {
                            "streamId": sid,
                            "vendorOrgId": vendor_id,
                            "reason": reasons.get(vendor_id, "업체 탐색에서 걸러졌습니다."),
                            "stage": 4,
                        }
                    )

        opened: list[Thread] = []
        for vendor_id in sorted(publics):
            name = self.store.get_org(vendor_id).get("name") or vendor_id
            if matched[vendor_id]:
                thread = Thread(
                    id=self.store.next_id("th"),
                    session_id=session["id"],
                    emitter_org=org_id,
                    vendor_org=vendor_id,
                    stream_ids=tuple(s["id"] for s in matched[vendor_id]),
                )
                for stream in matched[vendor_id]:
                    self._share_stream(stream, vendor_id)
                share_vendor_terms(self.registry, profiles[vendor_id], org_id)
                self.store.put_thread(thread)
                work["threadState"][thread.id] = "new"
                work["threadTools"][thread.id] = []
                opened.append(thread)
                self._event(
                    session["id"],
                    "system",
                    "thread",
                    f"{josa(name, '와과')} 대화를 엽니다(허가 품목 일치).",
                    thread.id,
                    vendorOrgId=vendor_id,
                    streamIds=list(thread.stream_ids),
                )
                self.feed.opened(session["id"], thread, name)
            if refused[vendor_id]:
                thread = Thread(
                    id=self.store.next_id("th"),
                    session_id=session["id"],
                    emitter_org=org_id,
                    vendor_org=vendor_id,
                    stream_ids=tuple(x["stream"]["id"] for x in refused[vendor_id]),
                )
                names = ", ".join(
                    f"{x['stream'].get('name')}({x['stream'].get('wasteItem')}·"
                    f"{x['stream'].get('material')})"
                    for x in refused[vendor_id]
                )
                evidence = list(dict.fromkeys(e for x in refused[vendor_id] for e in x["evidence"]))
                if all(x["unknown"] for x in refused[vendor_id]):
                    reason = (
                        f"업체의 허가 품목을 확인하지 못해 {names}에 대한 대화를 열지 않습니다."
                    )
                else:
                    reason = f"업체의 허가 품목에 {josa(names, '이가')} 없어 대화를 열지 않습니다."
                thread.refuse_open(reason, evidence)
                self.store.put_thread(thread)
                self._event(
                    session["id"],
                    "system",
                    "thread",
                    f"{name}: {reason}",
                    thread.id,
                    vendorOrgId=vendor_id,
                    streamIds=list(thread.stream_ids),
                    status="not_opened",
                    reasonEvidence=evidence,
                )

        agent = self._company(session)
        by_id = {s["id"]: s for s in streams}
        routes: dict[str, list[str]] = {s["id"]: [] for s in streams}
        work["stages"]["2"] = "running"
        for thread in opened:  # ② 흐름마다 업체 매입 기준과 대조
            _, trace = agent.run_tools(
                [("judge_route", {"streamId": sid}) for sid in thread.stream_ids],
                streams=[by_id[s] for s in thread.stream_ids],
                vendor=publics[thread.vendor_org],
                memo=work["threadTools"][thread.id],
            )
            self._flush(session["id"], trace, thread.id, org_id)
            for sid in thread.stream_ids:
                result = memo_result(work["threadTools"][thread.id], "judge_route", streamId=sid)
                if result and result["output"].get("route"):
                    routes[sid].append(result["output"]["route"])
        judged = [
            (memo_result(work["threadTools"][t.id], "judge_route", streamId=sid) or {}).get(
                "status"
            )
            for t in opened
            for sid in t.stream_ids
        ]
        work["stages"]["2"] = (
            "needs_check"
            if not judged or any(j in (None, "needs_information") for j in judged)
            else "done"
        )
        states = []
        for stream in streams:  # ③ 규정 검토
            sid = stream["id"]
            if not routes[sid]:
                work["baseline"][sid] = self._tool(
                    session, "judge_route", {"stream": stream}, summary_ids={"streamId": sid}
                )
            # ② 경로 후보 가운데 공정 재투입(업체가 끼지 않는다)
            work.setdefault("reuse", {})[sid] = self._tool(
                session, "judge_reuse", {"stream": stream}, summary_ids={"streamId": sid}
            )
            route = next((r for r in ("sell", "treat") if r in routes[sid]), "keep")
            # 허가 요건은 대화를 연 업체(허가 품목이 맞아 연 곳)로 판정한다
            talked = [
                self._assign_vendor(session, profiles[t.vendor_org])
                for t in opened
                if sid in t.stream_ids
            ]
            arguments = {"stream": stream, "route": route}
            if talked:
                arguments["vendors"] = talked
            required = self._tool(
                session,
                "check_requirements",
                arguments,
                summary_ids={"streamId": sid, "route": route},
            )
            work["requirements"][sid] = required
            states.append(required["status"])
            if (work["reuse"][sid]["output"] or {}).get("eligible"):
                # 공정 재투입 후보면 사업장 안에서 재활용할 때 지킬 것도 함께 낸다(배정을 막지 않는다)
                work.setdefault("reuseRequirements", {})[sid] = self._tool(
                    session,
                    "check_requirements",
                    {"stream": stream, "route": "reuse"},
                    summary_ids={"streamId": sid, "route": "reuse"},
                )
        work["stages"]["3"] = "needs_check" if any(s != "calculated" for s in states) else "done"

        for thread in opened:  # ④ 연 스레드마다 문의
            with self.feed.turn(session["id"], thread, "company", 4):
                outcome, trace = agent.compose(
                    thread,
                    "inquiry",
                    streams=[by_id[s] for s in thread.stream_ids],
                    vendor=publics[thread.vendor_org],
                    vendor_org=self.store.get_org(thread.vendor_org),
                    memo=work["threadTools"][thread.id],
                )
            self._send(session, thread, outcome, trace, stage=4)
        return False

    # ── 메시지 보내기와 받기 ──────────────────────────────────────────
    def _fingerprints(self, message: Message) -> dict[str, str]:
        """메시지가 인용한 근거의 지금 내용. 나중에 근거가 바뀌었는지 볼 때 쓴다."""
        marks: dict[str, str] = {}
        for claim in message.claims:
            for evidence_id in claim.evidence:
                try:
                    found = self.registry.get(evidence_id, None)
                except EvidenceNotFound:
                    continue
                marks[evidence_id] = ids.digest(
                    [found.value, found.unit, found.excerpt, found.valid_until]
                )
        return marks

    def _append(self, session, thread: Thread, message: Message, actor: str) -> None:
        message.meta["evidenceMarks"] = self._fingerprints(message)
        thread.append(message, now=self.store.now())
        if message.type == "decline":
            thread.status_reason = self._decline_reason(thread, message)
        self._share_claims(message, thread.counterpart(message.sender))
        self.store.put_thread(thread)
        validation = message.meta.get("validation") or {}
        self._event(
            session["id"],
            actor,
            "handoff" if message.type == "handoff" else "message",
            f"{message.type} · {self.store.get_org(message.sender).get('name')}",
            thread.id,
            type=message.type,
            seq=message.seq,
            stage=message.stage,
            streamIds=list(message.stream_ids),
            messageId=f"{thread.id}_m{message.seq}",
            **{"from": message.sender, "to": message.recipient},
            model=message.meta.get("model"),
            attempts=validation.get("attempts"),
            reason=message.meta.get("reason"),
        )
        self.feed.sent(session["id"], thread, message)
        state = session["work"]["threadState"]
        if thread.closed:
            state[thread.id] = "closed"
        elif message.sender == thread.emitter_org:
            state[thread.id] = "awaiting_vendor"
        else:
            state[thread.id] = "awaiting_company"

    def _decline_reason(self, thread: Thread, message: Message) -> str:
        """닫는 이유. 여러 흐름을 거절했으면 거절한 흐름을 이유와 함께 모두 적는다.

        모델이 고른 한 문장(meta.reason)에는 흐름 하나의 이유만 있기 쉽다. 흐름 이름은 저장소에서
        읽고 이유는 검증을 거친 meta.excluded의 문장을 쓴다. 이유가 같은 흐름은 한데 묶는다.
        """
        base = str(message.meta.get("reason") or message.text)
        groups: dict[str, list[str]] = {}
        for item in message.meta.get("excluded") or []:
            if not isinstance(item, dict) or item.get("streamId") not in thread.stream_ids:
                continue
            reason = str(item.get("reason") or "").strip()
            if not reason:
                continue
            try:
                name = stream_label(self.store.get_stream(item["streamId"]))
            except KeyError:
                name = item["streamId"]
            if name not in groups.setdefault(reason, []):
                groups[reason].append(str(name))
        if sum(len(names) for names in groups.values()) < 2:
            return base
        listed = " / ".join(
            # 이유 문장이 흐름 이름으로 시작하면 이름을 한 번 더 적지 않는다
            reason.rstrip(".")
            if len(names) == 1 and reason.startswith(names[0])
            else f"{', '.join(names)}: {reason.rstrip('.')}"
            for reason, names in groups.items()
        )
        head = "" if base in groups else f"{base.rstrip('.')}. "
        return f"{head}받지 않는 폐기물: {listed}."

    def _handoff(
        self,
        session,
        thread: Thread,
        sender: str,
        reason: str,
        *,
        stage=4,
        outcome=None,
        reassign: bool = True,
    ) -> bool:
        """대화를 담당자에게 넘긴다. 합의한 대화였으면 True(최적안을 다시 계산해야 한다).

        reassign이 참이면 여기서 바로 다시 계산한다. 여러 대화를 한꺼번에 넘길 때는 거짓으로 주고
        마지막에 _reassign을 한 번 부른다.
        """
        if thread.closed or thread.blocked:
            return False
        message = make_handoff(
            thread_id=thread.id,
            sender=sender,
            recipient=thread.counterpart(sender),
            reason=reason,
            stage=stage,
            rejections=outcome.rejections if outcome else None,
            attempts=outcome.attempts if outcome else 0,
        )
        actor = "company_agent" if sender == thread.emitter_org else "vendor_agent"
        self._append(session, thread, message, actor)
        return self._release(session, thread, reassign=reassign, cause="handoff")

    def _release(self, session, thread: Thread, *, reassign: bool, cause: str) -> bool:
        """닫힌 대화의 승인과 배정을 지운다. 합의한 대화였으면 True(최적안을 다시 계산해야 한다)."""
        if any(a["threadId"] == thread.id for a in self.store.list_approvals(session["id"])):
            self.store.block_approval(session["id"], thread.id, None)
            for side in ("emitter", "vendor"):
                self.store.set_approval(session["id"], thread.id, side, False)
        work = session["work"]
        work.setdefault("agreedBasis", {}).pop(thread.id, None)
        work.setdefault("agreedTerms", {}).pop(thread.id, None)
        work["offers"].pop(thread.id, None)  # 닫힌 대화의 제안은 더 쓰지 않는다
        # 조건이 바뀌어 다시 협상하던 대화였으면, 앞서 합의했던 흐름이 풀려난 것으로 본다
        again = work.setdefault("renegotiating", {}).pop(thread.id, None)
        freed = work["assigned"].pop(thread.id, None) or again
        if not freed or work.get("result") is None:
            return False
        # 합의한 대화가 닫혔다. 그 배정은 결과 한 장에서 빠지고 흐름은 "확인 필요"가 된다
        handed = work.setdefault("handedOff", [])
        for sid in freed:
            entry = {
                "streamId": sid,
                "vendorOrgId": thread.vendor_org,
                "threadId": thread.id,
                "cause": cause,
            }
            if entry not in handed:
                handed.append(entry)
        # 재협상 중(대화·최적안 단계)에는 여기서 다시 계산하지 않는다. 그 단계의 끝에 한 번 한다
        if reassign and session.get("phase") not in ("converse", "propose"):
            self._reassign(session, cause=cause)
        return True

    # ── 허가 품목: 연 뒤에 맞지 않게 되면 근거와 함께 닫는다 ───────────
    def _permit_failures(
        self, session, thread: Thread, stream_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """지금의 업체 프로필로 허가 품목을 다시 대조한다. (맞지 않는 흐름, 허가증 근거).

        대조는 판단 도구(match_permit)가 한다. 흐름의 품목·재질과 허가 품목이 지난번 대조와 같으면
        다시 부르지 않는다.
        """
        work = session["work"]
        profile = self.store.get_vendor_profile(thread.vendor_org)
        permits = [e for p in profile.get("permits") or [] for e in p.get("evidence") or []]
        for evidence_id in permits:  # 허가증 근거는 대조하기 전에 배출 기업에 연다
            try:
                share_evidence(self.registry, evidence_id, thread.emitter_org)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
        public = public_profile(profile, fleet_cost=False)
        seen = work.setdefault("permitChecks", {}).setdefault(thread.vendor_org, {})
        failed: list[str] = []
        evidence: list[str] = []
        for sid in stream_ids:
            stream = self.store.get_stream(sid)
            digest = permit_digest(stream, public)
            done = seen.get(sid)
            if not done or done["digest"] != digest:
                permit = self._tool(
                    session,
                    "match_permit",
                    {"stream": stream, "vendor": public},
                    thread_id=thread.id,
                    summary_ids={
                        "streamId": sid,
                        "vendorOrgId": thread.vendor_org,
                        "reason": "recheck",
                    },
                )
                ok, _ = can_open_thread(permit)
                done = {"digest": digest, "ok": ok, "evidence": permit_evidence(permit)}
                seen[sid] = done
            if not done["ok"]:
                failed.append(sid)
                evidence += done["evidence"]
        if failed:
            evidence += permits
        return failed, list(dict.fromkeys(evidence))

    def _decline_for_permit(
        self,
        session,
        thread: Thread,
        failed: list[str],
        evidence: list[str],
        *,
        reassign=True,
        reason: str | None = None,
    ) -> bool:
        """허가 품목이 맞지 않음을 연 뒤에 알았다. 회사 측이 근거와 함께 decline으로 닫는다."""
        if thread.closed or thread.blocked:
            return False
        reason = (
            reason or "업체의 허가 품목이 이 대화의 폐기물과 맞지 않아 근거와 함께 대화를 닫습니다."
        )
        message = make_decline(
            thread_id=thread.id,
            sender=thread.emitter_org,
            recipient=thread.vendor_org,
            reason=reason,
            stage=4,
            stream_ids=tuple(s for s in thread.stream_ids if s in failed),
            evidence=evidence,
            excluded=[{"streamId": sid, "reason": PERMIT_EXCLUDED} for sid in failed],
        )
        try:
            thread.check(message)
        except ThreadError:  # 턴 상한 등으로 decline을 보낼 수 없으면 담당자에게 넘긴다
            return self._handoff(session, thread, thread.emitter_org, reason, reassign=reassign)
        self._append(session, thread, message, "company_agent")
        self._mark_excluded(session, thread, failed, evidence)
        return self._release(session, thread, reassign=reassign, cause="permit")

    def _mark_excluded(self, session, thread: Thread, failed: list[str], evidence: list[str]):
        """허가 품목이 맞지 않는 흐름을 이 업체의 후보에서 뺀다(받은 제안도 지운다)."""
        work = session["work"]
        offers = work["offers"].get(thread.id) or {}
        for sid in failed:
            entry = {
                "streamId": sid,
                "vendorOrgId": thread.vendor_org,
                "reason": PERMIT_EXCLUDED,
                "stage": 4,
            }
            if not any({k: x.get(k) for k in entry} == entry for x in work["excluded"]):
                work["excluded"].append({**entry, "evidence": list(evidence)})
            offers.pop(sid, None)

    def _permitted_left(
        self, session, thread: Thread, failed: list[str], evidence: list[str]
    ) -> tuple[list[str], list[str], list[str]]:
        """이 대화의 나머지 흐름도 지금 프로필로 다시 대조한다.

        (허가가 맞아 남는 흐름, 맞지 않는 흐름 전부, 허가증 근거). 이미 받지 않기로 한 흐름은 보지 않는다.
        """
        gone = {
            x["streamId"]
            for x in session["work"]["excluded"]
            if x["vendorOrgId"] == thread.vendor_org
        }
        others = [s for s in thread.stream_ids if s not in failed and s not in gone]
        more, found = self._permit_failures(session, thread, others)
        return (
            [s for s in others if s not in more],
            list(dict.fromkeys([*failed, *more])),
            list(dict.fromkeys([*evidence, *found])),
        )

    def _drop_for_permit(
        self,
        session,
        thread: Thread,
        failed: list[str],
        evidence: list[str],
        *,
        reassign: bool = True,
        ask: bool = False,
        reason: str | None = None,
        recheck: bool = True,
    ) -> str:
        """허가 품목이 맞지 않는 흐름을 연 뒤에 알았다. "closed"(대화를 닫음) | "excluded"(그 흐름만 뺌).

        이 대화에 허가가 맞는 흐름이 남지 않으면 근거와 함께 decline으로 닫는다. 일부만 맞지 않으면
        대화 전체를 닫지 않고 그 흐름만 빼고 나머지는 그대로 둔다(나머지에 받은 제안도 남는다).
        ask가 참이면 회사 측이 남은 흐름의 조건을 다시 묻는다(방금 받은 답을 붙이지 않았을 때).
        합의한 대화에서 배정된 흐름이 빠지면 승인을 지우고 최적안을 다시 계산한다.
        """
        if thread.closed or thread.blocked:
            return "closed"
        if recheck:
            left, failed, evidence = self._permitted_left(session, thread, failed, evidence)
        else:
            # 허가 품목은 맞지만 원문 대조가 이 흐름만 맞지 않다고 했다. 나머지 흐름은 그대로 남는다
            gone = {
                x["streamId"]
                for x in session["work"]["excluded"]
                if x["vendorOrgId"] == thread.vendor_org
            }
            left = [sid for sid in thread.stream_ids if sid not in failed and sid not in gone]
        if not left:
            self._decline_for_permit(
                session, thread, failed, evidence, reassign=reassign, reason=reason
            )
            return "closed"
        work = session["work"]
        self._mark_excluded(session, thread, failed, evidence)
        names = ", ".join(stream_label(self.store.get_stream(sid)) for sid in failed)
        self._event(
            session["id"],
            "system",
            "thread",
            f"허가증 원문 대조가 맞지 않은 폐기물({names})만 이 대화에서 뺐습니다. 나머지 폐기물은 그대로 둡니다."
            if reason
            else f"허가 품목이 맞지 않는 폐기물({names})만 이 대화에서 뺐습니다. 나머지 폐기물은 그대로 둡니다.",
            thread.id,
            vendorOrgId=thread.vendor_org,
            status=thread.status,
            excludedStreamIds=list(failed),
            streamIds=left,
            reasonEvidence=list(evidence),
        )
        assigned = list(work["assigned"].get(thread.id) or [])
        freed = [sid for sid in assigned if sid in failed]
        if freed:
            # 합의에 들어 있던 흐름이 빠졌다. 앞선 승인은 빠지기 전의 조건에 대한 것이다
            kept = [sid for sid in assigned if sid not in failed]
            for side in ("emitter", "vendor"):
                thread.approvals[side] = False
                self.store.set_approval(session["id"], thread.id, side, False)
            self.store.block_approval(session["id"], thread.id, None)
            handed = work.setdefault("handedOff", [])
            for sid in freed:
                entry = {
                    "streamId": sid,
                    "vendorOrgId": thread.vendor_org,
                    "threadId": thread.id,
                    "cause": "permit_check" if reason else "permit_stream",
                }
                if entry not in handed:
                    handed.append(entry)
            if kept:
                work["assigned"][thread.id] = kept
                work.setdefault("agreedBasis", {})[thread.id] = self._basis(session, thread)
                terms = work.setdefault("agreedTerms", {}).get(thread.id)
                for sid in freed if terms else []:
                    # 남은 흐름의 합의 조건 지문은 그대로 둔다(그 조건이 바뀌었는지는 계속 본다)
                    terms["streams"].pop(sid, None)
                    terms["inputs"].pop(sid, None)
                profiles = {p["orgId"]: p for p in self.store.list_vendor_profiles()}
                streams = self.store.list_streams(session["emitterOrgId"])
                self._loading_for(session, thread, kept, streams, profiles)
            else:
                # 합의한 흐름이 모두 빠졌다. 남은 흐름으로 대화를 이어 갈 수 있게 합의를 푼다
                work["assigned"].pop(thread.id, None)
                work.setdefault("agreedBasis", {}).pop(thread.id, None)
                work.setdefault("agreedTerms", {}).pop(thread.id, None)
                if thread.status == "agreed":
                    thread.status, thread.agreed_seq = "open", None
            self.store.put_thread(thread)
            if reassign and work.get("result") is not None:
                self._reassign(session, cause="permit_check" if reason else "permit_stream")
        if ask and thread.status == "open":
            message = make_notice(
                kind="question",
                thread_id=thread.id,
                sender=thread.emitter_org,
                recipient=thread.vendor_org,
                text=f"허가 품목이 맞지 않는 폐기물({names})은 이 대화에서 뺍니다. "
                "나머지 폐기물을 받을 수 있는지와 조건을 다시 알려 주세요.",
                stage=4,
                stream_ids=tuple(left),
                evidence=evidence,
                excluded=[{"streamId": sid, "reason": PERMIT_EXCLUDED} for sid in failed],
                asks=[{"streamId": sid, "field": "terms"} for sid in left],
            )
            try:
                thread.check(message)
            except ThreadError:  # 턴 상한에 닿았으면 담당자에게 넘긴다
                self._handoff(
                    session,
                    thread,
                    thread.emitter_org,
                    "주고받을 수 있는 횟수를 다 써서 담당자 확인이 필요합니다.",
                    reassign=reassign,
                )
                return "closed"
            self._append(session, thread, message, "company_agent")
        return "excluded"

    def _reassign(self, session: dict[str, Any], cause: str = "handoff") -> None:
        """합의한 대화가 담당자에게 넘어간 뒤: 그 업체를 빼고 최적안과 결과 한 장을 다시 만든다.

        - 넘어간 대화의 업체는 후보에서 뺀다. 이미 합의했거나 확정한 다른 대화의 배정은 그대로 둔다.
        - 풀려난 흐름을 받을 다른 제안이 있으면 그 대화가 새로 합의 상태가 되고(양쪽 승인을 새로 받는다),
          없으면 그 흐름은 결과 한 장에서 "확인 필요"로 나간다.
        - 승인을 기다리는 대화가 하나도 남지 않으면 진단은 승인 대기에서 벗어난다.
        """
        work, org_id = session["work"], session["emitterOrgId"]
        streams = self.store.list_streams(org_id)
        profiles = {p["orgId"]: p for p in self.store.list_vendor_profiles()}
        live = [
            t
            for t in self._threads(session["id"])
            if t.status in ("open", "agreed", "confirmed") and work["offers"].get(t.id)
        ]
        held = {
            sid: t.vendor_org
            for t in live
            if t.status in ("agreed", "confirmed")
            for sid in work["assigned"].get(t.id) or []
        }
        exclude = []
        for thread in live:
            for stream in streams:
                sid = stream["id"]
                keeps = held.get(sid) == thread.vendor_org
                if (
                    sid not in work["offers"][thread.id]
                    or (sid in held and not keeps)  # 이미 합의한 배정은 옮기지 않는다
                    or (thread.status == "confirmed" and not keeps)  # 확정한 대화에는 더하지 않는다
                ):
                    exclude.append({"streamId": sid, "vendorOrgId": thread.vendor_org})
        vendors = [self._assign_vendor(session, profiles[t.vendor_org]) for t in live]
        arguments: dict[str, Any] = {"streams": streams, "vendors": vendors}
        if exclude:
            arguments["exclude"] = exclude
        self._with_origin(session, arguments)
        optimized = self._tool(
            session,
            "optimize_assignment",
            arguments,
            summary_ids={
                "streamIds": [s["id"] for s in streams],
                "vendorOrgIds": [v["orgId"] for v in vendors],
                "reason": cause
                if cause in ("permit", "permit_stream", "terms", "permit_check")
                else "handoff",
            },
        )
        work["optimize"] = optimized
        thread_of = {t.vendor_org: t for t in live}
        chosen: dict[str, list[str]] = {}
        for item in optimized["output"].get("assignments") or []:
            thread = thread_of.get(item.get("vendorOrgId"))
            if thread is not None:
                chosen.setdefault(thread.id, []).append(item["streamId"])
        for thread in live:
            old = list(work["assigned"].get(thread.id) or [])
            new = list(dict.fromkeys([*old, *chosen.get(thread.id, [])]))
            if thread.status == "confirmed" or not new:
                continue
            if thread.status == "open":
                thread.mark_agreed()
                self.store.put_thread(thread)
                self.store.block_approval(session["id"], thread.id, None)
                self._event(
                    session["id"],
                    "system",
                    "thread",
                    "최적안을 다시 계산했습니다. "
                    "이 조건이 최적안에 들어갔고 양쪽 담당자의 승인을 기다립니다.",
                    thread.id,
                    status="agreed",
                    streamIds=new,
                    agreedSeq=thread.agreed_seq,
                )
            elif sorted(new) != sorted(old):
                # 배정된 흐름이 늘었다. 앞서 한 승인은 이전 조건에 대한 것이므로 다시 받는다
                for side in ("emitter", "vendor"):
                    thread.approvals[side] = False
                    self.store.set_approval(session["id"], thread.id, side, False)
                self.store.put_thread(thread)
                self._event(
                    session["id"],
                    "system",
                    "thread",
                    "최적안을 다시 계산해 이 대화에 배정된 폐기물이 바뀌었습니다. 승인을 다시 받습니다.",
                    thread.id,
                    status="agreed",
                    streamIds=new,
                )
            work["assigned"][thread.id] = new
            if sorted(new) != sorted(old):
                self._remember(session, thread)
                self._loading_for(session, thread, new, streams, profiles)
                self._verify(session, thread)
        self._assigned_requirements(session, streams)
        work["result"] = self._result(session, streams)
        self._announce_reviews(session)
        if cause != "terms":
            work["stages"]["4"] = "needs_check"
        work["stages"]["6"] = (
            "needs_check" if work["result"]["status"] == "needs_information" else "done"
        )
        pending = [t for t in self._threads(session["id"]) if t.id in work["assigned"]]
        if pending and all(t.status == "confirmed" for t in pending):
            session["status"], session["phase"] = "confirmed", "done"
        elif not pending:
            # 승인할 대화가 남지 않았다. 합의 없이 끝난 진단과 같은 상태로 둔다
            session["status"], session["phase"] = "idle", "done"
        else:
            session["status"], session["phase"] = "awaiting_approval", "approval"
        self._event(
            session["id"],
            "system",
            "stage",
            {
                "permit": "합의한 대화가 허가 품목 불일치로 닫혀 그 업체를 빼고 "
                "최적안을 다시 계산했습니다.",
                "permit_stream": "허가 품목이 맞지 않게 된 폐기물을 그 업체의 배정에서 빼고 "
                "최적안을 다시 계산했습니다.",
                "terms": "업체의 조건이 바뀐 대화를 새 조건으로 이어 간 뒤 최적안을 다시 계산했습니다.",
                "permit_check": "허가 확인 상태(허가증 업로드, 담당자의 원문 대조)가 바뀌어 "
                "최적안을 다시 계산했습니다.",
            }.get(
                cause, "합의한 대화가 담당자에게 넘어가 그 업체를 빼고 최적안을 다시 계산했습니다."
            ),
            status=session["status"],
            resultStatus=work["result"]["status"],
            orgId=org_id,
        )

    def _loading_for(self, session, thread: Thread, assigned, streams, profiles) -> None:
        """실제로 배정된 흐름만 합쳐 최소 물량과 적재를 검사한다(결과 한 장의 checks, 이행 계획서)."""
        by_id = {s["id"]: s for s in streams}
        _, trace = self._company(session).run_tools(
            [("check_loading", {"streamIds": sorted(assigned)})],
            streams=[by_id[s] for s in thread.stream_ids if s in by_id],
            vendor=self._public(session, profiles[thread.vendor_org]),
            memo=session["work"]["threadTools"].setdefault(thread.id, []),
        )
        self._flush(session["id"], trace, thread.id, session["emitterOrgId"])

    def _send(self, session, thread: Thread, outcome: Outcome, trace: Trace, *, stage: int) -> None:
        """회사 측이 쓴 결과를 스레드에 붙인다. 두 번 거부됐으면 handoff로 넘긴다."""
        self._flush(session["id"], trace, thread.id, thread.emitter_org)
        if outcome.kind == "message" and outcome.message is not None:
            self._append(session, thread, outcome.message, "company_agent")
            return
        # 거부 코드는 문장에 넣지 않는다(handoff 메시지의 meta.validation과 실행 기록 detail에 있다)
        reason = "회사 측 AI가 근거를 갖춘 메시지를 만들지 못해 담당자 확인이 필요합니다."
        self._handoff(session, thread, thread.emitter_org, reason, stage=stage, outcome=outcome)

    def _vendor_context(self, session, thread: Thread) -> dict[str, Any]:
        emitter = self.store.get_org(thread.emitter_org)
        streams = []
        for sid in thread.stream_ids:
            # 현행 계약 단가, 재투입 입력, 전처리 인건비는 업체에 열지 않는다
            streams.append(without_emitter_only(self.store.get_stream(sid)))
        return {
            "sessionId": session["id"],
            "today": self.today,
            "thread": thread.to_dict(),
            "streams": streams,
            "emitter": {
                "id": emitter["id"],
                "name": emitter.get("name"),
                "location": emitter.get("location"),
                # 업체 측이 자기 노선이 이 공장 지역을 지나는지 보고 수거·운반 견적을 낸다(judge/pickup.py)
                "region": emitter.get("region"),
            },
        }

    def _receive(self, session, thread: Thread, reply: dict[str, Any], turn: Any = None) -> None:
        """업체 측 답을 회사 측에서 다시 검사한 뒤에만 스레드에 붙인다.

        turn은 이 차례의 실시간 알림(live.Turn)이다. 업체 측에서 알림이 하나도 오지 않았으면(다른
        프로세스) 업체 측이 보낸 기록으로 거부와 다시 쓰기를 진행 기록에 채운다.
        """
        work = session["work"]
        raw_meta = reply.get("meta") if isinstance(reply.get("meta"), dict) else {}
        reported = reported_trace(raw_meta.get("trace"), self.registry)
        self._flush(session["id"], reported, thread.id, thread.vendor_org)
        if turn is not None and not turn.steps:
            turn.replay(reported)
        stage = reply.get("stage") if reply.get("stage") in STAGE_NUMBERS else 5
        self.feed.recheck(session["id"], thread, None, stage)
        meta = self._clean_meta(raw_meta, thread)
        reported = meta.get("validation") or {}
        problems: list[dict[str, Any]] = []
        message: Message | None = None
        try:
            # 상대가 보낸 claim의 이름(label)과 구실(role)은 믿지 않고 코드가 다시 정한다
            claims = [naming.trusted(c) for c in reply.get("claims") or [] if isinstance(c, dict)]
            message = Message.from_dict(
                {**reply, "claims": claims, "meta": meta, "seq": 0, "createdAt": ""}
            )
            if message.sender != thread.vendor_org or message.thread_id != thread.id:
                raise ThreadError("보낸 조직이나 스레드가 이 대화와 다르다")
            thread.check(message)
        except (ThreadError, KeyError, TypeError, ValueError) as error:
            problems.append(
                {"code": "THREAD_RULE", "message": f"대화 규칙에 맞지 않습니다: {error}"}
            )
        if message is not None and not problems:
            result = validate_message(
                message, self.registry, today=self.today, names=thread_names(self.store, thread)
            )
            problems += [r.to_dict() for r in result.rejections]
            problems += [
                {"code": p["code"], "message": p["message"]}
                for p in check_disclosure(message, self.registry, self.store)
            ]
            # 숫자 없이 조건을 뜻하는 낱말도 다시 본다(업체 측이 검사를 건너뛰었을 때)
            problems += [
                {"code": v.code, "message": v.message}
                for v in rails.check_term_words(
                    message, meta.get("terms"), thread_names(self.store, thread)
                )
            ]
        self.feed.recheck(session["id"], thread, [str(p["code"]) for p in problems], stage)
        self._event(
            session["id"],
            "validator",
            "validation",
            "받은 답을 회사 측에서 근거와 다시 대조: " + ("통과" if not problems else "돌려보냄"),
            thread.id,
            ok=not problems,
            recheckedBy=thread.emitter_org,
            codes=[p["code"] for p in problems],
            type=reply.get("type"),
        )
        if problems or message is None:
            self._handoff(
                session,
                thread,
                thread.emitter_org,
                "업체 측 답이 회사 측 근거 확인을 통과하지 못해 담당자 확인이 필요합니다.",
                stage=5,
            )
            return
        if message.type in TERMS_TYPES:
            # 조건을 낸 흐름의 허가 품목을 지금 프로필로 다시 대조한다. 맞지 않으면 그 답은 붙이지 않고
            # 근거와 함께 닫는다(업체 측이 대조를 건너뛰었거나 보낸 뒤에 허가 품목이 바뀐 경우)
            wanted = [
                str(item.get("streamId"))
                for item in (meta.get("terms") or {}).get("items") or []
                if item.get("streamId") in thread.stream_ids
            ]
            failed, evidence = self._permit_failures(session, thread, list(dict.fromkeys(wanted)))
            if failed:
                # 허가가 맞는 흐름이 남아 있으면 대화를 닫지 않는다. 그 흐름만 빼고 조건을 다시 묻는다
                self._drop_for_permit(session, thread, failed, evidence, ask=True)
                return
        message.meta["validation"] = {
            "ok": True,
            "attempts": reported.get("attempts") or 1,
            "rejections": list(reported.get("rejections") or []),
            "recheckedBy": thread.emitter_org,
        }
        if meta.get("model") and meta["model"].get("provider") != "code":
            # 실제로 답을 쓴 구현만 적는다. 코드가 쓴 handoff·decline은 모델이 쓴 답이 아니다
            work["vendorModels"][thread.vendor_org] = meta["model"]
        claims = {c.id: c for c in message.claims}
        for item in (message.meta.get("terms") or {}).get("items") or []:
            # 조건의 단가는 업체가 적어 보낸 숫자가 아니라 검증된 claim에서 다시 읽어 적는다
            claim = claims.get(item["claimId"])
            try:
                item["unitPriceKrwPerKg"] = convert(float(claim.value), claim.unit, "원/kg")
            except (AttributeError, UnitError, TypeError, ValueError):
                item["unitPriceKrwPerKg"] = None
        self._append(session, thread, message, "vendor_agent")
        if thread.closed:
            # 업체 측이 닫았다(decline, handoff). 다시 협상하던 대화였으면 앞서 합의한 배정이 풀린다
            self._release(session, thread, reassign=True, cause="handoff")
        if message.type in TERMS_TYPES:
            terms = meta.get("terms") or {}
            offers = work["offers"].setdefault(thread.id, {})
            for item in terms.get("items") or []:
                claim = claims.get(str(item.get("claimId")))
                sid = item.get("streamId")
                if claim is None or sid not in thread.stream_ids:
                    continue
                try:  # 값은 업체가 적은 숫자가 아니라 검증된 claim에서 읽는다
                    price = convert(float(claim.value), claim.unit, "원/kg")
                except (UnitError, TypeError, ValueError):
                    continue
                offers[sid] = {
                    "seq": message.seq,
                    "type": message.type,
                    "claim": claim.to_dict(),
                    "unitPriceKrwPerKg": price,
                    "days": list(terms.get("days") or []),
                    "conditions": [c for c in terms.get("conditions") or [] if isinstance(c, str)],
                }
            # 업체가 밝힌 수거 상태와 운반 견적(금액·단위·횟수는 검증된 claim에서 읽는다). 업체마다 마지막 답을 쓴다
            answered = haul_answer(terms.get("pickup"), claims, message.seq)
            if answered is not None:
                work.setdefault("pickups", {})[thread.vendor_org] = answered
        for item in meta.get("excluded") or []:
            if isinstance(item, dict) and item.get("streamId") in thread.stream_ids:
                entry = {
                    "streamId": item["streamId"],
                    "vendorOrgId": thread.vendor_org,
                    "reason": str(item.get("reason") or "업체가 받지 않는다고 답했습니다."),
                    "stage": 5,
                }
                if entry not in work["excluded"]:
                    work["excluded"].append(entry)

    @staticmethod
    def _clean_meta(raw: dict[str, Any], thread: Thread) -> dict[str, Any]:
        """업체 측이 보낸 meta에서 정해 둔 항목만 정해 둔 모양으로 남긴다.

        상대가 보낸 것은 믿지 않는다. 남긴 문장(이유, 조건)은 근거 검증기가 본문과 같이 검사한다.
        """
        meta: dict[str, Any] = {}
        reported = raw.get("validation") if isinstance(raw.get("validation"), dict) else {}
        attempts = reported.get("attempts")
        meta["validation"] = {
            "attempts": attempts
            if isinstance(attempts, int) and not isinstance(attempts, bool)
            else 1,
            "rejections": public_rejections(
                reported.get("rejections") if isinstance(reported.get("rejections"), list) else []
            ),
        }
        model = raw.get("model")
        if isinstance(model, dict) and model.get("provider"):
            meta["model"] = {"provider": str(model["provider"]), "name": str(model.get("name"))}
        if isinstance(raw.get("reason"), str) and raw["reason"].strip():
            meta["reason"] = raw["reason"].strip()
        if isinstance(raw.get("reasonEvidence"), list):
            meta["reasonEvidence"] = [e for e in raw["reasonEvidence"] if isinstance(e, str)]
        asks = questions.clean_asks(raw.get("asks"))
        if asks:
            meta["asks"] = asks
        excluded = [
            {"streamId": x["streamId"], "reason": str(x.get("reason") or "").strip()}
            for x in (raw.get("excluded") if isinstance(raw.get("excluded"), list) else [])
            if isinstance(x, dict) and x.get("streamId") in thread.stream_ids
        ]
        if excluded:
            meta["excluded"] = excluded
        terms = raw.get("terms")
        if isinstance(terms, dict):
            meta["terms"] = {
                "items": [
                    {"streamId": str(i.get("streamId")), "claimId": str(i.get("claimId"))}
                    for i in (terms.get("items") if isinstance(terms.get("items"), list) else [])
                    if isinstance(i, dict)
                ],
                "days": [
                    d
                    for d in (terms.get("days") if isinstance(terms.get("days"), list) else [])
                    if d in WEEKDAYS
                ],
                "conditions": [
                    c.strip()
                    for c in (
                        terms.get("conditions") if isinstance(terms.get("conditions"), list) else []
                    )
                    if isinstance(c, str) and c.strip()
                ],
            }
            pickups = terms.get("pickupsPerMonth")
            if isinstance(pickups, (int, float)) and not isinstance(pickups, bool):
                meta["terms"]["pickupsPerMonth"] = pickups  # 화면에는 나가지 않는다(값은 claims에)
            haul = clean_haul(terms.get("pickup"))
            if haul is not None:
                meta["terms"]["pickup"] = haul
        return meta

    # ── ④~⑤ 대화 ──────────────────────────────────────────────────────
    def _converse(self, session: dict[str, Any]) -> bool:
        work = session["work"]
        state = work["threadState"]
        agent = self._company(session)
        profiles = {p["orgId"]: p for p in self.store.list_vendor_profiles()}
        for _ in range(MAX_STEPS):
            moved = False
            for thread in self._threads(session["id"]):
                current = state.get(thread.id)
                if thread.status != "open" or current in (None, "settled", "closed"):
                    continue
                if current == "waiting_user":
                    if self._open_questions(session["id"]):
                        continue
                    for sid in thread.stream_ids:
                        self._share_stream(self.store.get_stream(sid), thread.vendor_org)
                    state[thread.id] = current = "awaiting_company"
                if thread.turn_limit_reached:
                    self._handoff(
                        session,
                        thread,
                        thread.emitter_org,
                        "주고받을 수 있는 횟수를 다 써서 담당자 확인이 필요합니다.",
                        stage=5,
                    )
                    moved = True
                elif current == "awaiting_vendor":
                    self._ask_vendor(session, thread)
                    moved = True
                elif current == "awaiting_company":
                    self._company_turn(session, thread, agent, profiles)
                    moved = True
                self._save(session)
            if not moved:
                break
        opened = [t for t in self._threads(session["id"]) if not t.blocked]
        if any(t.status == "handoff" for t in opened) or not opened:
            work["stages"]["4"] = "needs_check"
        else:
            work["stages"]["4"] = "done"
        return bool(self._open_questions(session["id"]))

    def _ask_vendor(self, session, thread: Thread) -> None:
        last = thread.messages[-1]
        try:
            with self.feed.turn(session["id"], thread, "vendor", last.stage) as turn:
                turn.announce()
                reply = self.app.vendor_gateway.deliver(
                    thread.vendor_org,
                    thread.to_dict()["messages"][-1] | {"threadId": thread.id, "seq": last.seq},
                    self._vendor_context(session, thread),
                )
        except Exception as error:  # noqa: BLE001 - 업체 측 장애는 종류와 관계없이 사람에게 넘긴다
            self._event(
                session["id"],
                "system",
                "handoff",
                "업체 측 AI와 연결하지 못했습니다.",
                thread.id,
                error=type(error).__name__,
            )
            self._handoff(
                session,
                thread,
                thread.emitter_org,
                "업체 측 AI와 연결이 원활하지 않아 담당자 확인이 필요합니다.",
                stage=4,
            )
            return
        self._receive(session, thread, reply, turn)

    def _company_turn(self, session, thread: Thread, agent: CompanyAgent, profiles) -> None:
        work = session["work"]
        streams = [self.store.get_stream(sid) for sid in thread.stream_ids]
        offered = sorted(work["offers"].get(thread.id) or {})
        excluded = sorted(
            {
                x["streamId"]
                for x in work["excluded"]
                if x["vendorOrgId"] == thread.vendor_org and x["streamId"] in thread.stream_ids
            }
        )
        # 질문 한도(묶음당 세 개, 두 묶음)를 다 썼으면 처음부터 담당자에게 물을 수 없다고 알린다. 그러면
        # 회사 측은 ask_user를 고를 수 없고 모르는 값을 '확인 필요'로 둔 채 조건부 제안으로 나아간다
        history = self.store.list_questions(session["id"])
        exhausted = self.policy.batches_used(history) >= self.policy.max_batches
        with self.feed.turn(session["id"], thread, "company", 5):
            outcome, trace = agent.compose(
                thread,
                "reply",
                streams=streams,
                vendor=self._public(session, profiles[thread.vendor_org]),
                vendor_org=self.store.get_org(thread.vendor_org),
                memo=work["threadTools"].setdefault(thread.id, []),
                state={
                    "offered": offered,
                    "excluded": excluded,
                    "cannotAskUser": exhausted or bool(work["cannotAsk"].get(thread.id)),
                    "unresolved": list(work.get("unresolved") or []),
                },
            )
        if outcome.kind == ACCEPT:
            self._flush(session["id"], trace, thread.id, thread.emitter_org)
            work["threadState"][thread.id] = "settled"
            self.feed.note(
                session["id"],
                "회사 측 AI가 받은 조건으로 최적안 계산에 넘깁니다.",
                stage=5,
                thread=thread,
                org_id=thread.emitter_org,
            )
            return
        if outcome.kind == ASK_USER:
            self._flush(session["id"], trace, thread.id, thread.emitter_org)
            by_id = {s["id"]: s for s in streams}
            wanted = []
            for item in outcome.draft.get("userQuestions") or []:
                stream = by_id.get(str((item or {}).get("streamId")))
                field = (item or {}).get("field")
                if stream is None or field not in questions.FIELDS:
                    continue  # 모델은 물을 항목을 새로 만들 수 없다
                if not questions.known(stream, field):
                    wanted.append(questions.make_question(stream, field, asked_by=thread.id))
            batch = self.policy.next_batch(questions.ordered(wanted), history)
            if batch:
                self._ask(session, batch, stage=5)
                work["threadState"][thread.id] = "waiting_user"
                self.feed.note(
                    session["id"],
                    "회사 측 AI가 담당자에게 물어본 뒤 이어서 답합니다.",
                    stage=5,
                    thread=thread,
                    org_id=thread.emitter_org,
                )
            else:
                # 이미 물은 값이거나 답을 기다리는 묶음이 있다. 확인 필요로 두고 다음 차례에 답하게 한다.
                # 다음 차례의 compose는 ask_user를 받지 않으므로 다시 묻다가 담당자에게 넘어가지 않는다
                work["cannotAsk"][thread.id] = True
                for question in wanted:
                    entry = {"streamId": question["streamId"], "field": question["field"]}
                    if entry not in work["unresolved"]:
                        work["unresolved"].append(entry)
            return
        self._send(session, thread, outcome, trace, stage=5)

    # ── ⑤⑥ 비용·탄소 비교와 최적안 ────────────────────────────────────
    def _assign_vendor(self, session: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        """최적 배정과 법령 요건에 넘기는 업체 프로필. 허가 확인 상태를 가르도록 업체 이름, 업체가 올린 허가증,
        이 진단의 배출 기업 담당자가 남긴 대조 기록(다른 배출 기업의 기록은 넣지 않는다)을 더한다."""
        shown = self._public(session, profile)
        try:
            org = self.store.get_org(str(profile.get("orgId")))
        except NotFound:
            return shown
        shown = {**shown, **permits.context(profile, org, session.get("emitterOrgId"))}
        name = org_label(org.get("name"))
        return {**shown, "name": name} if name else shown

    # ── 허가 확인 상태(업체 입력·허가증 업로드·담당자의 원문 대조) ───────────────
    def _seen(self, evidence_ids, org_id: str) -> list[str]:
        """org_id가 볼 수 있는 근거 ID만."""
        out: list[str] = []
        for evidence_id in evidence_ids or []:
            try:
                self.registry.get(evidence_id, org_id)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
            out.append(evidence_id)
        return list(dict.fromkeys(out))

    def _permit_of(self, session, stream: dict[str, Any], vendor_id: str | None) -> dict | None:
        """vendor_id로 이 폐기물을 보낼 때의 지금 허가 확인 상태(화면용). 그 업체 자신의 근거로만 판정한다."""
        if not vendor_id:
            return None
        try:
            profile = self.store.get_vendor_profile(vendor_id)
        except NotFound:
            return None
        vendor = self._assign_vendor(session, profile)
        view = permits.short_view(permits.permit_level(self.registry, stream, vendor, self.today))
        if view is not None:
            view["evidence"] = self._seen(view["evidence"], session["emitterOrgId"])
        return view

    def _assigned_requirements(self, session, streams: list[dict[str, Any]]) -> None:
        """배정이 정해진 뒤 폐기물마다 배정한 업체 하나와 그 경로로 법령 요건을 다시 판정한다.

        ③에서는 대화를 연 업체 모두로 허가 요건을 봤다. 결과에는 실제로 받는 업체의 허가 확인 상태만 들어가야
        한다(다른 후보의 허가증으로 배정 업체의 상태를 채우지 않는다). 업체에 배정하지 않은 폐기물은 지금 방식
        그대로(keep)로 보고, 공정 재투입은 따로 낸 요건(reuseRequirements)을 그대로 둔다.
        """
        work = session["work"]
        by_id = {s["id"]: s for s in streams}
        profiles = {p["orgId"]: p for p in self.store.list_vendor_profiles()}
        for item in (work.get("optimize") or {}).get("output", {}).get("assignments") or []:
            stream = by_id.get(item["streamId"])
            if stream is None or item.get("route") == "reuse":
                continue
            vendor_id = item.get("vendorOrgId")
            route = item.get("route") or "keep"
            arguments: dict[str, Any] = {"stream": stream, "route": route}
            if vendor_id and vendor_id in profiles:
                arguments["vendors"] = [self._assign_vendor(session, profiles[vendor_id])]
            work["requirements"][stream["id"]] = self._tool(
                session,
                "check_requirements",
                arguments,
                summary_ids={"streamId": stream["id"], "route": route, "vendorOrgId": vendor_id},
            )

    def _blocked_routes(self, session, stream, assigned: str | None, names) -> list[dict]:
        """이 폐기물로 대화한 업체 가운데 허가 확인 결과 이 경로로 진행할 수 없는 곳(사유 포함)."""
        out, seen = [], set()
        for thread in self._threads(session["id"]):
            vendor = thread.vendor_org
            if vendor == assigned or vendor in seen or stream["id"] not in thread.stream_ids:
                continue
            seen.add(vendor)
            permit = self._permit_of(session, stream, vendor)
            if permit and permit["level"] == "mismatch":
                out.append(
                    {
                        "vendorOrgId": vendor,
                        "vendorName": names.get(vendor),
                        "permit": permit,
                        "note": permit["text"],
                    }
                )
        return out

    def permit_states(self, session, thread: Thread) -> list[dict[str, Any]]:
        """이 대화에 배정된 폐기물마다 지금의 허가 확인 상태. [{streamId, level, label, status, ...}]"""
        out = []
        for sid in sorted(session["work"]["assigned"].get(thread.id) or []):
            try:
                stream = self.store.get_stream(sid)
            except NotFound:
                continue
            view = self._permit_of(session, stream, thread.vendor_org)
            if view is not None:
                out.append({"streamId": sid, **view})
        return out

    def _hold_reason(self, held: list[dict[str, Any]], vendor_name: str) -> str:
        parts = []
        for item in held:
            try:
                label = stream_label(self.store.get_stream(item["streamId"]))
            except NotFound:
                label = "폐기물"
            parts.append(f"{label} → {vendor_name}({item['label']})")
        return (
            "허가증 원문 대조가 끝나지 않은 경로가 있어 확정할 수 없습니다: "
            + ", ".join(parts)
            + ". 배출 기업 담당자가 결과표에서 업체가 올린 허가증 원문의 품목과 처리 방법을 대조해 기록하면 "
            "승인할 수 있습니다."
        )

    def refresh_permits(self, session_id: str) -> bool:
        """승인을 기다리는 진단의 허가 확인 상태를 지금 자료로 다시 본다. 바뀌었으면 True.

        원문 대조 결과 진행할 수 없게 된 배정은 그 대화에서 빼고 다시 배정한다(_drop_for_permit). 상태만 바뀌었으면
        (허가증 업로드, 대조 완료) 최적안과 결과 한 장을 다시 계산해 결과표·승인 카드가 같은 상태를 보이게 한다.
        """
        session = self.store.get_session(session_id)
        work = session["work"]
        if session.get("status") != "awaiting_approval" or work.get("result") is None:
            return False
        dropped = False
        for thread in self._threads(session_id):
            if thread.status != "agreed":
                continue
            failed = [p for p in self.permit_states(session, thread) if p["level"] == "mismatch"]
            if failed:
                evidence = list(dict.fromkeys(e for p in failed for e in p["evidence"]))
                self._drop_for_permit(
                    session,
                    thread,
                    [p["streamId"] for p in failed],
                    evidence,
                    reason="허가증 원문 대조 결과 이 폐기물이나 처리 방식이 업체의 허가 내용과 맞지 않아 "
                    "근거와 함께 대화를 닫습니다.",
                    recheck=False,
                )
                dropped = True
        if dropped:
            self._save(session)
            return True
        streams = {s["id"]: s for s in self.store.list_streams(session["emitterOrgId"])}

        def mark(permit: dict | None) -> tuple:
            permit = permit or {}
            return (
                permit.get("level"),
                permit.get("source"),
                (permit.get("check") or {}).get("id"),
                (permit.get("document") or {}).get("id"),
                tuple(sorted(str(x.get("name")) for x in permit.get("ignored") or [])),
            )

        changed = False
        for item in work["result"].get("assignments") or []:
            stream = streams.get(item["streamId"])
            if stream is None:
                continue
            shown = [(item.get("vendorOrgId"), item.get("permit"))]
            shown += [(a["vendorOrgId"], a.get("permit")) for a in item.get("alternatives") or []]
            shown += [(b["vendorOrgId"], b.get("permit")) for b in item.get("blockedRoutes") or []]
            for vendor_id, permit in shown:
                if vendor_id and mark(permit) != mark(self._permit_of(session, stream, vendor_id)):
                    changed = True
        if changed:
            self._reassign(session, cause="permit_check")
            self._save(session)
        return changed

    def record_permit_check(
        self, session_id: str, org_id: str, body: dict[str, Any], checker: dict[str, Any]
    ) -> dict[str, Any]:
        """배출 기업 담당자가 업체가 올린 허가증 원문에서 이 폐기물의 품목과 처리 방법을 대조한 기록을 남긴다.

        body: {streamId, vendorOrgId, documentId, page, itemFound, processFits, itemQuote?, processQuote?}.
        그 업체가 올린, 상호·소재지가 맞는 허가증만 받는다. 기록은 업체 프로필의 permitChecks에 쌓이고(이 배출
        기업의 판단에만 쓰인다), human 근거로 남는다. 그 뒤 허가 확인 상태를 다시 본다(refresh_permits).
        """
        session = self.store.get_session(session_id)
        if session["emitterOrgId"] != org_id:
            raise OrchestratorError(
                "PERMIT_CHECK_FORBIDDEN",
                "허가증 원문 대조는 이 진단의 배출 기업 담당자만 기록할 수 있습니다.",
                403,
            )
        if session.get("status") != "awaiting_approval":
            raise OrchestratorError(
                "NOT_AWAITING_APPROVAL",
                "허가증 원문 대조는 승인을 기다리는 진단에서 기록합니다.",
            )
        sid = str(body.get("streamId") or "")
        vendor_id = str(body.get("vendorOrgId") or "")
        threads = [
            t
            for t in self._threads(session_id)
            if t.vendor_org == vendor_id and sid in t.stream_ids
        ]
        thread = next((t for t in threads if t.status in ("open", "agreed")), None)
        if thread is None:
            if any(t.status == "confirmed" for t in threads):
                raise OrchestratorError("ALREADY_CONFIRMED", "이미 확정한 경로입니다.")
            raise OrchestratorError(
                "ROUTE_NOT_FOUND", "이 진단에서 그 업체와 이 폐기물로 대화한 경로가 없습니다.", 404
            )
        stream = self.store.get_stream(sid)
        try:
            profile = self.store.get_vendor_profile(vendor_id)
        except NotFound:
            raise OrchestratorError("ROUTE_NOT_FOUND", "그런 업체가 없습니다.", 404) from None
        vendor = self._assign_vendor(session, profile)
        if permits.matched_permit(stream, vendor) is None:
            raise OrchestratorError(
                "PERMIT_NOT_MATCHED", "업체의 허가 품목에 이 폐기물이 없어 대조할 경로가 아닙니다."
            )
        usable, ignored = permits.usable_documents(vendor)
        document_id = body.get("documentId")
        document = next((d for d in usable if d.get("id") == document_id), None)
        if document is None:
            known = [d for d in profile.get("permitDocuments") or [] if d.get("id") == document_id]
            if known:
                why = next(
                    (x["reason"] for x in ignored if x.get("name") == known[0].get("name")),
                    "이 업체의 허가증으로 쓸 수 없는 문서입니다.",
                )
                raise OrchestratorError("PERMIT_DOCUMENT_IGNORED", why)
            raise OrchestratorError(
                "PERMIT_DOCUMENT_NOT_FOUND",
                "그 업체가 올린 허가증이 아닙니다. 업체가 올린 허가증 가운데에서 고르세요.",
                404,
            )
        page = body.get("page")
        pages = [int(p) for p in document.get("pages") or [] if isinstance(p, int)]
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise OrchestratorError("INVALID_PAGE", "대조한 쪽을 1 이상의 정수로 적어 주세요.", 422)
        if pages and page not in pages:
            raise OrchestratorError(
                "INVALID_PAGE", f"허가증에 없는 쪽입니다(1~{max(pages)}쪽).", 422
            )
        found, fits = body.get("itemFound"), body.get("processFits")
        if not isinstance(found, bool) or not isinstance(fits, bool):
            raise OrchestratorError(
                "INVALID_VALUE",
                "품목이 있는지(itemFound)와 처리 방법이 맞는지(processFits)를 참·거짓으로 보내 주세요.",
                422,
            )

        def quote(key: str) -> str | None:
            text = " ".join(str(body.get(key) or "").split())[:200]
            return text or None

        process = permits.process_of(vendor)
        material = stream.get("material")
        at = self.store.now()
        record = {
            "vendorOrgId": vendor_id,
            "streamId": sid,
            "sessionId": session_id,
            "documentId": document["id"],
            "document": document.get("name"),
            "page": page,
            "wasteItem": stream.get("wasteItem"),
            "materials": [material] if material else [],
            "process": process,
            "itemFound": found,
            "processFits": fits,
            "itemQuote": quote("itemQuote"),
            "processQuote": quote("processQuote"),
            "checkedBy": dict(checker),
            "checkedAt": at,
        }
        record["id"] = f"pc_{ids.digest(record)}"
        vendor_name = org_label(self.store.get_org(vendor_id).get("name"), "업체")
        scope = permits.scope_of(stream)
        need = permits.needed_business(vendor)
        lines = [
            f"{document.get('name')} {page}쪽 원문 대조",
            f"품목 {scope}: {'있음' if found else '없음'}"
            + (f" (원문: {record['itemQuote']})" if record["itemQuote"] else ""),
            f"처리 방식 {process}({need}업): {'맞음' if fits else '다름'}"
            + (f" (원문: {record['processQuote']})" if record["processQuote"] else ""),
        ]
        who = f"{checker.get('orgName') or '배출 기업'} 담당자"
        # 계정 이름은 이 배출 기업만 보는 근거의 출처에만 남긴다(진행 기록은 업체도 본다)
        signed = f"{who} {checker['userName']}" if checker.get("userName") else who
        evidence = self.registry.put(
            Evidence(
                id=ids.human_id(org_id, f"허가증대조-{vendor_id}-{record['id']}"),
                kind="human",
                label=f"허가증 원문 대조: {vendor_name} · {scope} · {process}",
                value="맞음" if found and fits else "맞지 않음",
                excerpt="\n".join(lines),
                source=f"{signed} 확인({at[:10]})",
                as_of=self.today,
                # 배출 기업의 기록이다(담당자 이름이 들어 있어 업체에는 열지 않는다)
                access=(org_id,),
                meta={
                    "ownerOrgId": org_id,
                    "permitCheck": record["id"],
                    "document": document.get("name"),
                    "documentId": document["id"],
                    "page": page,
                    "checkedAt": at,
                },
            )
        )
        record["evidence"] = evidence.id
        for evidence_id in document.get("evidence") or []:
            try:
                share_evidence(self.registry, evidence_id, org_id)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
        stored = dict(profile)
        stored["permitChecks"] = [*(profile.get("permitChecks") or []), record]
        self.store.put_vendor_profile(stored)
        self._event(
            session_id,
            "system",
            "thread",
            f"{josa(who, '이가')} {vendor_name}의 허가증 원문에서 {josa(scope, '와과')} "
            f"{josa(f'처리 방식 {process}', '을를')} 대조했습니다: "
            + ("맞음." if found and fits else "맞지 않음."),
            thread.id,
            vendorOrgId=vendor_id,
            streamIds=[sid],
            permitCheck=record["id"],
            orgId=org_id,
        )
        self.refresh_permits(session_id)
        return self.store.get_session(session_id)

    def _with_origin(self, session: dict[str, Any], arguments: dict[str, Any]) -> None:
        """공장 좌표가 있으면 최적 배정에 넘긴다(수거 운행 배출을 계산한다)."""
        try:
            location = self.store.get_org(session["emitterOrgId"]).get("location")
        except NotFound:
            return
        if location:
            arguments["origin"] = list(location)

    def _public(self, session: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        """판단 도구에 넘기는 업체 프로필. 이 진단에서 협상한 단가가 있으면 그 매입 기준의 단가와 근거를 바꾼다.

        협상 단가는 업체 측이 낸 계산 근거(negotiate_reply)이고, 그 기준의 나머지 근거(최소 물량, 상한)는 그대로다.
        """
        shown = public_profile(profile, fleet_cost=False)
        # 운반 조건은 이 공장에 적용되는 것(업체가 조건 답에 밝힌 수거 상태와 견적, 답이 없으면 프로필에서 정한 것)이다
        shown["transport"] = pickup_terms.tool_transport(self._haul(session, profile, measure=True))
        deals = ((session.get("work") or {}).get("negotiated") or {}).get(
            profile.get("orgId")
        ) or {}
        if not deals:
            return shown
        criteria = []
        for criterion in shown.get("criteria") or []:
            deal = deals.get(str(criterion.get("id")))
            if deal:
                # 협상 단가 근거를 맨 앞에 두고 매입 기준표 근거는 그대로 잇는다(협상 단가 계산의 입력이다)
                criterion = {
                    **criterion,
                    "unitPriceKrwPerKg": deal["price"],
                    "evidence": [deal["evidence"], *(criterion.get("evidence") or [])],
                }
            criteria.append(criterion)
        return {**shown, "criteria": criteria}

    def _region(self, session: dict[str, Any]) -> str | None:
        try:
            return self.store.get_org(session["emitterOrgId"]).get("region")
        except NotFound:
            return None

    def _profile_haul(
        self, session: dict[str, Any], profile: dict[str, Any], *, measure: bool = False
    ) -> dict[str, Any]:
        """업체 프로필(노선·운반 조건, 수거 가능 지역·최대 수거 거리)과 공장 지역으로 정한 이 공장의 수거·운반 조건."""
        fleet = profile.get("fleet") or {}
        found = pickup_terms.effective(
            profile.get("transport"),
            fleet.get("routes"),
            self._region(session),
            fleet,
            self._pickup_km(session, fleet, measure=measure),
        )
        return {**found, "source": "profile", "seq": None}

    def _pickup_km(
        self, session: dict[str, Any], fleet: dict[str, Any], *, measure: bool = False
    ) -> float | None:
        """공장 → 업체 차고지 거리(최대 수거 거리를 밝힌 업체만).

        배정·대화를 돌리는 동안(measure=True)에는 최적 배정과 같은 계산(도로 거리 제공자, 없으면 직선 거리 × 우회
        계수)으로 재고 진단에 적어 둔다. 조회(진단 뷰, 계약 검토, 승인 재확인)에서는 적어 둔 값을 읽고, 없으면 직선
        거리 × 우회 계수로 어림한다. 조회에서는 근거를 쓰거나 외부 길찾기를 부르지 않는다(체험하기는 쓰기를 거절한다).
        """
        if fleet.get("maxPickupKm") is None:
            return None
        depot = fleet.get("depot")
        try:
            origin = self.store.get_org(session["emitterOrgId"]).get("location")
        except NotFound:
            return None
        if not origin or not depot:
            return None
        key = f"{float(origin[0]):.5f},{float(origin[1]):.5f}:{float(depot[0]):.5f},{float(depot[1]):.5f}"
        known = ((session.get("work") or {}).get("pickupKm") or {}).get(key)
        if known is not None:
            return float(known)
        if not measure:
            factor = basis.resolve(self.registry, session["emitterOrgId"], "distance.detourFactor")
            return haversine_km(origin, depot) * float(factor.value) if factor else None
        rec = JudgeRecorder(
            "pickup_distance", self.registry, session["emitterOrgId"], self.today, {}
        )
        found = vendor_distance(
            rec, {"fleet": fleet}, origin, getattr(self.app, "distance_provider", None)
        )
        if not found["known"]:
            return None
        session.setdefault("work", {}).setdefault("pickupKm", {})[key] = found["km"]
        return found["km"]

    def _haul(
        self, session: dict[str, Any], profile: dict[str, Any], *, measure: bool = False
    ) -> dict[str, Any]:
        """이 공장의 수거·운반 조건. 업체가 조건 답에 밝힌 것이 있으면 그것, 없으면 프로필에서 정한 것."""
        answer = ((session.get("work") or {}).get("pickups") or {}).get(profile.get("orgId"))
        return dict(answer) if answer else self._profile_haul(session, profile, measure=measure)

    def _negotiate(self, session, live: list[Thread], assigned: dict[str, list[str]]) -> set[str]:
        """⑥ 합의 전 단가 협상 한 번. 배정된 흐름마다 회사 측이 요청 폭만큼 나은 단가를 요청하고(코드가 쓴 질문,
        claim은 요청 단가 계산 근거), 업체 측이 자기 범위 안에서 답한다. 단가가 바뀐 대화의 ID를 돌려준다.

        한 대화에 한 번만 한다(다시 실행해도 다시 묻지 않는다). 요청을 보낼 수 없으면(턴 상한, 근거 검사) 건너뛴다.
        """
        work = session["work"]
        if not getattr(self.app, "negotiate", True):
            return set()
        done = work.setdefault("negotiation", {})
        changed: set[str] = set()
        for thread in live:
            sids = sorted(assigned.get(thread.id) or [])
            offers = work["offers"].get(thread.id) or {}
            if not sids or thread.id in done or thread.status != "open":
                continue
            asks, claims, lines = [], [], []
            for sid in sids:
                offer = offers.get(sid)
                if not offer:
                    continue
                result = negotiate.ask_price(
                    self.registry,
                    org_id=thread.emitter_org,
                    today=self.today,
                    stream_id=sid,
                    offer=offer["claim"],
                    vendor_org=thread.vendor_org,
                )
                found = next((c for c in result["claims"] if c["id"] == "askedPrice"), None)
                if found is None:
                    continue
                key = (
                    f"ask_{safe_id(sid)}_unitPrice"  # 받는 쪽이 이름(매입 단가·처리비)을 정하는 꼴
                )
                fee = found["value"] < 0
                # claim의 근거는 요청 단가 계산 하나다(제안 단가와 요청 폭은 그 계산의 입력으로 이어진다)
                claim = {**found, "id": key, "evidence": list(found["evidence"])[:1]}
                claims.append(naming.annotate(claim, "treatmentFee" if fee else "unitPrice"))
                asks.append({"streamId": sid, "claimId": key})
                label = stream_label(self.store.get_stream(sid))
                word = "처리비" if fee else "매입 단가"
                lines.append(f"{label}의 {word}를 {{{{{key}}}}}에 맞춰 주실 수 있을까요?")
            done[thread.id] = {"asks": asks, "result": {}}
            if not asks:
                continue
            message = Message(
                thread_id=thread.id,
                sender=thread.emitter_org,
                recipient=thread.vendor_org,
                type="question",
                stage=5,
                text="합의하기 전에 단가를 한 번 더 여쭙니다. " + " ".join(lines),
                stream_ids=tuple(sids),
                claims=tuple(Claim.from_dict(c) for c in claims),
            )
            message.meta["priceAsks"] = asks
            message.meta["model"] = {"provider": "code", "name": "단가 협상(요청 계산)"}
            checked = validate_message(
                message, self.registry, today=self.today, names=thread_names(self.store, thread)
            )
            message.meta["validation"] = {
                "ok": checked.ok,
                "attempts": 1,
                "rejections": public_rejections([r.to_dict() for r in checked.rejections]),
            }
            if not checked.ok:
                continue
            try:
                thread.check(message)
            except ThreadError:
                continue  # 턴 상한이면 협상하지 않고 받은 조건으로 합의한다
            before = {sid: offers[sid]["unitPriceKrwPerKg"] for sid in sids if sid in offers}
            self._append(session, thread, message, "company_agent")
            self._ask_vendor(session, thread)
            after = work["offers"].get(thread.id) or {}
            memo = work["threadTools"].get(thread.id) or []
            for sid, price in before.items():
                new = after.get(sid)
                done[thread.id]["result"][sid] = {
                    "before": price,
                    "after": new["unitPriceKrwPerKg"] if new else None,
                }
                if not new or new["unitPriceKrwPerKg"] == price:
                    continue
                route = memo_result(memo, "judge_route", streamId=sid) or {}
                criterion = (route.get("output") or {}).get("criterionId")
                if criterion:
                    work.setdefault("negotiated", {}).setdefault(thread.vendor_org, {})[
                        str(criterion)
                    ] = {"price": new["unitPriceKrwPerKg"], "evidence": new["claim"]["evidence"][0]}
                    changed.add(thread.id)
        return changed

    def _propose(self, session: dict[str, Any]) -> bool:
        work, org_id = session["work"], session["emitterOrgId"]
        streams = self.store.list_streams(org_id)
        by_id = {s["id"]: s for s in streams}
        profiles = {p["orgId"]: p for p in self.store.list_vendor_profiles()}
        agent = self._company(session)
        live = [
            t
            for t in self._threads(session["id"])
            if t.status == "open" and work["offers"].get(t.id)
        ]
        for thread in list(live):  # 제안을 받은 뒤 허가 품목이 바뀌었으면 근거와 함께 닫는다
            failed, evidence = self._permit_failures(
                session, thread, sorted(work["offers"][thread.id])
            )
            if failed:
                done = self._drop_for_permit(session, thread, failed, evidence, reassign=False)
                if done == "closed" or not work["offers"].get(thread.id):
                    live.remove(thread)
        work["stages"]["5"] = "running"
        costs = []

        def cost_tools(threads: list[Thread]) -> None:
            for thread in threads:
                offered = sorted(work["offers"][thread.id])
                # 대화 중에 담당자 답으로 흐름이 바뀌었으면 경로 판단부터 다시 한다(같은 입력이면 다시 부르지
                # 않는다). 결과 한 장의 확인할 것과 민감도는 이 판단에서 나온다. 협상한 단가가 있으면 그 단가로
                calls = [("judge_route", {"streamId": sid}) for sid in offered]
                calls += [("cost_carbon", {"streamId": sid}) for sid in offered]
                calls.append(("check_loading", {"streamIds": offered}))
                results, trace = agent.run_tools(
                    calls,
                    streams=[by_id[s] for s in thread.stream_ids],
                    vendor=self._public(session, profiles[thread.vendor_org]),
                    memo=work["threadTools"].setdefault(thread.id, []),
                )
                self._flush(session["id"], trace, thread.id, org_id)
                costs.extend(r.get("status") for r in results if r.get("tool") != "judge_route")

        cost_tools(live)
        work["stages"]["5"] = (
            "needs_check"
            if not live or "needs_information" in costs or "error" in costs
            else "done"
        )

        if work.get("result") is not None:
            # 다시 연 대화의 재협상이 끝났다. 수거·운반 조건만 바뀌어 다시 연 대화는 처음 합의 때처럼 합의 전 단가 협상을
            # 한 번 하고(단가가 바뀌면 그 단가로 비용을 다시 계산한다), 이미 합의했거나 확정한 다른 대화의 배정은 그대로
            # 두고 최적안과 결과 한 장을 다시 만든다(새 조건이 최적안에 들어가면 그 대화는 다시 합의가 된다)
            again = [
                t
                for t in live
                if t.id in (work.get("renegotiating") or {})
                and (work.get("renegotiatePrice") or {}).get(t.id)
            ]
            asked = {
                t.id: [s for s in work["renegotiating"][t.id] if s in work["offers"][t.id]]
                for t in again
            }
            changed = self._negotiate(session, again, asked)
            if changed:
                cost_tools([t for t in again if t.id in changed])
            self._reassign(session, cause="terms")
            # run()이 다음 단계로 넘기므로 한 단계 앞에 둔다(승인 대기면 propose → approval)
            session["phase"] = "propose" if session["phase"] == "approval" else "approval"
            return False

        def optimize(threads: list[Thread]) -> tuple[dict[str, Any], dict[str, list[str]]]:
            vendors = [self._assign_vendor(session, profiles[t.vendor_org]) for t in threads]
            exclude = [
                {"streamId": s["id"], "vendorOrgId": t.vendor_org}
                for t in threads
                for s in streams
                if s["id"] not in work["offers"][t.id]
            ]
            arguments: dict[str, Any] = {"streams": streams, "vendors": vendors}
            if exclude:
                arguments["exclude"] = exclude
            self._with_origin(session, arguments)
            optimized = self._tool(
                session,
                "optimize_assignment",
                arguments,
                summary_ids={
                    "streamIds": [s["id"] for s in streams],
                    "vendorOrgIds": [v["orgId"] for v in vendors],
                },
            )
            thread_of = {t.vendor_org: t for t in threads}
            assigned: dict[str, list[str]] = {}
            for item in optimized["output"].get("assignments") or []:
                thread = thread_of.get(item.get("vendorOrgId"))
                if thread is not None:
                    assigned.setdefault(thread.id, []).append(item["streamId"])
            return optimized, assigned

        optimized, assigned = optimize(live)
        # ⑥ 합의 전에 단가를 한 번 협상하고, 단가가 바뀌었으면 그 단가로 비용과 최적안을 다시 계산한다
        changed = self._negotiate(session, live, assigned)
        live = [t for t in live if t.status == "open" and work["offers"].get(t.id)]
        if changed:
            cost_tools([t for t in live if t.id in changed])
            optimized, assigned = optimize(live)
        work["optimize"] = optimized
        work["assigned"] = assigned
        for thread in live:
            if thread.id not in assigned:
                continue
            thread.mark_agreed()
            self.store.put_thread(thread)
            self.store.block_approval(session["id"], thread.id, None)
            self._event(
                session["id"],
                "system",
                "thread",
                "최적안에 들어간 조건입니다. 양쪽 담당자의 승인을 기다립니다.",
                thread.id,
                status="agreed",
                streamIds=assigned[thread.id],
                agreedSeq=thread.agreed_seq,
            )
            self.feed.note(
                session["id"],
                "최적안에 들어간 조건입니다. 양쪽 담당자의 승인을 기다립니다.",
                stage=6,
                thread=thread,
                org_id=thread.emitter_org,
                shared=True,
            )
            self._remember(session, thread)
            self._loading_for(session, thread, assigned[thread.id], streams, profiles)
            self._verify(session, thread)
        self._assigned_requirements(session, streams)
        work["result"] = self._result(session, streams)
        self._announce_reviews(session)
        work["stages"]["6"] = (
            "needs_check" if work["result"]["status"] == "needs_information" else "done"
        )
        if not assigned:
            session["phase"] = "approval"  # 다음 단계(done)로 넘어가게 한다
        return False

    def _failures(self, session, thread: Thread) -> list[str]:
        """합의한 스레드의 승인을 막아야 하는 사유(없으면 빈 목록). 저장소를 바꾸지 않는다.

        - 메시지를 근거 검증기에 다시 넣어 나온 거부 코드
        - EVIDENCE_CHANGED: 메시지가 인용한 근거가 대화 뒤에 바뀌었거나 사라졌다
        - STREAM_CHANGED: 합의한 뒤에 배정된 흐름의 자료가 바뀌었다
        - VENDOR_TERMS_CHANGED: 합의한 뒤에 업체가 바꾼 설정이 합의 조건에 영향을 준다
        """
        failed: list[str] = []
        work = session["work"]
        names = thread_names(self.store, thread)
        reopened = int((work.get("reopened") or {}).get(thread.id) or 0)
        for message in thread.messages:
            if message.seq < reopened and message.sender == thread.vendor_org:
                # 조건이 바뀌어 다시 협상하기 전에 업체가 낸 답은 지난 조건의 기록이다. 지금 합의는
                # 그 뒤의 메시지에 있다(업체가 기준표 값을 고치면 예전 답의 근거도 함께 바뀐다)
                continue
            result = validate_message(message, self.registry, today=self.today, names=names)
            failed += [f"{thread.id}_m{message.seq}: {code}" for code in result.codes]
            # 대화 뒤에 근거가 바뀌었거나 사라졌으면 그 메시지의 근거 확인은 실패다
            if message.meta.get("evidenceMarks", {}) != self._fingerprints(message):
                failed.append(f"{thread.id}_m{message.seq}: EVIDENCE_CHANGED")
        basis = (session["work"].get("agreedBasis") or {}).get(thread.id)
        if basis is not None and basis != self._basis(session, thread):
            # 합의한 뒤에 흐름 자료(재질, 물량, 측정값 등)가 바뀌었다. 앞선 합의와 승인은 이전 자료 기준이다
            failed.append(f"{thread.id}: STREAM_CHANGED")
        agreed = (work.get("agreedTerms") or {}).get(thread.id)
        if agreed:
            now = self._vendor_terms(session, thread, agreed.get("inputs"))
            assigned = work["assigned"].get(thread.id) or []
            if now["common"] != agreed["common"] or any(
                now["streams"].get(sid) != agreed["streams"].get(sid) for sid in assigned
            ):
                # 합의한 뒤에 업체가 매입 기준표·허가 품목·가드레일을 바꿔 합의 조건이 달라졌다
                failed.append(f"{thread.id}: VENDOR_TERMS_CHANGED")
            if agreed.get("pickup") is not None and now["pickup"] != agreed["pickup"]:
                # 합의한 뒤에 업체의 노선이나 운반 조건이 바뀌어 이 공장의 수거 상태나 운반비가 달라졌다
                failed.append(f"{thread.id}: PICKUP_TERMS_CHANGED")
        return list(dict.fromkeys(failed))

    def _vendor_terms(
        self, session, thread: Thread, inputs: dict[str, dict] | None = None
    ) -> dict[str, Any]:
        """합의 조건에 영향을 주는 업체 쪽 설정의 지문(단가, 최소 물량, 상한, 요일, 횟수).

        배정된 흐름마다 지금 프로필로 다시 본다: 허가 품목이 맞는지, 어느 매입 기준이 골라지고 그
        단가·최소 물량·오염도·수분 상한이 얼마인지, 합의한 단가·요일이 가드레일 안인지, 자동 거절에
        걸리는지. 여기에 수거 횟수를 정하는 값(월 최소 횟수, 차량 용량)을 더한다. 합의 조건과 관계없는
        변경(다른 재질의 기준, 가능 요일 추가, 모델 선택 등)은 지문을 바꾸지 않는다.
        inputs는 합의한 때의 흐름 자료다. 그것으로 계산해 흐름 쪽 변경(STREAM_CHANGED)과 섞이지 않게 한다.
        """
        work = session["work"]
        offers = work["offers"].get(thread.id) or {}
        profile = self.store.get_vendor_profile(thread.vendor_org)
        public = public_profile(profile, fleet_cost=False)
        guard = profile.get("guardrails") or {}
        fleet = profile.get("fleet") or {}
        streams: dict[str, str] = {}
        used: dict[str, dict] = {}
        for sid in sorted(work["assigned"].get(thread.id) or []):
            stream = (inputs or {}).get(sid)
            if stream is None:
                stream = without_emitter_only(self.store.get_stream(sid))
            used[sid] = stream
            criterion, checks = best_criterion(stream, public, quality(stream))
            offer = offers.get(sid) or {}
            violations = rails.check_terms(
                guard,
                {
                    "unitPriceKrwPerKg": offer.get("unitPriceKrwPerKg"),
                    "days": list(offer.get("days") or []),
                    "conditions": list(offer.get("conditions") or []),
                },
            )
            streams[sid] = ids.digest(
                {
                    "permit": permit_match(stream, public)[0],
                    "criterion": None
                    if criterion is None
                    else {
                        key: criterion.get(key)
                        for key in (
                            "id",
                            "material",
                            "grade",
                            "form",
                            "unitPriceKrwPerKg",
                            "minMonthlyKg",
                            "maxContaminationPct",
                            "maxMoisturePct",
                        )
                    },
                    "checks": [[check[0], check[1]] for check in checks],
                    "guardrails": sorted({v.code for v in violations}),
                    "autoDecline": rails.auto_decline_reason(guard, stream) is not None,
                }
            )
        common = ids.digest(
            {
                "minPickupsPerMonth": guard.get("minPickupsPerMonth"),
                "capacityKg": fleet.get("capacityKg"),
                "capacityM3": fleet.get("capacityM3"),
            }
        )
        # 이 공장의 수거 상태와 운반비 조건(노선, 운반 조건, 노선 밖 수거 조건). 근거 ID 없이 값만 견준다
        haul = pickup_terms.shown(self._profile_haul(session, profile))
        return {
            "streams": streams,
            "common": common,
            "inputs": used,
            "pickup": ids.digest(haul),
            "pickupShown": haul,
        }

    def _remember(self, session, thread: Thread) -> None:
        """합의한 내용의 지문을 남긴다(흐름 자료와 받은 조건, 업체 쪽 설정). 승인 전에 다시 견준다.

        배정된 흐름이 늘어난 경우에는 이미 합의해 둔 흐름의 업체 쪽 지문을 그대로 둔다.
        """
        work = session["work"]
        work.setdefault("renegotiating", {}).pop(thread.id, None)
        work.setdefault("agreedBasis", {})[thread.id] = self._basis(session, thread)
        fresh = self._vendor_terms(session, thread)
        old = work.setdefault("agreedTerms", {}).get(thread.id)
        if old:
            fresh["common"] = old["common"]
            if old.get("pickup") is not None:
                fresh["pickup"], fresh["pickupShown"] = old["pickup"], old.get("pickupShown")
            for sid in fresh["streams"]:
                if sid in old["streams"] and sid in old.get("inputs", {}):
                    fresh["streams"][sid] = old["streams"][sid]
                    fresh["inputs"][sid] = old["inputs"][sid]
        work["agreedTerms"][thread.id] = fresh

    @staticmethod
    def _block_reason(failed: list[str], change: str | None = None) -> str:
        reason = "근거 확인에 실패한 항목이 있어 승인할 수 없습니다: " + ", ".join(failed)
        codes = block_codes(reason)
        if "STREAM_CHANGED" in codes:
            reason += ". 합의한 뒤에 폐기물 자료가 바뀌었습니다. 진단을 새로 진행해 주세요."
        if "VENDOR_TERMS_CHANGED" in codes:
            reason += (
                ". 합의한 뒤에 업체의 조건(매입 기준표·허가 품목·가드레일)이 바뀌었습니다. "
                "진단을 다시 진행하면 새 조건으로 대화를 이어 갑니다."
            )
        if "PICKUP_TERMS_CHANGED" in codes:
            reason += (
                ". 합의한 뒤에 업체의 수거·운반 조건이 바뀌었습니다"
                + (f"({change})" if change else "")
                + ". 앞선 승인은 지웠습니다. 진단을 다시 진행하면 새 조건으로 운반비와 한 달 손익을 다시 계산합니다."
            )
        return reason

    def _verify(self, session, thread: Thread) -> list[str]:
        """합의한 스레드를 다시 확인한다. 실패가 있으면 승인을 막고 앞서 한 승인을 지운다.

        몇 번을 불러도 결과가 같다: 막는 이유가 그대로면 저장소도 실행 기록도 건드리지 않는다
        (뷰를 만들 때마다 부른다).
        """
        failed = self._failures(session, thread)
        current = next(
            (a for a in self.store.list_approvals(session["id"]) if a["threadId"] == thread.id),
            None,
        )
        if failed:
            reason = self._block_reason(failed, self._pickup_change(session, thread))
            if current and current["blocked"] and current["blockedReason"] == reason:
                return failed
            self.store.block_approval(session["id"], thread.id, reason)
            if any(thread.approvals.values()):
                # 앞서 한 승인은 바뀌기 전의 내용에 대한 것이다. 스레드의 승인도 함께 지운다
                for side in ("emitter", "vendor"):
                    thread.approvals[side] = False
                self.store.put_thread(thread)
            self._event(
                session["id"],
                "validator",
                "validation",
                "근거 확인 실패로 승인을 막았습니다.",
                thread.id,
                ok=False,
                failed=failed,
                codes=block_codes(reason),
            )
        elif current and current["blocked"]:
            self.store.block_approval(session["id"], thread.id, None)
        return failed

    def _pickup_change(self, session, thread: Thread) -> str | None:
        """합의한 때와 지금의 수거·운반 조건을 숫자 없이 견준 문장. 바뀌지 않았으면 None."""
        agreed = ((session["work"].get("agreedTerms") or {}).get(thread.id) or {}).get(
            "pickupShown"
        )
        if not agreed:
            return None
        try:
            now = pickup_terms.shown(
                self._profile_haul(session, self.store.get_vendor_profile(thread.vendor_org))
            )
        except NotFound:
            return None
        if now == agreed:
            return None
        return f"합의: {haul_words(agreed)} → 지금: {haul_words(now)}"

    def recheck(self, session_id: str) -> list[str]:
        """승인을 기다리는 진단의 합의한 대화를 모두 다시 확인한다. 막힌 대화의 ID를 돌려준다.

        근거나 흐름 자료가 바뀌면 그것을 인용한 대화가 여럿일 수 있다. 승인을 누른 대화 하나만이
        아니라 관련된 대화를 모두, 승인을 시도하기 전에 막는다(자료를 바꾼 때와 뷰를 만들 때 부른다).
        """
        session = self.store.get_session(session_id)
        if session.get("status") != "awaiting_approval":
            return []
        blocked = []
        for thread in self._threads(session_id):
            if thread.status == "agreed" and self._verify(session, thread):
                blocked.append(thread.id)
        return blocked

    def _basis(self, session, thread: Thread) -> str:
        """합의한 내용의 지문: 배정된 흐름의 자료와 그 흐름에 받은 조건.

        현행 처리 단가는 업체와 나눈 조건이 아니므로 넣지 않는다.
        """
        work = session["work"]
        assigned = sorted(work["assigned"].get(thread.id) or [])
        offers = work["offers"].get(thread.id) or {}
        streams = []
        for sid in assigned:
            streams.append(without_emitter_only(self.store.get_stream(sid)))
        return ids.digest(
            {
                "streams": streams,
                "offers": {sid: offers.get(sid) for sid in assigned},
                "agreedSeq": thread.agreed_seq,
            }
        )

    def _result(self, session, streams: list[dict[str, Any]]) -> dict[str, Any]:
        """결과 한 장(계약 7장 result). 숫자는 모두 도구가 낸 claim이다."""
        work, org_id = session["work"], session["emitterOrgId"]
        optimized = work["optimize"]
        # 담당자에게 넘어갔거나 거절로 닫힌 대화의 제안은 배정에도 대안에도 쓰지 않는다
        threads = {
            t.vendor_org: t
            for t in self._threads(session["id"])
            if t.status in ("open", "agreed", "confirmed")
        }
        names = {o["id"]: o.get("name") for o in self.store.list_orgs()}
        by_id = {s["id"]: s for s in streams}
        assignments, sensitivity, used = [], [], []
        handed = {h["streamId"]: h for h in work.get("handedOff") or []}

        def offer_of(vendor_id: str | None, sid: str) -> dict[str, Any] | None:
            thread = threads.get(vendor_id)
            return (work["offers"].get(thread.id) or {}).get(sid) if thread else None

        def tool_of(vendor_id: str | None, name: str, **arguments: Any) -> dict[str, Any] | None:
            thread = threads.get(vendor_id)
            return (
                memo_result(work["threadTools"].get(thread.id) or [], name, **arguments)
                if thread
                else None
            )

        for item in optimized["output"].get("assignments") or []:
            sid, vendor_id = item["streamId"], item.get("vendorOrgId")
            tag = safe_id(sid)
            offer = offer_of(vendor_id, sid)
            route = tool_of(vendor_id, "judge_route", streamId=sid)
            cost = tool_of(vendor_id, "cost_carbon", streamId=sid)
            status = item["status"]
            checks = []
            for source in (route, cost):
                for check in (source or {}).get("checks") or []:
                    if check["key"] == "pickup" or any(c["key"] == check["key"] for c in checks):
                        continue  # 수거 상태는 아래 "수거 노선" 한 줄로 보인다
                    if check["status"] != "met" or check["key"] in (
                        "current_assumed",
                        "current_method",
                    ):
                        checks.append(
                            {k: check.get(k) for k in ("key", "status", "text", "evidence")}
                        )
            if offer:
                if offer["type"] == "conditional":
                    # 조건이 "흐름 이름: 조건" 꼴로 나뉘어 있으면 조건이 걸린 흐름만 조건부다.
                    # 어느 흐름의 것인지 알 수 없는 조건이 하나라도 있으면 모두 조건부로 둔다
                    # 업체 쪽 글은 흐름을 보이는 이름(stream_label)으로 적는다
                    heads = [f"{stream_label(s)}:" for s in by_id.values()]
                    loose = [t for t in offer["conditions"] if not t.startswith(tuple(heads))]
                    mine = any(
                        t.startswith(f"{stream_label(by_id[sid])}:") for t in offer["conditions"]
                    )
                    if mine or loose or not offer["conditions"]:
                        status = "conditional"
                known = {c["id"]: c for c in self._thread_claims(threads[vendor_id])}
                head = f"{stream_label(by_id[sid])}:"
                for text in offer["conditions"]:
                    if text.startswith(head):
                        cited = [
                            known[m.group(1)]
                            for m in PLACEHOLDER.finditer(text)
                            if m.group(1) in known
                        ]
                        checks.append(
                            {
                                "key": "vendor_condition",
                                "status": "conditional",
                                # 결과 한 줄은 이미 그 폐기물의 것이라 앞의 "이름:"은 뺀다.
                                # 자리표시자는 claims로 그린다
                                "text": text[len(head) :].strip() or text,
                                "claims": cited,
                                "evidence": list(
                                    dict.fromkeys(e for c in cited for e in c["evidence"])
                                ),
                            }
                        )
                expected = claim_in(route, "unitPrice", "x")
                if expected and abs(float(expected["value"]) - offer["unitPriceKrwPerKg"]) > 1e-9:
                    status = "conditional"
                    checks.append(
                        {
                            "key": "price_differs",
                            "status": "conditional",
                            "text": "업체가 낸 단가가 매입 기준표로 계산한 단가와 다릅니다. 확인 필요.",
                            "evidence": list(offer["claim"]["evidence"]),
                        }
                    )
            pending = [
                questions.FIELDS[u["field"]]["label"]
                for u in work.get("unresolved") or []
                if u["streamId"] == sid and u["field"] in questions.FIELDS
            ]
            thread = threads.get(vendor_id)
            if thread is not None:
                # 최소 물량과 무게·부피 적재(check_loading). 실제로 배정된 흐름을 합친 값이 있으면 그것을,
                # 없으면 업체가 낸 제안 전체를 합친 값을 쓴다. 충족한 검사도 함께 보여 준다
                together = sorted(work["assigned"].get(thread.id) or [])
                loading = tool_of(vendor_id, "check_loading", streamIds=together) or tool_of(
                    vendor_id,
                    "check_loading",
                    streamIds=sorted(work["offers"].get(thread.id) or {}),
                )
                for check in (loading or {}).get("checks") or []:
                    cited = [
                        claim_in(loading, key, f"{tag}_load_{key}")
                        for key in LOADING_CLAIMS.get(check["key"], ())
                    ]
                    checks.append(
                        {
                            "key": check["key"],
                            "status": check["status"],
                            "text": check["text"],
                            "claims": [c for c in cited if c],
                            "evidence": list(dict.fromkeys(check.get("evidence") or [])),
                        }
                    )
                    if check["status"] != "met" and status == "calculated":
                        status = "conditional"
                used.append(loading)
                pickup = self._route_check(session, vendor_id)
                if pickup is not None:
                    checks.append(pickup)
                # 수거 가능 지역·최대 수거 거리(2026-11-19 멘토링)도 위 수거 상태 한 줄(pickup_route)에 들어 있다
                permit = item.get("permit")
                if permit:
                    checks.append(
                        {
                            "key": "permit_level",
                            "status": permit["status"],
                            "text": permit["text"],
                            "evidence": list(permit.get("evidence") or []),
                        }
                    )
            gone = [
                x for x in optimized["output"].get("pickupExcluded") or [] if x["streamId"] == sid
            ]
            if vendor_id is None and gone:
                # 수거 불가라고 밝힌 업체를 빼서 옮길 곳이 없어졌다. 다른 확인 항목보다 먼저 그 까닭을 적는다
                who = ", ".join(dict.fromkeys(names.get(x["vendorOrgId"]) or "업체" for x in gone))
                checks.insert(
                    0,
                    {
                        "key": "pickup_route",
                        "status": "unmet",
                        "text": f"{josa(who, '은는')} 이 공장까지 수거하러 올 수 없다고 밝혀 배정에서 "
                        "뺐습니다. 다른 업체를 찾거나 업체와 수거 방법을 다시 정해야 합니다.",
                        "evidence": list(
                            dict.fromkeys(e for x in gone for e in x.get("evidence") or [])
                        ),
                    },
                )
            if vendor_id is None and sid in handed:
                # 이 흐름을 맡기로 합의한 대화가 담당자에게 넘어갔다. 현행 유지를 "확정"으로 보이지 않게 한다
                status = "needs_information"
                checks.append(
                    {
                        "key": "handoff",
                        "status": "needs_information",
                        "text": (
                            f"{names.get(handed[sid]['vendorOrgId']) or '업체'}의 허가 품목이 맞지 "
                            "않게 되어 합의한 대화를 닫고 이 폐기물의 배정을 뺐습니다. 확인 필요."
                            if handed[sid].get("cause") == "permit"
                            else f"{names.get(handed[sid]['vendorOrgId']) or '업체'}의 허가 품목이 "
                            "이 폐기물과 맞지 않게 되어 배정을 뺐습니다. 확인 필요."
                            if handed[sid].get("cause") == "permit_stream"
                            else f"{names.get(handed[sid]['vendorOrgId']) or '업체'}로 가는 경로는 허가증 원문 "
                            "대조 결과 진행할 수 없어 배정을 뺐습니다. 다른 업체를 찾지 못했습니다. 확인 필요."
                            if handed[sid].get("cause") == "permit_check"
                            else f"{josa(names.get(handed[sid]['vendorOrgId']) or '업체', '와과')} 합의한 대화가 "
                            "담당자에게 넘어가 이 폐기물의 배정을 뺐습니다. 확인 필요."
                        ),
                        "evidence": [],
                    }
                )
            if vendor_id is None and pending:
                # 모르는 값 때문에 업체를 정하지 못한 흐름을 "확정(현행 유지)"으로 보이지 않게 한다
                status = "needs_information"
                checks.append(
                    {
                        "key": "unresolved",
                        "status": "needs_information",
                        "text": f"확인되지 않은 값({', '.join(dict.fromkeys(pending))})이 있어 "
                        "업체 배정을 확정하지 못했습니다. 확인 필요.",
                        "evidence": [],
                    }
                )
            alternatives = []
            blocked_routes = self._blocked_routes(session, by_id[sid], vendor_id, names)
            blocked_ids = {b["vendorOrgId"] for b in blocked_routes}
            for other_id, thread in sorted(threads.items()):
                other = (work["offers"].get(thread.id) or {}).get(sid)
                if other_id == vendor_id or not other or other_id in blocked_ids:
                    # 허가 확인 결과 진행할 수 없는 경로는 대안(진행 가능한 선택지)에 넣지 않는다
                    continue
                other_route = tool_of(other_id, "judge_route", streamId=sid)
                # 운반비와 전처리 인건비까지 뺀 값(cost_carbon)이 있으면 그것으로 견준다
                other_cost = tool_of(other_id, "cost_carbon", streamId=sid)
                other_after = claim_in(
                    other_cost, "monthlyAfter", f"{tag}_alt_{safe_id(other_id)}_monthlyAfter"
                ) or claim_in(
                    other_route, "monthlyAfter", f"{tag}_alt_{safe_id(other_id)}_monthlyAfter"
                )
                now_claim = claim_in(optimized, item.get("monthlyNow"), "x")
                worse = bool(
                    vendor_id is None
                    and other_after
                    and now_claim
                    and float(other_after["value"]) <= float(now_claim["value"])
                )
                other_output = (other_cost or {}).get("output") or {}
                excluded = next(
                    (
                        x
                        for x in optimized["output"].get("permitExcluded") or []
                        if x["streamId"] == sid and x["vendorOrgId"] == other_id
                    ),
                    None,
                )
                # 수거 불가라고 밝힌 업체는 진행할 수 있는 선택지가 아니다(한 달 값을 견주지 않는다)
                no_pickup = next(
                    (
                        x
                        for x in optimized["output"].get("pickupExcluded") or []
                        if x["streamId"] == sid and x["vendorOrgId"] == other_id
                    ),
                    None,
                )
                other_pending = bool(other_output.get("transportPending")) and not no_pickup
                alternatives.append(
                    {
                        "vendorOrgId": other_id,
                        "vendorName": names.get(other_id),
                        "route": (other_route or {}).get("output", {}).get("route"),
                        # 이 업체에만 보낼 때의 탄소 변화(처리 방식 + 수거 운행). 거리가 먼 업체는 운행 배출이 크다
                        "carbon": claim_in(
                            other_cost,
                            other_output.get("carbonTotal") or other_output.get("carbon"),
                            f"{tag}_alt_{safe_id(other_id)}_carbon",
                        ),
                        "unitPrice": {
                            **other["claim"],
                            "id": f"{tag}_alt_{safe_id(other_id)}_unitPrice",
                        },
                        "monthlyAfter": None if no_pickup else other_after,
                        # 이 업체로 보낼 때의 허가 확인 상태(이 업체 자신의 근거로만 판정)
                        "permit": self._permit_of(session, by_id[sid], other_id),
                        # 운반비가 정해지지 않아 한 달 값이 별도 운반비 반영 전이다(확정 견적과 견줄 때 밝힌다)
                        "transportPending": other_pending,
                        "pickupStatus": other_output.get("pickupStatus"),
                        "note": "허가 근거가 맞지 않아 배정하지 않았습니다. " + excluded["text"]
                        if excluded
                        else "수거 불가: " + no_pickup["text"]
                        if no_pickup
                        else "운반비와 인건비를 넣으면 지금보다 이득이 없어 고르지 않았습니다"
                        if worse
                        else (
                            "조건부 제안"
                            if other["type"] == "conditional"
                            else "선택되지 않은 제안"
                        )
                        + (" · 한 달 값은 별도 운반비 반영 전" if other_pending else ""),
                    }
                )
            entry = {
                "streamId": sid,
                "vendorOrgId": vendor_id,
                "vendorName": names.get(vendor_id) if vendor_id else None,
                "threadId": threads[vendor_id].id if vendor_id in threads else None,
                "route": item["route"],
                # 처리 방식(재활용, 열분해, 지금 방식이면 현행 처리). 경로(매각·위탁 처리)와 따로 보인다
                "process": self._process_of(vendor_id, by_id[sid]),
                "status": status,
                "unitPrice": {**offer["claim"], "id": f"{tag}_unitPrice"} if offer else None,
                "monthlyAfter": claim_in(
                    optimized, item.get("monthlyAfter"), f"{tag}_monthlyAfter"
                ),
                "monthlyNow": claim_in(optimized, item.get("monthlyNow"), f"{tag}_monthlyNow"),
                "difference": claim_in(optimized, item.get("difference"), f"{tag}_difference"),
                # 순비용의 내역: 매각 수익(처리비면 음수), 배출 기업이 따로 내는 운반비, 전처리 인건비
                "revenue": claim_in(optimized, item.get("revenue"), f"{tag}_revenue"),
                "transport": claim_in(optimized, item.get("transport"), f"{tag}_transport"),
                "labor": claim_in(optimized, item.get("labor"), f"{tag}_labor"),
                "transportPaidBy": item.get("transportPaidBy"),
                # 수거 상태와 운반비 조건(견적 금액·단위·횟수, 업체 월 운반비, 업체 답의 메시지 번호)
                "pickup": self._pickup_view(session, vendor_id, item, optimized, tag)
                if vendor_id
                else None,
                # 운반비가 정해지지 않아 이 줄의 한 달 값은 별도 운반비 반영 전이다
                "transportPending": bool(item.get("transportPending")),
                "reuse": reuse_view((work.get("reuse") or {}).get(sid), tag),
                # 탄소 변화 = 처리 방식이 바뀌어 달라지는 배출 + 수거 운행 배출(같은 업체로 가는 폐기물끼리 나눈 몫)
                "carbon": claim_in(optimized, item.get("carbon"), f"{tag}_carbon"),
                "carbonProcess": claim_in(
                    optimized, item.get("carbonProcess"), f"{tag}_carbonProcess"
                ),
                "carbonTransport": claim_in(
                    optimized, item.get("carbonTransport"), f"{tag}_carbonTransport"
                ),
                "transportKm": claim_in(optimized, item.get("transportKm"), f"{tag}_transportKm"),
                # 배정과 같은 탄소(운행 몫 포함)로 환산한 값이 있으면 그것을, 없으면 업체 하나만 볼 때의 환산
                "carbonValue": claim_in(optimized, item.get("carbonValue"), f"{tag}_carbonValue")
                or claim_in(cost, (cost or {}).get("output", {}).get("kau"), f"{tag}_carbonValue"),
                # 허가 근거의 수준(허가증·공공 명부 확인, 허가증 문서, 업체 입력, 공공 명부와 다름)
                "permit": item.get("permit"),
                "currentAssumed": bool(item.get("currentAssumed")),
                "checks": checks,
                "alternatives": alternatives,
                # 허가 확인 결과 이 경로로 진행할 수 없는 업체(사유와 함께). 예상 손익은 싣지 않는다
                "blockedRoutes": blocked_routes,
            }
            assignments.append(entry)
            for found in ((route or {}).get("output", {}).get("current") or {}).get(
                "sensitivity"
            ) or []:
                claim = claim_in(route, found.get("difference"), f"{tag}_{found.get('difference')}")
                if claim:
                    sensitivity.append(
                        {
                            "label": f"{by_id[sid].get('name')}: {found['label']}",
                            "streamId": sid,
                            "difference": claim,
                        }
                    )
            used += [route, cost]
        for item in optimized["output"].get("skipped") or []:
            assignments.append(
                {
                    "streamId": item["streamId"],
                    "vendorOrgId": None,
                    "vendorName": None,
                    "threadId": None,
                    "route": None,
                    "status": "needs_information",
                    "unitPrice": None,
                    "monthlyAfter": None,
                    "monthlyNow": None,
                    "difference": None,
                    "revenue": None,
                    "transport": None,
                    "labor": None,
                    "transportPaidBy": None,
                    "pickup": None,
                    "transportPending": False,
                    "reuse": reuse_view(
                        (work.get("reuse") or {}).get(item["streamId"]),
                        safe_id(item["streamId"]),
                    ),
                    "carbon": None,
                    "carbonValue": None,
                    "currentAssumed": False,
                    "checks": [
                        {
                            "key": "skipped",
                            "status": "needs_information",
                            "text": item["reason"],
                            "evidence": [],
                        }
                    ],
                    "alternatives": [],
                    "blockedRoutes": [],
                }
            )
        totals = optimized["output"].get("totals") or {}
        assumptions: list[str] = []
        for source in (optimized, *[u for u in used if u]):
            for claim in source.get("claims") or []:
                assumptions += [e for e in claim["evidence"] if e.startswith("assumption:")]
        for done in work["identify"].values():
            assumptions += done["grade"].get("assumptions") or []
        if work.get("negotiated"):
            # 협상 단가는 요청 폭(가정값)으로 요청한 결과다
            found = basis.resolve(self.registry, org_id, negotiate.ASK_KEY)
            if found is not None:
                assumptions.append(found.id)
        materials = {
            part for s in streams for part in str(s.get("material") or "").split("+") if part
        }
        reference = [
            {
                "key": found["key"],
                "material": found["material"],
                "form": found["form"],
                "label": found["evidence"].label,
                "asOf": found["evidence"].as_of,
                "price": naming.annotate(
                    {
                        "id": f"referencePrice{index}",
                        "kind": "number",
                        "value": found["evidence"].value,
                        "unit": found["evidence"].unit,
                        "evidence": [found["evidence"].id],
                    },
                    "referencePrice",
                ),
                "note": basis.REFERENCE_PRICE_NOTE,
            }
            for index, found in enumerate(
                basis.reference_prices(self.registry, org_id, sorted(materials)), 1
            )
        ]
        statuses = [a["status"] for a in assignments] or ["needs_information"]
        shown_totals = {
            key: claim_in(optimized, totals.get(key), f"total_{key}")
            for key in ("monthlyNow", "monthlyAfter", "difference", "carbon")
        }
        return {
            "status": max(statuses, key=lambda s: WORST[s]),
            "assignments": assignments,
            "totals": shown_totals,
            # 운반비가 정해지지 않아 별도 운반비 반영 전 값이 합계에 섞인 폐기물(합계는 잠정값이다)
            "transportPending": list(totals.get("pending") or []),
            # 비용·탄소 비교의 계산 범위(지금 처리 쪽 운송이 빠졌다는 것 등)
            "scope": comparison_scope(assignments),
            "assumptions": self._assumptions(sorted(dict.fromkeys(assumptions)), org_id),
            "sensitivity": sensitivity,
            "referencePrices": reference,
            "carbonInObjective": bool(optimized["output"].get("carbonInObjective")),
            # 탄소를 배정에 넣을 때 쓴 내부 탄소 가격(배출권 시세, 원/tCO2e)
            "carbonPrice": claim_in(
                optimized, optimized["output"].get("carbonPrice"), "carbonPrice"
            ),
            # 탄소를 가장 많이 줄이는 배정과의 비교(완전 탐색으로 풀었을 때만)
            "greenest": self._greenest_view(optimized, names, by_id),
            # 자원순환 효과와 네 시나리오(배출 기업만 본다: 업체 뷰는 최상위 키를 싣지 않는다)
            "circular": self._circular_view(optimized, by_id, org_id),
            "evidence": list(optimized.get("evidence") or []),
            # 가입하지 않은 업체 후보(배출 기업만 본다: 업체 뷰는 최상위 키를 싣지 않는다)
            "outsideVendors": work.get("outside"),
            # 분리 개선 안내(배출 기업만 본다)
            "improvements": self._improvements(org_id, streams, assignments, sorted(threads)),
            # ③ 규정 확인: 폐기물마다 경로 요건 체크리스트와 관련 조항(배출 기업만 본다)
            "regulation": self._regulation(work, streams),
        }

    def _circular_view(
        self, optimized: dict[str, Any], by_id: dict[str, Any], org_id: str
    ) -> dict[str, Any] | None:
        """최적 배정이 낸 자원순환 효과(judge/circular.py)를 화면용으로. 숫자는 모두 claim이다.

        세 지표(감축량·에너지 절감·신재 대체)는 합치지 않고, 산정 보류는 0으로 바꾸지 않는다. 출처는 근거 시드의
        meta(자료명·국가·연도·표·단위·기준선·적용 조건)에서 만든다.
        """
        found = optimized["output"].get("circular")
        if not found:
            return None

        def claim(key: str | None) -> dict[str, Any] | None:
            return claim_in(optimized, key, key) if key else None

        def name_of(sid: str | None) -> str | None:
            return (by_id.get(sid) or {}).get("name") if sid else None

        used: list[str] = []

        def metric_view(item: dict[str, Any]) -> dict[str, Any]:
            used.extend(e for e in item.get("basis") or [] if e.startswith("ref:"))
            return {
                "status": item["status"],
                "method": item.get("method"),
                "reason": item.get("reason") or "",
                "value": claim(item.get("claim")),
                # 이 값에 쓴 계수(문헌·현장 입력)의 근거 ID
                "evidence": list(item.get("basis") or []),
            }

        scenarios = []
        for scene in found["scenarios"]:
            rows = []
            for row in scene.get("rows") or []:
                rows.append(
                    {
                        "streamId": row["streamId"],
                        "streamName": name_of(row["streamId"]),
                        "route": row["route"],
                        "baseline": row["baseline"],
                        "baselineAssumed": row["baselineAssumed"],
                        **{m: metric_view(row[m]) for m in circular.METRICS},
                        "virginMaterial": row.get("virginMaterial"),
                        "resources": [
                            {"name": r["name"], "value": claim(r["claim"])}
                            for r in row.get("resources") or []
                        ],
                        "unconfirmed": list(row.get("unconfirmed") or []),
                        "explain": row["explain"],
                    }
                )
            totals = {}
            for metric, total in (scene.get("totals") or {}).items():
                totals[metric] = {
                    "status": total["status"],
                    "total": claim(total.get("claim")),
                    "countedKg": claim(total.get("countedKg")),
                    "heldKg": claim(total.get("heldKg")),
                    "heldStreams": [
                        {"streamId": sid, "streamName": name_of(sid)}
                        for sid in total.get("heldStreams") or []
                    ],
                }
            baseline = scene.get("baselineEmission") or {}
            if baseline.get("claim"):
                made = claim(baseline["claim"])
                for part in (made or {}).get("evidence") or []:
                    used.extend(
                        e
                        for c in optimized.get("claims") or []
                        if c["evidence"][:1] == [part]
                        for e in c["evidence"]
                        if e.startswith("ref:")
                    )
            scenarios.append(
                {
                    "id": scene["id"],
                    "label": scene["label"],
                    "available": bool(scene.get("available", True)),
                    "reason": scene.get("reason"),
                    "money": claim(scene.get("money")),
                    "moneyReason": scene.get("moneyReason"),
                    "circularKg": claim(scene.get("circularKg")),
                    "flows": [
                        {"route": route, "kg": claim(key)}
                        for route, key in (scene.get("flows") or {}).items()
                        if key
                    ],
                    "totals": totals,
                    "virginByMaterial": [
                        {"material": m["material"], "total": claim(m["claim"])}
                        for m in scene.get("virginByMaterial") or []
                    ],
                    "baselineEmission": claim(baseline.get("claim")),
                    "rows": rows,
                }
            )
        sources = []
        for evidence_id in dict.fromkeys(used):
            try:
                evidence = self.registry.get(evidence_id, org_id)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
            meta = evidence.meta or {}
            sources.append(
                {
                    "id": evidence.id,
                    "title": evidence.label,
                    "source": evidence.source,
                    "region": meta.get("region")
                    or ("한국" if "환경성적표지" in evidence.id else None),
                    "country": meta.get("country")
                    or ("한국" if "환경성적표지" in evidence.id else None),
                    "year": meta.get("year"),
                    "table": meta.get("table"),
                    "unit": evidence.unit,
                    "originalUnit": meta.get("originalUnit"),
                    "metric": meta.get("metric"),
                    "material": meta.get("material"),
                    "route": meta.get("route"),
                    "baseline": meta.get("baseline"),
                    "conditions": list(meta.get("conditions") or []),
                    "caveats": list(meta.get("caveats") or []),
                    "url": meta.get("url"),
                    "asOf": evidence.as_of,
                }
            )
        bundle = found.get("bundle") or {}
        return {
            "scenarios": scenarios,
            "baselines": dict(found.get("baselines") or {}),
            "bundle": {
                "available": bool(bundle.get("available")),
                "streams": [
                    {"streamId": sid, "streamName": name_of(sid)}
                    for sid in bundle.get("streams") or []
                ],
                "kg": claim(bundle.get("kg")),
            },
            "excluded": [
                {
                    "streamId": x["streamId"],
                    "streamName": name_of(x["streamId"]),
                    "reason": x["reason"],
                }
                for x in found.get("excluded") or []
            ],
            "sources": sources,
        }

    def _greenest_view(
        self, optimized: dict[str, Any], names: dict[str, Any], by_id: dict[str, Any]
    ) -> dict[str, Any] | None:
        """최적 배정이 낸 탄소 우선 배정 비교를 화면용으로. 바뀌는 폐기물과 돈·탄소 차이(claim)."""
        found = optimized["output"].get("greenest")
        if not found:
            return None
        if found.get("same"):
            return {"same": True, "changes": []}
        return {
            "same": False,
            "changes": [
                {
                    "streamId": change["streamId"],
                    "streamName": (by_id.get(change["streamId"]) or {}).get("name"),
                    "vendorOrgId": change["vendorOrgId"],
                    "vendorName": names.get(change["vendorOrgId"])
                    if change["vendorOrgId"]
                    else None,
                    "route": change["route"],
                }
                for change in found["changes"]
            ],
            "monthlyAfter": claim_in(optimized, found["monthlyAfter"], "green_monthlyAfter"),
            "carbon": claim_in(optimized, found["carbon"], "green_carbon"),
            "moneyGap": claim_in(optimized, found["moneyGap"], "green_moneyGap"),
            "carbonGap": claim_in(optimized, found["carbonGap"], "green_carbonGap"),
        }

    def _route_check(self, session: dict[str, Any], vendor_id: str) -> dict[str, Any] | None:
        """④ 수거 노선 확인 한 줄. 업체가 밝힌 수거 상태(노선 포함, 노선 밖 방문, 확인 필요, 수거 불가, 미상)."""
        try:
            haul = self._haul(session, self.store.get_vendor_profile(vendor_id))
        except NotFound:
            return None
        return haul_check(haul)

    def _pickup_view(self, session, vendor_id, item, optimized, tag) -> dict[str, Any] | None:
        """결과 한 줄의 수거·운반: 수거 상태, 운반비 방식과 부담, 견적 금액·횟수, 업체 월 운반비."""
        try:
            haul = self._haul(session, self.store.get_vendor_profile(vendor_id))
        except NotFound:
            return None
        fee = claim_in(optimized, item.get("transportFee"), f"{tag}_transportFee")
        if fee is None and haul.get("fee"):
            # 금액은 밝혔지만 횟수가 없어 월 운반비를 못 낸 경우에도 업체가 밝힌 금액은 보인다
            fee = naming.annotate({**haul["fee"], "id": f"{tag}_transportFee"}, "transportFee")
        detail = pickup_terms.route_conflict_detail(haul)
        label = pickup_terms.STATUS_LABEL.get(str(haul.get("status")))
        if detail and haul.get("status") == "check":
            label = "주소 또는 수거 가능 여부 확인"
        return {
            "status": haul.get("status"),
            "label": label,
            **({"detail": detail} if detail else {}),
            "days": list(haul.get("days") or []),
            "basis": haul.get("basis"),
            "basisLabel": pickup_terms.BASIS_LABEL.get(str(haul.get("basis"))),
            "routeBasis": haul.get("routeBasis"),
            "paidBy": haul.get("paidBy"),
            "unit": item.get("transportUnit") or haul.get("unit"),
            "fee": fee,
            "pickups": claim_in(optimized, item.get("transportPickups"), f"{tag}_transportPickups"),
            "monthly": claim_in(optimized, item.get("transportMonthly"), f"{tag}_transportMonthly"),
            "room": claim_in(optimized, item.get("transportRoom"), f"{tag}_transportRoom"),
            "roomAgainst": item.get("transportRoomAgainst"),
            "pending": bool(item.get("transportPending")),
            "seq": haul.get("seq"),
            "source": haul.get("source"),
        }

    def _process_of(self, vendor_id: str | None, stream: dict[str, Any]) -> str | None:
        """받는 곳의 처리 방식. 업체면 프로필의 process, 지금 방식 그대로면 현행 처리(없으면 혼합 소각 가정)."""
        if not vendor_id:
            return str((stream.get("currentTreatment") or {}).get("method") or "혼합 소각")
        try:
            process = self.store.get_vendor_profile(vendor_id).get("process")
        except NotFound:
            return None
        return str(process) if process else None

    def _regulation(self, work: dict[str, Any], streams: list[dict[str, Any]]) -> list[dict]:
        """check_requirements 결과를 화면용으로. 조항은 원문 인용 claim이고 근거 패널에서 법령이 열린다."""

        def clause(claim: dict[str, Any]) -> dict[str, Any]:
            law_id = claim["evidence"][0]
            try:
                label = self.registry.get(law_id, None).label
            except EvidenceNotFound:
                label = None
            return {**claim, **({"label": label} if label else {})}

        def section(result: dict[str, Any] | None) -> dict[str, Any] | None:
            if not result:
                return None
            output = result.get("output") or {}
            if "related" not in output:  # 체크리스트 전의 결과
                return None
            claims = {c["id"]: c for c in result.get("claims") or []}
            return {
                "route": output.get("route"),
                "status": result.get("status"),
                "items": [
                    {
                        **{
                            k: item[k]
                            for k in ("key", "title", "applies", "action", "status", "note")
                        },
                        "kind": "check" if item["decidedBy"] == "permit_match" else "todo",
                        # 받는 업체의 허가 확인 상태(배정 뒤 판정). 결과표·승인 카드와 같은 값이다
                        "permit": item.get("permit"),
                        "clauses": [clause(claims[c]) for c in item["claims"] if c in claims],
                    }
                    for item in output.get("items") or []
                ],
                "related": [
                    clause(claims[r["claim"]])
                    for r in output.get("related") or []
                    if r["claim"] in claims
                ],
            }

        out = []
        for stream in streams:
            sid = stream["id"]
            main = section(work["requirements"].get(sid))
            if main is None:
                continue
            reuse = section((work.get("reuseRequirements") or {}).get(sid))
            out.append({"streamId": sid, **main, "reuse": reuse})
        return out

    def _improvements(self, org_id, streams, assignments, vendor_ids) -> list[dict[str, Any]]:
        """폐기물마다 무엇을 하면 어느 업체가 얼마에 더 받는지(improve.py). 실패해도 결과 한 장은 낸다."""
        try:
            return improve.improvements(
                self.registry, self.store, org_id, streams, assignments, vendor_ids, self.today
            )
        except Exception:  # 안내 계산 때문에 결과가 막히면 안 된다
            logger.warning("분리 개선 안내를 만들지 못했다", exc_info=True)
            return []

    def _assumptions(self, evidence_ids: list[str], org_id: str) -> list[dict[str, Any]]:
        """결과에 쓰인 가정값. 화면이 ID 대신 이름과 값을 보여 주게 {id, label, value, unit}로 준다."""
        items = []
        for evidence_id in evidence_ids:
            try:
                found = self.registry.get(evidence_id, org_id)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
            value = found.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                value = None  # 등급 기준표처럼 값 하나로 적을 수 없는 가정
            items.append(
                {
                    "id": found.id,
                    "label": found.label or found.id,
                    "value": value,
                    "unit": found.unit if value is not None else None,
                }
            )
        return items

    @staticmethod
    def _thread_claims(thread: Thread) -> list[dict[str, Any]]:
        return [c.to_dict() for m in thread.messages for c in m.claims]

    # ── 승인, 확정, 문서 ──────────────────────────────────────────────
    def approve(self, session_id: str, thread_id: str, org_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        try:
            thread = Thread.from_dict(self.store.get_thread(thread_id))
        except KeyError:
            raise OrchestratorError("THREAD_NOT_FOUND", "그런 대화가 없습니다.", 404) from None
        if thread.session_id != session_id or org_id not in (thread.emitter_org, thread.vendor_org):
            raise OrchestratorError("THREAD_NOT_FOUND", "그런 대화가 없습니다.", 404)
        if thread.status == "confirmed":
            return session  # 이미 확정됐다. 다시 눌러도 달라지지 않는다
        if thread.status != "agreed":
            raise OrchestratorError("NOT_AGREED", "합의된 조건이 있는 대화만 승인할 수 있습니다.")
        failed, evidence = self._permit_failures(
            session, thread, sorted(session["work"]["assigned"].get(thread.id) or thread.stream_ids)
        )
        if failed:
            # 합의한 뒤에 업체의 허가 품목이 바뀌어 맞지 않게 됐다. 확정하지 않는다. 맞는 흐름이 남지
            # 않으면 근거와 함께 닫고, 일부만 맞지 않으면 그 흐름만 빼고 승인을 다시 받는다
            done = self._drop_for_permit(session, thread, failed, evidence)
            self._save(session)
            raise OrchestratorError(
                "PERMIT_MISMATCH",
                "업체의 허가 품목이 폐기물과 맞지 않게 되어 승인하지 않고 대화를 닫았습니다."
                if done == "closed"
                else "업체의 허가 품목이 일부 폐기물과 맞지 않게 되어 그 폐기물만 뺐습니다. "
                "남은 조건을 확인하고 다시 승인해 주세요.",
            )
        side = thread.side_of(org_id)
        held = [p for p in self.permit_states(session, thread) if p["status"] != "met"]
        if held and (side == "emitter" or thread.approvals.get("emitter")):
            # 허가증 원문 대조가 끝나지 않은 경로는 확정하지 않는다. 배출 기업의 승인(과 확정이 될 승인)을 막는다.
            # 업체는 먼저 승인해 둘 수 있다. 근거 검증 실패(validation)와 섞이지 않게 승인 칸을 막지 않고 사유만 돌려준다
            vendor_name = org_label(self.store.get_org(thread.vendor_org).get("name"), "업체")
            raise OrchestratorError("PERMIT_UNVERIFIED", self._hold_reason(held, vendor_name))
        blocked = self._verify(session, thread)
        if blocked:
            self._save(session)
            codes = {item.rsplit(": ", 1)[-1] for item in blocked}
            raise OrchestratorError(
                "APPROVAL_BLOCKED",
                "합의한 뒤에 업체의 수거·운반 조건(노선, 수거 가능 지역, 운반비)이 바뀌어 승인할 수 없습니다. "
                "진단을 다시 진행하면 새 조건으로 운반비와 한 달 손익을 다시 계산합니다."
                if codes == {"PICKUP_TERMS_CHANGED"}
                else "합의한 뒤에 업체의 조건이 바뀌어 승인할 수 없습니다. 진단을 다시 진행하면 "
                "새 조건으로 대화를 이어 갑니다."
                if codes & REOPEN_CODES
                else "근거 확인에 실패한 항목이 있어 승인할 수 없습니다.",
            )
        # 계약 검토 담당(agents/reviewer.py)이 막은 항목이 있으면 어느 쪽도 승인할 수 없다. 사람이 승인하기 전의
        # 마지막 안전장치다(2026-11-19 멘토링: 아무도 써 보지 않아 단점이 보이지 않으니 최소한의 안전장치)
        stops = reviewer.blocked_titles(self.contract_review(session, thread))
        if stops:
            raise OrchestratorError(
                "REVIEW_BLOCKED",
                f"계약 검토 담당이 막은 항목이 있어 승인할 수 없습니다: {', '.join(stops)}.",
            )
        both = thread.approve(org_id)
        self.store.set_approval(session_id, thread_id, side, True)
        self.store.put_thread(thread)
        self._event(
            session_id,
            "system",
            "approval",
            f"{self.store.get_org(org_id).get('name')} 담당자가 승인했습니다.",
            thread_id,
            side=side,
            orgId=org_id,
            both=both,
        )
        self.feed.note(
            session_id,
            f"{'회사' if side == 'emitter' else '업체'} 측 담당자가 승인했습니다.",
            stage=6,
            thread=thread,
            org_id=org_id,
            shared=True,
        )
        if both:
            self._confirm(session, thread)
        agreed = [t for t in self._threads(session_id) if t.id in session["work"]["assigned"]]
        if agreed and all(t.status == "confirmed" for t in agreed):
            session["status"], session["phase"] = "confirmed", "done"
        elif not agreed:
            session["status"], session["phase"] = "idle", "done"
        self._save(session)
        return session

    def contract_review(self, session: dict[str, Any], thread: Thread) -> dict[str, Any]:
        """계약 검토 담당의 검토(agents/reviewer.py). 합의한 대화 하나의 조건을 결과 한 장과 다시 대조한다.

        수거 상태(노선, 수거 가능 지역·최대 거리, 노선 밖 조건)는 결과 한 장을 만든 뒤에 업체가 바꿨을 수 있어 지금
        프로필로 다시 본다(judge/pickup.py. 결과 한 장의 "수거 노선" 확인 항목과 같은 판단이다).
        """
        work = session["work"]
        result = work.get("result") or {}
        rows = []
        for row in result.get("assignments") or []:
            if row.get("threadId") != thread.id:
                continue
            # 수거 상태(노선, 수거 가능 지역·최대 거리, 노선 밖 조건)는 지금 업체 프로필로 다시 정한다
            checks = [c for c in row.get("checks") or [] if c.get("key") != "pickup_route"]
            try:
                profile = self.store.get_vendor_profile(thread.vendor_org)
            except NotFound:
                rows.append({**row, "checks": checks})
                continue
            area = haul_check(self._profile_haul(session, profile))
            rows.append({**row, "checks": [*checks, area]})
        try:
            guard = self.store.get_vendor_profile(thread.vendor_org).get("guardrails") or {}
        except NotFound:
            guard = {}
        approval = next(
            (a for a in self.store.list_approvals(session["id"]) if a["threadId"] == thread.id),
            None,
        )
        earlier = sum(
            1
            for t in self.store.list_threads(org_id=thread.emitter_org)
            if t.get("vendorOrgId") == thread.vendor_org
            and t.get("status") == "confirmed"
            and t.get("sessionId") != session["id"]
        )
        names: dict[str, str] = {}
        for row in rows:
            try:
                names[row["streamId"]] = stream_label(self.store.get_stream(row["streamId"]))
            except NotFound:
                continue
        return reviewer.review(
            rows=rows,
            offers=work["offers"].get(thread.id) or {},
            guardrails=guard,
            regulation=result.get("regulation"),
            approval=approval,
            earlier_deals=earlier,
            stream_names=names,
        )

    def _announce_reviews(self, session) -> None:
        """승인을 기다리는 대화마다 계약 검토 담당의 검토를 대화 기록에 남긴다(양쪽이 본다).

        결과가 앞서 남긴 것과 같으면 다시 쓰지 않는다. 화면의 승인 상자는 뷰를 만들 때마다 검토를 새로 받는다.
        """
        work = session["work"]
        seen = work.setdefault("reviews", {})
        for thread in self._threads(session["id"]):
            if thread.status != "agreed" or thread.id not in work["assigned"]:
                continue
            found = self.contract_review(session, thread)
            mark = ids.digest(
                {
                    "status": found["status"],
                    "items": [[i["key"], i["status"], i["text"]] for i in found["items"]],
                }
            )
            if seen.get(thread.id) == mark:
                continue
            seen[thread.id] = mark
            self.feed.activity(
                session["id"],
                "review",
                found["summary"],
                stage=6,
                actor=reviewer.ACTOR,
                thread=thread,
                org_id=thread.emitter_org,
                shared=True,
                review={
                    "status": found["status"],
                    "items": [
                        {k: item[k] for k in ("key", "title", "status", "text")}
                        for item in found["items"]
                    ],
                },
            )
            self._event(
                session["id"],
                reviewer.ACTOR,
                "review",
                found["summary"],
                thread.id,
                status=found["status"],
                blocked=reviewer.blocked_titles(found),
            )

    def _confirm(self, session, thread: Thread) -> None:
        """양쪽 승인 뒤에만 confirm을 보내고 요청서와 이행 계획서를 만든다."""
        work = session["work"]
        assigned = list(work["assigned"].get(thread.id) or [])
        offers = work["offers"].get(thread.id) or {}
        streams = [self.store.get_stream(sid) for sid in thread.stream_ids]
        profile = self.store.get_vendor_profile(thread.vendor_org)
        agreed = [
            {
                "streamId": sid,
                "unitPrice": safe_id(f"m{offers[sid]['seq']}", offers[sid]["claim"]["id"]),
                "monthlyKg": safe_id(f"{sid}_monthlyKg"),
            }
            for sid in assigned
            if sid in offers
        ]
        with self.feed.turn(session["id"], thread, "company", 6):
            outcome, trace = self._company(session).compose(
                thread,
                "confirm",
                streams=streams,
                vendor=self._public(session, profile),
                vendor_org=self.store.get_org(thread.vendor_org),
                memo=work["threadTools"].setdefault(thread.id, []),
                state={"offered": sorted(offers), "agreed": agreed},
            )
        self._send(session, thread, outcome, trace, stage=6)
        if thread.status != "confirmed":
            return
        # 이행 계획서에 쓸 적재 값은 실제로 배정된 흐름만 합쳐 다시 계산한다
        _, loading = self._company(session).run_tools(
            [("check_loading", {"streamIds": sorted(assigned)})],
            streams=streams,
            vendor=self._public(session, profile),
            memo=work["threadTools"][thread.id],
        )
        self._flush(session["id"], loading, thread.id, thread.emitter_org)
        self._event(
            session["id"],
            "system",
            "thread",
            "양쪽 승인으로 확정했습니다.",
            thread.id,
            status="confirmed",
        )
        self.feed.note(
            session["id"],
            "양쪽 승인으로 확정했습니다.",
            stage=6,
            thread=thread,
            org_id=thread.emitter_org,
            shared=True,
        )
        for document in self._documents(session, thread, assigned):
            result = validate_text(
                document["text"],
                document["claims"],
                self.registry,
                org_id=thread.emitter_org,
                today=self.today,
                allowed_tokens=(*ALLOWED_TOKENS, *name_tokens(thread_names(self.store, thread))),
            )
            self._event(
                session["id"],
                "validator",
                "validation",
                f"{document['title']} 근거 확인 " + ("통과" if result.ok else "돌려보냄"),
                thread.id,
                ok=result.ok,
                codes=list(result.codes),
                document=document["kind"],
            )
            if not result.ok:
                continue  # 검증을 통과하지 못한 문서는 만들지 않는다
            for claim in document["claims"]:
                for evidence_id in claim["evidence"]:
                    share_evidence(self.registry, evidence_id, thread.vendor_org)
            document.update(
                id=self.store.next_id("doc"),
                validation=result.to_dict(),
                rendered=render_text(document["text"], document["claims"]),
                createdAt=self.store.now(),
            )
            self.store.put_document(session["id"], document)
            self._event(
                session["id"],
                "system",
                "document",
                f"{josa(document['title'], '을를')} 만들었습니다.",
                thread.id,
                documentId=document["id"],
                document=document["kind"],
            )
            self.feed.note(
                session["id"],
                f"{josa(document['title'], '을를')} 만들었습니다.",
                stage=6,
                thread=thread,
                org_id=thread.emitter_org,
                shared=True,
            )

    def _documents(self, session, thread: Thread, assigned: list[str]) -> list[dict[str, Any]]:
        """요청서와 이행 계획서. 숫자는 대화와 도구에서 이미 검증된 claim만 쓴다."""
        work = session["work"]
        memo = work["threadTools"].get(thread.id) or []
        offers = work["offers"].get(thread.id) or {}
        emitter = self.store.get_org(thread.emitter_org)
        vendor = self.store.get_org(thread.vendor_org)
        streams = {sid: self.store.get_stream(sid) for sid in assigned}
        loading = memo_result(memo, "check_loading", streamIds=sorted(assigned))

        def build(kind: str, title: str, lines_for, footer: str | None = None) -> dict[str, Any]:
            claims: list[dict[str, Any]] = []

            def use(claim: dict[str, Any] | None, claim_id: str) -> str | None:
                if not claim or not claim.get("evidence"):
                    return None
                if all(c["id"] != claim_id for c in claims):
                    claims.append({**claim, "id": claim_id})
                return "{{" + claim_id + "}}"

            lines = [
                title,
                # 조직 ID와 대화 ID는 본문에 적지 않는다(문서의 threadId 항목에 있다). 사람이 읽는 글이다
                f"보내는 곳: {org_label(emitter.get('name'), emitter.get('id') or '')}",
                f"받는 곳: {org_label(vendor.get('name'), vendor.get('id') or '')}",
                f"작성일: {self.today}",
                "",
                *lines_for(use),
                "",
                footer or "이 문서는 앱 안에서만 쓰는 문서이며 계약서나 관청 제출 서류가 아닙니다.",
            ]
            return {
                "kind": kind,
                "threadId": thread.id,
                "title": f"{title} · {org_label(vendor.get('name'), vendor.get('id') or '')}",
                "text": "\n".join(lines),
                "claims": claims,
                "streamIds": list(assigned),
            }

        def stream_facts(sid: str) -> dict[str, Any]:
            stream = streams[sid]
            evidence = list(stream.get("monthlyKgEvidence") or [])
            kg = None
            if stream.get("monthlyKg") is not None and evidence:
                kg = naming.annotate(
                    {
                        "id": "kg",
                        "kind": "number",
                        "value": stream["monthlyKg"],
                        "unit": "kg/월",
                        "evidence": evidence,
                    },
                    "monthlyKg",
                )
            return {"stream": stream, "kg": kg, "offer": offers.get(sid)}

        def request_lines(use) -> list[str]:
            lines = ["아래 폐기물의 수거와 처리를 요청합니다."]
            for number, sid in enumerate(assigned, 1):
                facts, tag = stream_facts(sid), safe_id(sid)
                stream, offer = facts["stream"], facts["offer"]
                parts = [
                    f"{stream.get('wasteItem')}, {stream.get('material')}, {stream.get('form') or '형태 확인 필요'}"
                ]
                kg = use(facts["kg"], f"{tag}_monthlyKg")
                parts.append(f"월 발생량 {kg}" if kg else "월 발생량 확인 필요")
                price = use(offer["claim"], f"{tag}_unitPrice") if offer else None
                fee = bool(offer) and offer["unitPriceKrwPerKg"] < 0
                agreed = "합의 처리비" if fee else "합의 단가"  # 처리비는 크기로 적힌다(render)
                parts.append(f"{agreed} {price}" if price else "단가 확인 필요")
                route = memo_result(memo, "judge_route", streamId=sid)
                after = use(claim_in(route, "monthlyAfter", "x"), f"{tag}_monthlyAfter")
                if after:
                    parts.append(f"월 예상 금액 {after}(운송비 제외)")
                # 문서는 업체와 함께 본다. 흐름은 보이는 이름으로 적는다(업체 조건의 머리도 같다)
                label = stream_label(stream)
                lines.append(f"{number}. {label}: {' / '.join(parts)}")
                for text in (offer or {}).get("conditions") or []:
                    head = f"{label}:"
                    if text.startswith(head):
                        own = text[len(head) :].strip()
                        lines.append(f"   조건: {self._with_claims(own, thread, use)}")
            days = next(
                (offers[s]["days"] for s in assigned if s in offers and offers[s]["days"]), []
            )
            if days:
                lines.append(f"수거 요일: {', '.join(days)} 가운데 협의")
            lines.append("양쪽 담당자가 앱에서 승인한 조건입니다.")
            return lines

        def plan_lines(use) -> list[str]:
            lines = ["확정한 조건을 다음과 같이 이행합니다."]
            pickups = use(claim_in(loading, "pickupsPerMonth", "x"), "pickupsPerMonth")
            per_kg = use(claim_in(loading, "perPickupKg", "x"), "perPickupKg")
            lines.append(f"수거 횟수: {pickups}" if pickups else "수거 횟수: 확인 필요")
            if per_kg:
                lines.append(f"한 번 수거량(확정한 폐기물을 합친 값): {per_kg}")
            for number, sid in enumerate(assigned, 1):
                facts, tag = stream_facts(sid), safe_id(sid)
                stream = facts["stream"]
                kg = use(facts["kg"], f"{tag}_monthlyKg")
                label = stream_label(stream)  # 문서는 업체와 함께 본다(보이는 이름)
                lines.append(
                    f"{number}. {label}: 월 {kg} 반출"
                    if kg
                    else f"{number}. {label}: 반출 물량 확인 필요"
                )
                # 문서는 업체와 함께 본다. 배출 기업의 현행 처리 방법이나 그것에 견준 값(탄소 변화,
                # 현행 대비 차이)은 싣지 않는다. 그 값은 배출 기업만 보는 결과 한 장에 있다
                route = memo_result(memo, "judge_route", streamId=sid)
                for check in (route or {}).get("checks") or []:
                    if check["status"] == "conditional" and check["key"] not in CURRENT_CHECKS:
                        lines.append(f"   반입 전 확인: {check['text']}")
            lines.append("반입 검수 결과가 조건과 다르면 앱에서 다시 협의합니다.")
            lines.extend(contract_lines(use))
            lines.extend(law_lines(use))
            return lines

        def law_lines(use) -> list[str]:
            """③ 규정 확인의 요건 체크리스트. 요건마다 첫 조항을 원문 그대로 인용한다(법령은 공개 근거라 업체와 함께 본다)."""
            words = {"met": "충족", "unmet": "불충족", "needs_information": "확인 필요"}
            out: list[str] = []
            seen: set[str] = set()
            for sid in assigned:
                required = work["requirements"].get(sid) or {}
                claims = {c["id"]: c for c in required.get("claims") or []}
                for item in (required.get("output") or {}).get("items") or []:
                    if "key" not in item or item["key"] in seen:
                        continue
                    seen.add(item["key"])
                    first = next((claims[c] for c in item["claims"] if c in claims), None)
                    quote = use(first, f"law_{safe_id(item['key'])}") if first else None
                    state = (
                        words.get(item["status"], "확인 필요")
                        if item["decidedBy"] == "permit_match"
                        else "담당자 할 일"
                    )
                    line = f"- {item['title']}: {state}"
                    out.append(f"{line}. 근거 {quote}" if quote else line)
            return ["", "지켜야 할 법령 요건", *out] if out else []

        def contract_lines(use) -> list[str]:
            """단가와 계약 조건. 2026-11-19 배출 사업장 설문의 "단가 상호불신으로 장기 계약이 어렵다"에 답한다:
            단가를 공개 시세에 묶고, 검수 기록을 양쪽이 함께 보고, 바꿀 때는 미리 알린다."""
            org = thread.emitter_org
            out = ["", "단가와 계약 조건"]
            band = basis.resolve(self.registry, org, "contract.priceBandPct")
            band_claim = (
                {
                    "id": "x",
                    "kind": "number",
                    "value": band.value,
                    "unit": band.unit,
                    "evidence": [band.id],
                }
                if band is not None and isinstance(band.value, (int, float))
                else None
            )
            moved = use(band_claim, "contractPriceBand") if band_claim else None
            materials = sorted({str(streams[sid].get("material") or "") for sid in assigned} - {""})
            # 재질마다 시세 하나(폐기물에 가까운 형태 순: 압축품, 플레이크, 펠렛)
            order = {"압축": 0, "플레이크": 1, "펠렛": 2}
            picked: dict[str, dict[str, Any]] = {}
            for found in basis.reference_prices(self.registry, org, materials):
                best = picked.get(found["material"])
                if best is None or order.get(found["form"], 9) < order.get(best["form"], 9):
                    picked[found["material"]] = found
            for found in picked.values():
                evidence = found["evidence"]
                if not isinstance(evidence.value, (int, float)):
                    continue
                price = use(
                    {
                        "id": "x",
                        "kind": "number",
                        "value": evidence.value,
                        "unit": evidence.unit,
                        "evidence": [evidence.id],
                    },
                    f"reference_{safe_id(found['key'])}",
                )
                out.append(f"- 합의할 때의 기준 시세({evidence.label}): {price}")
            if moved:
                out.append(
                    f"- 기준 시세가 합의할 때보다 {moved} 넘게 오르거나 내리면 단가를 다시 협의합니다."
                )
            out.append(
                "- 반입 검수에서 오염도나 수분이 합의한 매입 기준의 상한을 넘으면 그 업체 매입 기준표의 다음 등급 단가를 따릅니다."
            )
            out.append("- 검수할 때마다 무게·등급·단가를 앱에 기록하고, 양쪽이 같은 기록을 봅니다.")
            out.append(
                "- 어느 쪽이든 조건을 바꾸려면 한 달 전에 앱에서 알리고, 그동안은 지금 조건을 지킵니다."
            )
            return out

        review = self.contract_review(session, thread)
        pilot = any(i["key"] == "pilot" and i.get("pilot") for i in review["items"])
        rows = {
            row["streamId"]: row
            for row in (work.get("result") or {}).get("assignments") or []
            if row.get("threadId") == thread.id
        }

        def draft_lines(use) -> list[str]:
            """위탁 계약서 초안(계약 검토 담당이 쓴다). 조항 머리는 번호 목록이고 숫자는 근거가 붙은 값만 쓴다."""
            heads: list[str] = []

            def head(text: str) -> list[str]:
                heads.append(text)
                return ["", f"{len(heads)}. {text}"]

            lines = [
                (
                    "보내는 곳(위탁자)과 받는 곳(수탁자)은 양쪽 담당자가 앱에서 승인한 조건으로 아래 폐기물의 "
                    "수거와 처리를 약속합니다."
                ),
                *head("대상 폐기물과 물량"),
            ]
            for sid in assigned:
                facts, tag = stream_facts(sid), safe_id(sid)
                stream = facts["stream"]
                kg = use(facts["kg"], f"{tag}_monthlyKg")
                kind = f"{stream.get('material')}, {stream.get('form') or '형태 확인 필요'}"
                amount = f"{kg} 안팎" if kg else "월 물량 확인 필요"
                lines.append(f"- {stream_label(stream)}({kind}): {amount}")
            lines += head("단가와 정산")
            for sid in assigned:
                facts, tag = stream_facts(sid), safe_id(sid)
                offer = facts["offer"]
                price = use(offer["claim"], f"{tag}_unitPrice") if offer else None
                fee = bool(offer) and offer["unitPriceKrwPerKg"] < 0
                word = "처리비(보내는 곳이 냄)" if fee else "매입 단가(받는 곳이 줌)"
                shown = f"{word} {price}" if price else "단가 확인 필요"
                lines.append(f"- {stream_label(facts['stream'])}: {shown}")
            lines.append(
                "- 정산은 반입 검수에서 잰 실제 무게로 하고, 검수할 때마다 무게·등급·단가를 앱에 기록해 "
                "양쪽이 같은 기록을 봅니다."
            )
            lines += head("수거와 운반")
            days = next(
                (offers[s]["days"] for s in assigned if s in offers and offers[s]["days"]), []
            )
            lines.append(
                f"- 수거 요일: {', '.join(days)} 가운데 협의" if days else "- 수거 요일: 협의"
            )
            pickups = use(claim_in(loading, "pickupsPerMonth", "x"), "pickupsPerMonth")
            if pickups:
                lines.append(f"- 한 달 수거 횟수: {pickups}")
            payers = {(rows.get(sid) or {}).get("transportPaidBy") for sid in assigned}
            if payers == {"vendor"}:
                lines.append("- 운반비: 받는 곳이 부담합니다(단가에 포함).")
            elif payers == {"emitter"}:
                lines.append("- 운반비: 보내는 곳이 부담합니다.")
            else:
                lines.append("- 운반비: 부담 주체를 정해 적어야 합니다(확인 필요).")
            where = next((i for i in review["items"] if i["key"] == "pickup"), None)
            if where:
                lines.append(f"- 수거 가능 지역: {where['text']}")
            lines += head("반입 검수와 등급")
            lines.append(
                "- 반입 검수에서 오염도나 수분이 합의한 매입 기준의 상한을 넘으면 받는 곳 매입 기준표의 "
                "다음 등급 단가를 따릅니다."
            )
            lines.append(
                "- 검수 결과가 조건과 다르면 앱에서 다시 협의하고, 합의 전까지는 지금 조건을 지킵니다."
            )
            lines += head("허가와 법령")
            lines.append("- 받는 곳은 허가받은 업종과 품목 안에서만 이 폐기물을 처리합니다.")
            lines.append(
                "- 보내는 곳은 위탁하기 전에 받는 곳의 허가증에 이 폐기물과 처리 방법이 들어 있는지 확인합니다."
            )
            permit = next((i for i in review["items"] if i["key"] == "permit"), None)
            if permit and permit["status"] != "ok":
                lines.append(f"- 확인할 것: {permit['text']}")
            if pilot:
                lines += head("시범 기간")
                lines.append("- 두 회사가 처음 거래하므로 첫 한 달을 시범 기간으로 합니다.")
                lines.append(
                    "- 첫 반입 검수 기록을 양쪽이 앱에서 확인한 뒤에 다음 달 수거를 이어 갑니다."
                )
                lines.append(
                    "- 시범 기간에는 어느 쪽이든 앱에서 알리고 바로 거래를 멈출 수 있습니다."
                )
            terms = contract_lines(use)
            lines += head("단가 조정과 변경")
            lines += terms[2:]
            lines += head("계약 검토 담당의 검토")
            lines.append(f"- {review['summary']}")
            for item in review["items"]:
                # 법령 요건은 바로 아래에 조항과 함께 적는다
                if item["status"] != "ok" and item["key"] != "law":
                    lines.append(f"- {item['title']}: {item['text']}")
            lines.extend(law_lines(use))
            return lines

        return [
            build("request", "수거·처리 요청서", request_lines),
            build("plan", "이행 계획서", plan_lines),
            build(
                "contract",
                "위탁 계약서 초안",
                draft_lines,
                "이 초안은 양쪽 담당자가 앱에서 승인한 조건과 계약 검토 담당의 검토로 만들었습니다. "
                "서명하기 전에 양쪽의 법무 검토와 허가 기관 확인이 필요하며, 관청 제출 서류가 아닙니다.",
            ),
        ]

    def _with_claims(self, text: str, thread: Thread, use) -> str:
        """조건 문장의 자리표시자를 문서 claim으로 옮긴다. 대화에서 검증된 claim만 쓴다."""
        known = {c["id"]: c for c in self._thread_claims(thread)}

        def swap(match) -> str:
            claim = known.get(match.group(1))
            return use(claim, match.group(1)) or "확인 필요"

        return PLACEHOLDER.sub(swap, text)

    # ── 업로드 문서의 지시문 주입 ─────────────────────────────────────
    def handoff_for_injection(self, org_id: str, filename: str) -> list[str]:
        """주입이 탐지된 문서를 올린 조직의 진행 중인 대화를 모두 담당자에게 넘긴다."""
        closed: list[str] = []
        again: list[str] = []
        for raw in self.store.list_threads(org_id=org_id):
            thread = Thread.from_dict(raw)
            if thread.status not in ("open", "agreed"):
                continue
            session = self.store.get_session(thread.session_id)
            self._event(
                session["id"],
                "system",
                "handoff",
                "업로드 문서에서 지시문 주입이 탐지됐습니다.",
                thread.id,
                orgId=org_id,
                file=filename,
            )
            if self._handoff(
                session,
                thread,
                org_id,
                "업로드한 문서에서 지시문 주입이 탐지되어 이 대화를 담당자에게 넘깁니다.",
                stage=4,
                reassign=False,
            ):
                again.append(session["id"])
            self._save(session)
            closed.append(thread.id)
        for session_id in dict.fromkeys(again):  # 진단마다 한 번만 다시 계산한다
            session = self.store.get_session(session_id)
            self._reassign(session)
            self._save(session)
        return closed

    # ── 탈퇴한 업체 ───────────────────────────────────────────────────
    def close_for_departed(self, vendor_org_id: str, reason: str) -> list[str]:
        """탈퇴한 업체와 다른 배출 기업 사이의 대화를 닫는다. 닫은 대화 ID를 준다.

        진행 중이거나 합의한 대화는 코드가 쓴 handoff(배출 기업 쪽, 숫자 없는 reason)로 닫고 승인과
        배정을 지운 뒤 최적안을 다시 계산한다(handoff_for_injection과 같다). 아직 열지 않은 대화는 열지
        않기로 하고 같은 이유를 남긴다. 확정한 대화, 이미 닫힌 대화는 그대로 둔다(상대의 기록이다).
        """
        closed: list[str] = []
        again: list[str] = []
        for raw in self.store.list_threads(org_id=vendor_org_id):
            thread = Thread.from_dict(raw)
            if thread.vendor_org != vendor_org_id:
                continue
            if thread.status == "not_opened" and not thread.blocked and not thread.messages:
                thread.refuse_open(reason)
                self.store.put_thread(thread)
                closed.append(thread.id)
                continue
            if thread.status not in ("open", "agreed"):
                continue
            session = self.store.get_session(thread.session_id)
            if self._handoff(session, thread, thread.emitter_org, reason, stage=4, reassign=False):
                again.append(session["id"])
            self._save(session)
            closed.append(thread.id)
        for session_id in dict.fromkeys(again):  # 진단마다 한 번만 다시 계산한다
            session = self.store.get_session(session_id)
            self._reassign(session)
            self._save(session)
        return closed


def vendor_model(app: Any, session: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """뷰에 붙일 업체 측 모델 표시. 실제로 답을 쓴 구현이 있으면 그것을, 없으면 쓰게 될 구현을 적는다."""
    configured = dict(profile.get("model") or {})
    used = (session.get("work") or {}).get("vendorModels", {}).get(profile["orgId"])
    if used:
        return {**used, "configured": configured, "used": True}
    llm = app.llm_for("vendor", profile["orgId"], configured or None)
    return {**model_label(llm), "configured": configured, "used": False}
