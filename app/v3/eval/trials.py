"""반복 실행기(평가 설계 7장 L3, 8장 L4). 사례마다 n번 돌려 pass^k와 초안 단위 기록을 낸다.

    run_trials(cases, repeats=4, llm_factory=None) -> {"trials": [...], "cases": [...], "meter": {...}}
    l4_layer(outcome) / l3_layer(outcome) -> 리포트용 층 결과(stats.layer)

- 한 번의 실행(trial)은 runner.run_case 그대로다. 성공 r = r_상태 × r_정책
  (verdictMatch이고 forbiddenViolations가 비었다).
- 초안 단위 기록은 실행 기록(events)의 validation 줄에서 읽는다. 차례(나가야 할 글 한 자리)마다
  시도 수, 거부 사유 코드, 첫 시도 통과, 재작성 뒤 통과, 인계(handoff)를 남긴다.
- llm_factory가 있으면 감싸서 호출 수, 입력·출력 토큰(모델 응답의 usage), 오류를 센다. 규칙 정책
  (llm_factory=None)은 모델을 부르지 않으므로 토큰이 없다.
- budget_calls에 닿으면 새 실행을 시작하지 않고 남은 칸을 "예산 초과로 미측정"으로 둔다. 진행 중인
  실행은 끝까지 돈다(실행 도중에 끊으면 결과가 망가진다).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from app.v3.agents.draft import MAX_ATTEMPTS
from app.v3.agents.llm import LLMError
from app.v3.eval import stats
from app.v3.eval.runner import run_case

MAX_TURNS = 12
NUMBER_CODES = {"NUMBER_OUTSIDE_PLACEHOLDER"}
EVIDENCE_CODES = {
    "UNKNOWN_CLAIM_REF",
    "UNKNOWN_PLACEHOLDER",
    "EVIDENCE_NOT_FOUND",
    "EVIDENCE_FORBIDDEN",
}
QUOTE_CODES = {"QUOTE_MISMATCH"}
# 잘못된 행동(AgentBench의 IA): 보낼 수 없는 종류, 없는 흐름, 허용 밖 도구, 규칙 밖 메시지
ACTION_CODES = {
    "TYPE_NOT_ALLOWED",
    "UNKNOWN_STREAM",
    "TOO_MANY_TOOL_ROUNDS",
    "APPROVAL_REQUIRED",
    "THREAD_RULE",
    "PERMIT_MISMATCH",
}
_CONTEXT_WORDS = ("context_length", "maximum context", "context window", "too many tokens")
FAILURE_TYPES = (
    "실행 오류",
    "문맥 초과",
    "형식 실패",
    "잘못된 행동",
    "턴 상한 초과",
    "판정 불일치",
    "정책 위반",
)
BUDGET_SKIPPED = "예산 초과로 미측정"


# ── 모델 호출 계량 ───────────────────────────────────────────────────


class Meter:
    """llm_factory를 감싸 호출 수, 토큰, 걸린 시간, 오류를 센다."""

    def __init__(self, inner: Callable[..., Any] | None, budget_calls: int | None = None) -> None:
        self.inner = inner
        self.budget_calls = budget_calls
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unknown_usage = 0  # usage를 주지 않은 응답 수
        self.errors: list[str] = []
        self.seconds = 0.0

    @property
    def factory(self) -> Callable[..., Any] | None:
        if self.inner is None:
            return None

        def make(role: str, org_id: str, model: dict[str, Any] | None = None) -> Any:
            llm = self.inner(role, org_id, model)
            return None if llm is None else MeteredLLM(llm, self)

        return make

    def over_budget(self) -> bool:
        return self.budget_calls is not None and self.calls >= self.budget_calls

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "unknownUsage": self.unknown_usage,
            "errors": len(self.errors),
            "seconds": round(self.seconds, 3),
        }


def _usage(response: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = response.get("usage") or {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    return (
        int(prompt) if isinstance(prompt, (int, float)) else None,
        int(completion) if isinstance(completion, (int, float)) else None,
    )


class MeteredLLM:
    """모델 클라이언트를 감싼다. provider와 model은 그대로 보여 실행 기록의 모델 이름이 같다."""

    def __init__(self, inner: Any, meter: Meter) -> None:
        self.inner = inner
        self.meter = meter
        self.provider = getattr(inner, "provider", "")
        self.model = getattr(inner, "model", "")

    def complete(self, messages, *, tools=None, response_schema=None):
        meter = self.meter
        meter.calls += 1
        started = time.perf_counter()
        try:
            response = self.inner.complete(messages, tools=tools, response_schema=response_schema)
        except LLMError as error:
            meter.errors.append(str(error)[:300])
            raise
        finally:
            meter.seconds += time.perf_counter() - started
        prompt, completion = _usage(response)
        if prompt is None and completion is None:
            meter.unknown_usage += 1
        meter.prompt_tokens += prompt or 0
        meter.completion_tokens += completion or 0
        return response


# ── 실행 기록에서 초안 단위 기록 읽기 ────────────────────────────────


def _side(event: dict[str, Any], emitter: str) -> str | None:
    actor = str(event.get("actor") or "")
    if actor.startswith("company"):
        return "company"
    if actor.startswith("vendor"):
        return "vendor"
    org = (event.get("detail") or {}).get("orgId")
    if org:
        return "company" if org == emitter else "vendor"
    return None


def drafts_from_events(events: list[dict[str, Any]], emitter: str) -> list[dict[str, Any]]:
    """validation 줄 가운데 초안 검사(detail.attempt가 있는 줄)를 차례로 묶는다.

    대화(threadId)마다 attempt가 1로 돌아오면 새 차례다. 받은 쪽의 재검증 줄(detail.recheckedBy)은
    초안이 아니므로 뺀다(rechecks_from_events가 따로 센다). 차례의 outcome은 passed(검증 통과로
    나감), decision(메시지 없는 결정: accept·ask_user), handoff(시도 상한까지 거부되어 담당자에게
    넘김), open(기록이 중간에 끊김)이다. 보낸 쪽은 통과 줄의 orgId나, 거부 줄 뒤에 같은 대화에서
    처음 나오는 에이전트 줄(actor)로 정한다.
    """
    slots: list[dict[str, Any]] = []
    current: dict[Any, dict[str, Any]] = {}
    for index, event in enumerate(events):
        detail = event.get("detail") or {}
        if event.get("kind") != "validation" or "attempt" not in detail:
            continue
        thread = event.get("threadId")
        attempt = int(detail.get("attempt") or 1)
        slot = current.get(thread)
        if slot is None or attempt == 1 or slot["outcome"] != "open":
            slot = {"threadId": thread, "side": None, "attempts": [], "outcome": "open"}
            slots.append(slot)
            current[thread] = slot
        if slot["side"] is None and (detail.get("orgId") or event.get("actor") != "validator"):
            slot["side"] = _side(event, emitter)
        if slot["side"] is None:
            for later in events[index + 1 :]:
                actor = str(later.get("actor") or "")
                if later.get("threadId") == thread and actor.endswith("_agent"):
                    slot["side"] = "company" if actor.startswith("company") else "vendor"
                    break
        codes = [str(r.get("code")) for r in detail.get("rejections") or [] if isinstance(r, dict)]
        ok = bool(detail.get("ok"))
        slot["attempts"].append(
            {
                "ok": ok,
                "codes": [] if ok else codes,
                "type": detail.get("type") or detail.get("draftType"),
                "model": (detail.get("model") or {}).get("provider"),
            }
        )
        if ok:
            pseudo = str(event.get("actor") or "") != "validator"
            slot["outcome"] = "decision" if pseudo else "passed"
        elif attempt >= MAX_ATTEMPTS:
            slot["outcome"] = "handoff"
    return slots


def rechecks_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """받은 쪽이 받은 메시지를 다시 검증한 줄. 거부되면 그 대화는 handoff로 간다."""
    rows = [
        e.get("detail") or {}
        for e in events
        if e.get("kind") == "validation" and "recheckedBy" in (e.get("detail") or {})
    ]
    failed = [r for r in rows if not r.get("ok")]
    return {
        "checked": len(rows),
        "failed": len(failed),
        "codes": [c for r in failed for c in r.get("codes") or []],
    }


def _tool_errors(events: list[dict[str, Any]]) -> int:
    return sum(
        1
        for e in events
        if e.get("kind") == "tool_call" and (e.get("detail") or {}).get("status") == "error"
    )


def _raw(session: Any) -> dict[str, Any]:
    """원 출력(평가 설계 15장): 나간 메시지와 문서, 검증 기록."""
    store = session.app.store
    threads = []
    for raw in store.list_threads(session_id=session.sid):
        threads.append(
            {
                "threadId": raw["id"],
                "vendor": raw["vendorOrgId"],
                "status": raw["status"],
                "messages": [
                    {
                        "seq": m["seq"],
                        "from": m["from"],
                        "to": m["to"],
                        "type": m["type"],
                        "text": m["text"],
                        "claims": m["claims"],
                        "model": (m.get("meta") or {}).get("model"),
                        "validation": (m.get("meta") or {}).get("validation"),
                    }
                    for m in raw["messages"]
                ],
            }
        )
    documents = [
        {"kind": d.get("kind"), "text": d.get("text"), "claims": d.get("claims")}
        for d in store.list_documents(session.sid)
    ]
    return {"threads": threads, "documents": documents}


# ── 실행 ─────────────────────────────────────────────────────────────


def _failure_type(trial: dict[str, Any], llm_errors: list[str]) -> str | None:
    """실패한 실행의 첫째 유형(평가 설계 8장, AgentBench의 CLE·IF·IA·TLE를 맞춤)."""
    if trial["success"]:
        return None
    if trial["error"]:
        return "실행 오류"
    if any(word in e.lower() for e in llm_errors for word in _CONTEXT_WORDS):
        return "문맥 초과"
    slots = trial["slots"]
    if any(s["outcome"] == "handoff" for s in slots):
        return "형식 실패"
    codes = {c for s in slots for a in s["attempts"] for c in a["codes"]}
    if codes & ACTION_CODES or trial["toolErrors"]:
        return "잘못된 행동"
    kinds = {v["kind"] for v in trial["violationItems"]}
    if "invariant:turns" in kinds or any(t >= MAX_TURNS for t in trial["turns"]):
        return "턴 상한 초과"
    if not trial["verdictMatch"]:
        return "판정 불일치"
    return "정책 위반"


def _first_rejection(slots: list[dict[str, Any]]) -> str | None:
    for slot in slots:
        for attempt in slot["attempts"]:
            if attempt["codes"]:
                return attempt["codes"][0]
    return None


def run_trial(
    case: dict[str, Any],
    options: dict[str, Any],
    *,
    meter: Meter,
    repeat: int = 0,
    keep_raw: bool = False,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def grab(session: Any) -> None:
        captured["session"] = session

    before = meter.snapshot()
    errors_before = len(meter.errors)
    started = time.perf_counter()
    result = run_case(case, options, on_session=grab)
    elapsed = time.perf_counter() - started
    after = meter.snapshot()
    session = captured.get("session")
    emitter = session.emitter if session is not None else ""
    events = session.app.store.list_events(session.sid) if session and session.sid else []
    slots = drafts_from_events(events, emitter)
    rechecks = rechecks_from_events(events)
    observed = result.get("observed") or {}
    threads = observed.get("threads") or []
    expect_q = (case.get("expect") or {}).get("questions") or {}
    trial = {
        "caseId": case["id"],
        "repeat": repeat,
        "tags": list(case.get("tags") or []),
        "open": result["open"],
        "success": bool(result["verdictMatch"] and not result["forbiddenViolations"]),
        "verdictMatch": bool(result["verdictMatch"]),
        "error": result["error"],
        "mismatches": [m["path"] for m in result["mismatches"]],
        "violations": len(result["forbiddenViolations"]),
        "violationItems": [
            {"kind": v["kind"], "why": v.get("why")} for v in result["forbiddenViolations"]
        ],
        "outgoing": {
            "checked": result["validator"]["checked"],
            "passed": result["validator"]["passed"],
            "failed": result["validator"]["failed"],
        },
        "slots": slots,
        "rechecks": rechecks,
        "toolErrors": _tool_errors(events),
        "turns": [int(t.get("turns") or 0) for t in threads if t.get("status") != "not_opened"],
        "threadStatuses": [t.get("status") for t in threads],
        "handoffs": sum(1 for t in threads if t.get("status") == "handoff"),
        "questionBatches": len({q["batch"] for q in observed.get("questions") or []}),
        "questions": {
            "asked": [[q["streamId"], q["field"]] for q in observed.get("questions") or []],
            "required": [list(x) for x in expect_q.get("asked") or []],
            "notAsked": [list(x) for x in expect_q.get("notAsked") or []],
        },
        "latencyS": round(elapsed, 4),
        "calls": after["calls"] - before["calls"],
        "promptTokens": after["promptTokens"] - before["promptTokens"],
        "completionTokens": after["completionTokens"] - before["completionTokens"],
        "llmErrors": meter.errors[errors_before:],
        "observed": {
            "assignments": observed.get("assignments") or {},
            "threads": threads,
            "result": observed.get("result") or {},
            "session": observed.get("session") or {},
        },
    }
    trial["failureType"] = _failure_type(trial, trial["llmErrors"])
    trial["firstRejection"] = _first_rejection(slots)
    if keep_raw and session is not None and session.sid:
        trial["raw"] = _raw(session)
    return trial


def run_trials(
    cases: list[dict[str, Any]],
    *,
    repeats: int = 1,
    llm_factory: Callable[..., Any] | None = None,
    registry_source: Any = None,
    distance_provider: Any = None,
    budget_calls: int | None = None,
    keep_raw: bool = False,
    progress: Callable[[str], None] | None = None,
    meter: Meter | None = None,
) -> dict[str, Any]:
    """사례마다 repeats번 돈다. 같은 사례의 반복은 한데 모아 pass^k를 낸다.

    meter를 주면 그 계량(과 예산)을 이어 쓴다. 이때 llm_factory와 budget_calls는 meter의 것이다.
    """
    if repeats < 1:
        raise ValueError("repeats는 1 이상이다")
    meter = meter or Meter(llm_factory, budget_calls)
    budget_calls = meter.budget_calls
    options = {
        "llm_factory": meter.factory,
        "registry_source": registry_source,
        "distance_provider": distance_provider,
    }
    trials: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for case in cases:
        for repeat in range(repeats):
            if meter.over_budget():
                skipped.append({"caseId": case["id"], "repeat": repeat, "reason": BUDGET_SKIPPED})
                continue
            trials.append(run_trial(case, options, meter=meter, repeat=repeat, keep_raw=keep_raw))
            if progress:
                last = trials[-1]
                progress(f"{case['id']}#{repeat + 1}: {'성공' if last['success'] else '실패'}")
    per_case = summarize_cases(trials, repeats)
    return {
        "repeats": repeats,
        "trials": trials,
        "cases": per_case,
        "skipped": skipped,
        "meter": meter.snapshot(),
        "budgetCalls": budget_calls,
    }


def summarize_cases(trials: list[dict[str, Any]], repeats: int) -> list[dict[str, Any]]:
    """사례마다 반복 n번 가운데 성공 c번. n이 repeats보다 작은 사례(예산 초과)는 complete=False."""
    by: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        by.setdefault(trial["caseId"], []).append(trial)
    out = []
    for cid, items in by.items():
        out.append(
            {
                "caseId": cid,
                "tags": items[0]["tags"],
                "open": items[0]["open"],
                "n": len(items),
                "successes": sum(1 for t in items if t["success"]),
                "complete": len(items) == repeats,
            }
        )
    return out


# ── 지표 ─────────────────────────────────────────────────────────────


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


TARGETS = {
    "pass1": stats.target(">=", 0.80),
    "pass4": stats.target(">=", 0.60),
    "violations": stats.target("==", 0, "= 0 (게이트)"),
    "requiredRecall": stats.target(">=", 0.95),
    "latencyP95": stats.target("<=", 180, "≤ 180초 (잠정)"),
    "firstTry": stats.target(">=", 0.85),
    "afterRewrite": stats.target(">=", 0.97),
    "handoff": stats.target("<=", 0.03),
    "outgoingHallucination": stats.target("==", 0, "= 0 (게이트)"),
}


def l4_metrics(
    outcome: dict[str, Any], *, resamples: int = stats.RESAMPLES
) -> list[dict[str, Any]]:
    repeats = outcome["repeats"]
    cases = [c for c in outcome["cases"] if c["complete"]]
    trials = [t for t in outcome["trials"] if any(c["caseId"] == t["caseId"] for c in cases)]
    rows: list[dict[str, Any]] = []
    for k in range(1, 5):
        key, label = f"pass^{k}", f"pass^{k}" + (" (성공률)" if k == 1 else "")
        goal = TARGETS["pass1"] if k == 1 else TARGETS["pass4"] if k == 4 else None
        if k > repeats or not cases:
            reason = f"반복 n={repeats}이라 k={k}를 추정할 수 없음"
            if repeats == 1 and k > 1:
                reason += "(규칙 정책은 결정적이라 한 번만 돈다)"
            rows.append(stats.not_measured(key, label, reason, goal))
            continue
        values = [stats.per_task_pass_hat_k(c["successes"], c["n"], k) for c in cases]
        rows.append(
            stats.metric(
                key,
                label,
                stats.mean(values),
                ci=stats.bootstrap_ci(values, resamples=resamples),
                n=len(cases),
                runs=len(trials),
                goal=goal,
            )
        )
    rows.append(
        stats.rate_metric(
            "verdictMatch",
            "끝 상태 일치율(r_상태)",
            [t["verdictMatch"] for t in trials],
            clusters=[t["caseId"] for t in trials] if repeats > 1 else None,
            resamples=resamples,
        )
    )
    violations = sum(t["violations"] for t in trials)
    rows.append(
        stats.count_metric(
            "violations", "금지 위반 건수", violations, n=len(trials), goal=TARGETS["violations"]
        )
    )
    recalls = []
    recall_cases = []
    for t in trials:
        required = t["questions"]["required"]
        if required:
            asked = t["questions"]["asked"]
            recalls.append(sum(1 for r in required if r in asked) / len(required))
            recall_cases.append(t["caseId"])
    rows.append(
        stats.rate_metric(
            "requiredRecall",
            "필수 질문 재현율",
            recalls,
            clusters=recall_cases if repeats > 1 else None,
            goal=TARGETS["requiredRecall"],
            resamples=resamples,
            note="사례 expect.questions.asked가 있는 실행만",
        )
    )
    extra, extra_cases = [], []
    for t in trials:
        banned = t["questions"]["notAsked"]
        asked = t["questions"]["asked"]
        if banned and asked:
            extra.append(sum(1 for a in asked if a in banned) / len(asked))
            extra_cases.append(t["caseId"])
    rows.append(
        stats.rate_metric(
            "unneededQuestions",
            "불필요 질문률(근사)",
            extra,
            clusters=extra_cases if repeats > 1 else None,
            resamples=resamples,
            note="허용 집합 대신 사례의 notAsked로 센 근사값",
        )
    )
    latencies = [t["latencyS"] for t in trials]
    rows.append(
        stats.metric(
            "latencyP50", "시나리오 소요 시간 p50(초)", _quantile(latencies, 0.5), n=len(trials)
        )
    )
    rows.append(
        stats.metric(
            "latencyP95",
            "시나리오 소요 시간 p95(초)",
            _quantile(latencies, 0.95),
            n=len(trials),
            goal=TARGETS["latencyP95"],
            note="평가 실행기 안(같은 프로세스)의 시간. 배포 주소의 시간이 아니다",
        )
    )
    turns = [x for t in trials for x in t["turns"]]
    rows.append(
        stats.metric(
            "turnsPerThread",
            "열린 스레드당 턴 수(평균)",
            stats.mean(turns),
            ci=stats.bootstrap_ci(turns, resamples=resamples) if turns else None,
            n=len(turns),
        )
    )
    rows.append(
        stats.metric(
            "questionBatches",
            "실행당 질문 묶음 수(평균)",
            stats.mean([t["questionBatches"] for t in trials]),
            n=len(trials),
        )
    )
    rows.append(
        stats.metric(
            "handoffs",
            "실행당 인계(handoff) 스레드 수(평균)",
            stats.mean([t["handoffs"] for t in trials]),
            n=len(trials),
        )
    )
    meter = outcome["meter"]
    if meter["calls"]:
        rows.append(stats.metric("modelCalls", "모델 호출 수(합)", meter["calls"], n=len(trials)))
        rows.append(
            stats.metric(
                "promptTokens",
                "입력 토큰(합)",
                meter["promptTokens"],
                n=len(trials),
                note=f"usage를 주지 않은 응답 {meter['unknownUsage']}건"
                if meter["unknownUsage"]
                else None,
            )
        )
        rows.append(stats.metric("completionTokens", "출력 토큰(합)", meter["completionTokens"]))
    else:
        rows.append(
            stats.not_measured("tokens", "입력·출력 토큰", "모델을 부르지 않았다(규칙 정책)")
        )
    rows.append(
        stats.not_measured("cost", "비용(원)", "단가 설정이 없다. 첫 실제 모델 측정 뒤 정한다")
    )
    return rows


def _slot_rows(outcome: dict[str, Any], resamples: int) -> list[dict[str, Any]]:
    trials = outcome["trials"]
    slots = [(t["caseId"], s) for t in trials for s in t["slots"]]
    drafts = [(t["caseId"], a) for t in trials for s in t["slots"] for a in s["attempts"]]
    slot_clusters = [cid for cid, _ in slots]
    draft_clusters = [cid for cid, _ in drafts]
    rows = [
        stats.rate_metric(
            "firstTry",
            "초안 첫 시도 통과율(차례)",
            [s["attempts"][0]["ok"] for _, s in slots],
            clusters=slot_clusters,
            goal=TARGETS["firstTry"],
            resamples=resamples,
        ),
        stats.rate_metric(
            "afterRewrite",
            f"재작성 뒤 통과율(차례, 시도 상한 {MAX_ATTEMPTS}번)",
            [any(a["ok"] for a in s["attempts"]) for _, s in slots],
            clusters=slot_clusters,
            goal=TARGETS["afterRewrite"],
            resamples=resamples,
            note="설계는 첫 시도 + 재작성 2번(3번)이고 코드(agents/draft.py MAX_ATTEMPTS)는 2번이다",
        ),
        stats.rate_metric(
            "handoff",
            "검증 실패 인계율(차례)",
            [s["outcome"] == "handoff" for _, s in slots],
            clusters=slot_clusters,
            goal=TARGETS["handoff"],
            resamples=resamples,
        ),
        stats.rate_metric(
            "numberHallucination",
            "초안 숫자 환각률(초안)",
            [bool(set(a["codes"]) & NUMBER_CODES) for _, a in drafts],
            clusters=draft_clusters,
            resamples=resamples,
        ),
        stats.rate_metric(
            "evidenceHallucination",
            "초안 근거 환각률(초안)",
            [bool(set(a["codes"]) & EVIDENCE_CODES) for _, a in drafts],
            clusters=draft_clusters,
            resamples=resamples,
        ),
    ]
    quote = sum(1 for _, a in drafts if set(a["codes"]) & QUOTE_CODES)
    clause_messages = 0
    for t in trials:
        for thread in (t.get("raw") or {}).get("threads") or []:
            for m in thread["messages"]:
                if any(c.get("kind") == "clause" for c in m.get("claims") or []):
                    clause_messages += 1
    denominator = quote + clause_messages
    rows.append(
        stats.metric(
            "quoteMismatch",
            "인용문 불일치율(근사)",
            quote / denominator if denominator else None,
            n=denominator,
            note=(
                "분모 = QUOTE_MISMATCH 초안 + 조항을 인용해 나간 메시지(거부된 초안의 인용 여부는 "
                "기록에 없다)"
                if denominator
                else "조항을 인용한 초안이 없었다"
            ),
        )
    )
    checked = sum(t["rechecks"]["checked"] for t in trials)
    rows.append(
        stats.metric(
            "recheckRejected",
            "받은 글 재검증 거부율",
            sum(t["rechecks"]["failed"] for t in trials) / checked if checked else None,
            n=checked,
            note="상대가 보낸 메시지를 받는 쪽이 다시 검증해 거부한 비율(거부되면 handoff)",
        )
    )
    failed = sum(len(codes) for t in trials for codes in t["outgoing"]["failed"].values())
    rows.append(
        stats.count_metric(
            "outgoingHallucination",
            "나간 글 재검증 거부 건수(숫자·근거 환각)",
            failed,
            n=sum(t["outgoing"]["checked"] for t in trials),
            goal=TARGETS["outgoingHallucination"],
            note="n은 다시 검증한 나간 메시지와 문서 수",
        )
    )
    return rows


def rejection_distribution(outcome: dict[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for t in outcome["trials"]:
        for s in t["slots"]:
            for a in s["attempts"]:
                counts.update(a["codes"])
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def category_table(outcome: dict[str, Any], *, resamples: int = stats.RESAMPLES) -> dict:
    """사례 tags마다 n, 성공률, CI(평가 설계 15장). n < 10이면 표본 적음."""
    by: dict[str, list[float]] = {}
    for case in outcome["cases"]:
        rate = case["successes"] / case["n"] if case["n"] else 0.0
        for tag in case["tags"]:
            by.setdefault(tag, []).append(rate)
    rows = []
    for tag in sorted(by):
        values = by[tag]
        ci = stats.bootstrap_ci(values, resamples=resamples)
        small = len(values) < stats.SMALL_SAMPLE
        rows.append(
            [
                tag,
                len(values),
                round(stats.mean(values) or 0.0, 3),
                None if ci is None else [round(ci[0], 3), round(ci[1], 3)],
                "표본 적음" if small else "",
            ]
        )
    return {
        "title": "범주별 성공률",
        "columns": ["범주", "n", "성공률", "95% CI", ""],
        "rows": rows,
    }


def failure_list(outcome: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for t in outcome["trials"]:
        if t["success"]:
            continue
        rows.append(
            [
                t["caseId"],
                t["repeat"] + 1,
                t["failureType"],
                t["firstRejection"] or "-",
                ", ".join(t["mismatches"][:3]) or "-",
                ", ".join(v["kind"] for v in t["violationItems"][:3]) or "-",
                (t["error"] or "")[:120] or "-",
            ]
        )
    return {
        "title": "실패 목록",
        "columns": ["사례", "반복", "첫째 실패 유형", "첫 거부 사유", "다른 곳", "위반", "오류"],
        "rows": rows,
    }


def failure_counts(outcome: dict[str, Any]) -> dict[str, int]:
    counts = Counter(t["failureType"] for t in outcome["trials"] if not t["success"])
    return {kind: counts.get(kind, 0) for kind in FAILURE_TYPES}


def l4_layer(
    outcome: dict[str, Any],
    *,
    resamples: int = stats.RESAMPLES,
    extra_metrics: list[dict[str, Any]] | None = None,
    extra_tables: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    cases = [c for c in outcome["cases"] if c["complete"]]
    counts = failure_counts(outcome)
    missing = [f"{s['caseId']}#{s['repeat'] + 1}: {s['reason']}" for s in outcome["skipped"]]
    return stats.layer(
        "L4",
        "에이전트 과업 성공(끝 상태)",
        unit="시나리오",
        n=len(cases),
        status="partial" if outcome["skipped"] else "measured",
        metrics=[*l4_metrics(outcome, resamples=resamples), *(extra_metrics or [])],
        tables=[
            {
                "title": "실패 유형(첫째 유형)",
                "columns": ["유형", "건수"],
                "rows": [[k, v] for k, v in counts.items()],
            },
            category_table(outcome, resamples=resamples),
            failure_list(outcome),
            *(extra_tables or []),
        ],
        not_measured_items=missing,
        notes=notes or [],
        details={
            "repeats": outcome["repeats"],
            "runs": len(outcome["trials"]),
            "meter": outcome["meter"],
        },
    )


def l3_layer(outcome: dict[str, Any], *, resamples: int = stats.RESAMPLES) -> dict[str, Any]:
    slots = sum(len(t["slots"]) for t in outcome["trials"])
    distribution = rejection_distribution(outcome)
    by_side: dict[str, list[bool]] = {}
    for t in outcome["trials"]:
        for s in t["slots"]:
            by_side.setdefault(s["side"] or "모름", []).append(s["attempts"][0]["ok"])
    side_rows = [
        [side, len(flags), round(sum(flags) / len(flags), 3)]
        for side, flags in sorted(by_side.items())
    ]
    return stats.layer(
        "L3",
        "근거 있는 생성(결정적 지표)",
        unit="차례(사례로 묶음)",
        n=slots,
        metrics=_slot_rows(outcome, resamples),
        tables=[
            {
                "title": "거부 사유 분포(초안)",
                "columns": ["사유 코드", "건수"],
                "rows": [[k, v] for k, v in distribution.items()] or [["없음", 0]],
            },
            {
                "title": "보낸 쪽별 첫 시도 통과율",
                "columns": ["보낸 쪽", "차례 수", "첫 시도 통과율"],
                "rows": side_rows,
            },
        ],
        not_measured_items=[
            "의미 지표(인용 재현율·정밀도, 주장 충실도): 판정 모델과 사람 라벨 100문장이 없다(7장)",
        ],
    )
