"""실시간 진행: 두 에이전트가 주고받는 대화를 화면에 바로 보여 주는 기록과, 한 진단에 실행 하나(실행 자리).

계약 8장 "실시간 진행"의 기록 쪽이다. 보는 조직에 맞춘 모양은 service.stream_events가 만든다.

- 진행기(agents/orchestrator.py)가 Recorder로 기록을 쓴다. 기록은 저장소의 activity(SQL은
  v3_activity)에 하나씩 곧바로 쓰이고(SQL은 기록마다 커밋한다), 같은 프로세스에서 기다리는 스트림은
  HUB로 깨운다. 다른 요청은 실행이 끝나기 전에도 기록을 읽는다.
- 작성 중·근거 확인·거부·다시 쓰기·도구 호출은 draft.run_draft가 Trace.notify로 알리고, 진행기가 차례마다
  정한 Turn(draft.LIVE)이 기록으로 옮긴다. 업체 측 에이전트가 다른 프로세스(HTTP)에 있으면 그 알림이
  오지 않으므로, 답을 받은 뒤 업체 측이 보낸 기록(reported_trace)으로 거부와 다시 쓰기를 채운다.
- 기록에는 화면에 보일 짧은 문장(text)과 거르는 데 쓸 값만 둔다: threadId, orgId(그 일을 한 조직),
  shared(대화의 두 당사자가 모두 보는 일: 보낸 메시지, 넘김, 대화 열기). 내부 코드는 codes에만 두고
  관리자 조직에만 보인다. 문장에 근거 ID, 숫자, 내부 코드를 넣지 않는다.
- 실행 자리: 한 진단에는 실행이 하나만 돈다(store.claim_run). 도는 동안 HEARTBEAT_SECONDS마다 살아 있다고
  적고(Heartbeat), STALE_SECONDS 동안 적지 않은 자리는 멈춘 것(프로세스가 죽거나 재시작됨)으로 본다.

표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Self

from app.v3 import names
from app.v3.agents import draft
from app.v3.judge.toolbox import TOOLS
from app.v3.textutil import josa

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 10.0
STALE_SECONDS = 60.0
wall_clock = time.time  # 실행 자리의 시각(초). App.wall_clock으로 바꿔 끼울 수 있다

# SSE(GET /sessions/{id}/stream). 기록이 없어도 저장소를 다시 읽는 간격, 같은 프로세스 알림을 보는 간격,
# 빈 줄(주석) 간격(초), 브라우저가 다시 붙기 전에 기다릴 시간(밀리초)
STREAM_READ_SECONDS = 1.0
STREAM_POLL_SECONDS = 0.1
STREAM_KEEPALIVE_SECONDS = 15.0
STREAM_RETRY_MS = 3000
# 프록시(Caddy, nginx, Next.js)가 응답을 모아 두지 않게 한다
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# 대화 화면의 작성자 이름(service.AUTHOR_LABELS와 같은 낱말). 구현 이름(모델 이름)은 쓰지 않는다
AGENT_LABELS = {"company": "회사 측 AI", "vendor": "업체 측 AI"}
SYSTEM_LABEL = "자동 안내"
ACTORS = {"company": "company_agent", "vendor": "vendor_agent"}

KINDS = (
    "drafting",
    "validating",
    "rejected",
    "rewriting",
    "sent",
    "handoff",
    "tool",
    "question",
    "status",
)

TYPE_LABELS = {
    "inquiry": "문의",
    "question": "질문",
    "answer": "답변",
    "offer": "제안",
    "counter": "수정 제안",
    "conditional": "조건부 제안",
    "decline": "거절",
    "handoff": "담당자 넘김",
    "confirm": "확정",
}

TOOL_LABELS = names.TOOL_LABELS  # 판단 도구의 화면 이름(names.py)
TOOL_STATUS_LABELS = {
    "calculated": "확인됨",
    "conditional": "조건부",
    "needs_information": "확인 필요",
    "error": "실패",
}

# 초안 거부 코드(계약 8장 "초안 거부 코드") → 화면에 보일 사유 분류. 코드는 관리자에게만 보인다
REASONS = {
    "NUMBER_OUTSIDE_PLACEHOLDER": "근거에 없는 숫자",
    "UNKNOWN_PLACEHOLDER": "근거에 없는 값",
    "UNKNOWN_CLAIM_REF": "근거에 없는 값",
    "CLAIM_WITHOUT_EVIDENCE": "근거에 없는 값",
    "EVIDENCE_NOT_FOUND": "근거에 없는 값",
    "UNUSED_CLAIM": "본문과 값이 맞지 않음",
    "VALUE_MISMATCH": "근거와 다른 값",
    "UNIT_MISMATCH": "근거와 다른 값",
    "QUOTE_MISMATCH": "원문과 다른 조항 인용",
    "EVIDENCE_FORBIDDEN": "볼 수 없는 근거",
    "EVIDENCE_EXPIRED": "기한이 지난 근거",
    "TYPE_NOT_ALLOWED": "지금 보낼 수 없는 종류",
    "APPROVAL_REQUIRED": "지금 보낼 수 없는 종류",
    "THREAD_RULE": "대화 규칙에 맞지 않음",
    "UNKNOWN_STREAM": "대화에 없는 폐기물",
    "PERMIT_MISMATCH": "허가 품목이 맞지 않음",
    "UNBACKED_TERM": "근거 없는 조건 낱말",
    "TOO_MANY_TOOL_ROUNDS": "응답 형식 오류",
    "INVALID_OUTPUT": "응답 형식 오류",
    "MODEL_ERROR": "모델 호출 실패",
}
GUARDRAIL_REASON = "업체 기준 밖의 조건"
OTHER_EMITTER_REASON = "다른 회사 정보"
DEFAULT_REASON = "검사를 통과하지 못함"

PHASE_NOTES = {
    "identify": (1, "폐기물을 식별하고 등급을 매깁니다."),
    "discover": (4, "업체를 찾고 허가 품목과 매입 기준을 대조합니다."),
    "converse": (4, "업체 측 AI와 조건을 주고받습니다."),
    "propose": (6, "비용과 탄소를 견주어 최적안을 만듭니다."),
}
STATUS_NOTES = {
    "awaiting_answers": "담당자 답을 기다립니다.",
    "awaiting_approval": "양쪽 담당자의 승인을 기다립니다.",
    "confirmed": "확정했습니다.",
    "idle": "멈췄습니다. 다시 진행할 수 있습니다.",
}


def reason_of(code: str) -> str:
    code = str(code)
    if code.startswith("GUARDRAIL_"):
        return GUARDRAIL_REASON
    if code.startswith("OTHER_EMITTER"):
        return OTHER_EMITTER_REASON
    return REASONS.get(code, DEFAULT_REASON)


def reasons_of(codes: list[str]) -> list[str]:
    return list(dict.fromkeys(reason_of(code) for code in codes))


# ── 같은 프로세스 안의 알림 ──────────────────────────────────────────
class Hub:
    """진단마다 기록이 늘어난 횟수. 스트림은 이 값이 바뀌면 저장소를 다시 읽는다(깨우기만 한다).

    값이 없거나 다른 프로세스가 쓴 기록은 스트림이 주기적으로 저장소를 읽어 잡는다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}

    def bump(self, session_id: str) -> None:
        with self._lock:
            self._versions[session_id] = self._versions.get(session_id, 0) + 1

    def version(self, session_id: str) -> int:
        with self._lock:
            return self._versions.get(session_id, 0)


HUB = Hub()


# ── 실행 자리 ────────────────────────────────────────────────────────
def alive(run: dict[str, Any] | None, stale_after: float, now: float) -> bool:
    """실행 자리가 살아 있는지(heartbeat가 now 앞 stale_after초 안에 있었는지)."""
    if not run:
        return False
    return float(run.get("heartbeat") or 0) >= now - float(stale_after)


class Heartbeat:
    """실행이 도는 동안 every초마다 자리에 살아 있다고 적는 데몬 스레드."""

    def __init__(
        self,
        store: Any,
        session_id: str,
        run_id: str,
        every: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = wall_clock,
    ) -> None:
        self.store, self.session_id, self.run_id, self.every = store, session_id, run_id, every
        self.clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._beat, name=f"v3-heartbeat-{session_id}", daemon=True
        )

    def _beat(self) -> None:
        while not self._stop.wait(self.every):
            try:
                if not self.store.touch_run(self.session_id, self.run_id, self.clock()):
                    return  # 자리를 잃었다(멈춘 것으로 보고 다른 실행이 차지했다)
            except Exception:  # 저장소가 잠시 닿지 않아도 실행은 계속한다
                logger.warning("실행 자리에 살아 있다고 적지 못했다", exc_info=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()


class Locked:
    """메서드 호출 하나마다 잠금을 잡는 감싸개(App.with_lock).

    메모리 저장소는 동시에 고치면 깨진다. 두 서버는 요청 하나를 처리하는 동안 같은 잠금을 잡으므로, 뒤에서
    도는 실행이 저장소를 부를 때마다 그 잠금을 잡으면 요청과 번갈아 돈다(모델을 기다리는 동안에는 잡지 않는다).
    """

    def __init__(self, inner: Any, lock: Any) -> None:
        self._inner, self._lock = inner, lock

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._inner, name)
        if not callable(value):
            return value
        lock = self._lock

        @functools.wraps(value)
        def locked(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return value(*args, **kwargs)

        return locked


# ── SSE ─────────────────────────────────────────────────────────────
def sse(event: str, data: Any, event_id: int | None = None) -> str:
    """SSE 한 덩어리. data는 JSON 한 줄이다(줄바꿈은 JSON이 이스케이프한다)."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"{head}event: {event}\ndata: {body}\n\n"


def resume_point(after: str | None, last_event_id: str | None) -> int:
    """이어 받을 지점: ?after=와 Last-Event-ID 가운데 큰 값(없으면 0, 처음부터).

    after가 0 이상의 정수가 아니면 ValueError. Last-Event-ID는 브라우저가 다시 붙을 때 보내는 값이라
    읽을 수 없으면 쓰지 않는다.
    """
    start = int(after) if after not in (None, "") else 0
    if start < 0:
        raise ValueError("after는 0 이상이어야 한다")
    with contextlib.suppress(TypeError, ValueError):
        start = max(start, int(str(last_event_id)))
    return start


def stream_chunks(
    first: dict[str, Any],
    read: Callable[[int], dict[str, Any]],
    session_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[str]:
    """동기 서버(devserver)의 SSE 조각. read(after)는 service.stream_events의 결과를 준다.

    FastAPI(api.py)는 같은 순서를 비동기로 한다. 실행이 끝나고 기록을 다 보냈으면 done으로 닫는다.
    """
    yield f"retry: {STREAM_RETRY_MS}\n\n"
    batch, last = first, int(first["lastSeq"])
    seen = HUB.version(session_id)
    read_at = sent_at = clock()
    while True:
        for item in batch["events"]:
            yield sse(item["event"], item["data"], item["id"])
            sent_at = clock()
        last = max(last, int(batch["lastSeq"]))
        if not batch["running"]:
            yield sse("done", {"status": batch["status"], "lastSeq": last})
            return
        while True:
            sleep(STREAM_POLL_SECONDS)
            now = clock()
            if now - sent_at >= STREAM_KEEPALIVE_SECONDS:
                yield ": keep-alive\n\n"
                sent_at = now
            version = HUB.version(session_id)
            if version != seen or now - read_at >= STREAM_READ_SECONDS:
                seen, read_at = version, now
                break
        batch = read(last)


# ── 기록 쓰기 ────────────────────────────────────────────────────────
def _side_of(thread: Any, org_id: str | None) -> str:
    return "vendor" if org_id and org_id == thread.vendor_org else "company"


def _org_of(thread: Any, side: str) -> str:
    return thread.vendor_org if side == "vendor" else thread.emitter_org


class Recorder:
    """진행기가 쓰는 실시간 기록. 기록이 실패해도 진단은 멈추지 않는다(경고만 남긴다)."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._status: dict[str, tuple[Any, ...]] = {}

    def _add(self, session_id: str, entry: dict[str, Any]) -> dict[str, Any] | None:
        """기록 하나를 쓰고 쓴 것(seq 포함)을 준다. 쓰지 못했으면 None."""
        try:
            saved = self.store.add_activity(session_id, entry)
        except Exception:  # 화면 기록 때문에 진단이 멈추면 안 된다
            logger.warning("실시간 진행 기록을 쓰지 못했다", exc_info=True)
            return None
        HUB.bump(session_id)
        return saved

    def activity(
        self,
        session_id: str,
        kind: str,
        text: str,
        *,
        stage: int | None = None,
        actor: str = "system",
        thread: Any = None,
        side: str | None = None,
        org_id: str | None = None,
        shared: bool = False,
        author_kind: str = "agent",
        **extra: Any,
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"모르는 진행 종류: {kind}")
        entry: dict[str, Any] = {
            "event": "activity",
            "stage": stage,
            "actor": actor,
            "kind": kind,
            "threadId": thread.id if thread is not None else None,
            "vendorOrgId": thread.vendor_org if thread is not None else None,
            "side": side,
            "authorKind": author_kind if side else None,
            "orgId": org_id,
            "shared": bool(shared),
            "text": text,
        }
        entry.update({key: value for key, value in extra.items() if value is not None})
        self._add(session_id, entry)

    def note(self, session_id: str, text: str, *, stage: int | None = None, **kw: Any) -> None:
        """진행기의 안내 한 줄(단계 시작, 멈춤, 오류). kind는 status다."""
        self.activity(session_id, "status", text, stage=stage, **kw)

    def phase(self, session: dict[str, Any]) -> None:
        stage, text = PHASE_NOTES.get(str(session.get("phase")), (None, ""))
        if text:
            self.note(session["id"], text, stage=stage, org_id=session["emitterOrgId"])

    def status(self, session: dict[str, Any]) -> dict[str, Any] | None:
        """진단 상태·단계·단계별 상태가 바뀌었으면 status 기록 하나(같은 Recorder에서 같은 값은 한 번만)."""
        stages = dict((session.get("work") or {}).get("stages") or {})
        snapshot = (session.get("status"), session.get("phase"), tuple(sorted(stages.items())))
        if self._status.get(session["id"]) == snapshot:
            return None
        self._status[session["id"]] = snapshot
        return self._add(
            session["id"],
            {
                "event": "status",
                "status": session.get("status"),
                "phase": session.get("phase"),
                "stages": stages,
            },
        )

    def questions(
        self, session: dict[str, Any], asked: list[dict[str, Any]], stage: int | None = None
    ) -> None:
        """담당자에게 묻는 묶음. question 기록(배출 기업만 본다)과 안내 한 줄."""
        if not asked:
            return
        self._add(
            session["id"],
            {"event": "question", "questionIds": [q["id"] for q in asked], "stage": stage},
        )
        self.activity(
            session["id"],
            "question",
            f"담당자에게 물을 것이 {len(asked)}개 있습니다. 답을 기다립니다.",
            stage=stage,
            org_id=session["emitterOrgId"],
        )

    def tool(
        self,
        session_id: str,
        name: Any,
        status: Any,
        *,
        thread: Any = None,
        side: str | None = None,
        org_id: str | None = None,
    ) -> None:
        name = str(name) if name in TOOL_LABELS else None
        label = TOOL_LABELS.get(name or "", "판단 도구")
        shown = TOOL_STATUS_LABELS.get(str(status), "확인됨")
        self.activity(
            session_id,
            "tool",
            f"판단 도구 · {label}: {shown}",
            stage=_tool_stage(name),
            actor="tool",
            thread=thread,
            side=side,
            org_id=org_id,
            tool=name,
            toolStatus=status if status in TOOL_STATUS_LABELS else None,
        )

    def from_trace(
        self, session_id: str, events: list[dict[str, Any]], thread: Any, org_id: str | None
    ) -> None:
        """알림 없이 만든 작성 기록(진행기가 정해진 순서로 부른 도구)을 다 끝난 뒤 옮긴다."""
        for event in events:
            detail = event.get("detail") or {}
            if event.get("kind") == "tool_call":
                who = detail.get("orgId") or org_id
                self.tool(
                    session_id,
                    detail.get("tool"),
                    detail.get("status"),
                    thread=thread,
                    side=_side_of(thread, who) if thread is not None else None,
                    org_id=who,
                )

    def sent(self, session_id: str, thread: Any, message: Any) -> None:
        """대화에 붙은 메시지(두 당사자가 모두 본다). handoff는 kind handoff다."""
        side = _side_of(thread, message.sender)
        code = (message.meta.get("model") or {}).get("provider") == "code"
        label = AGENT_LABELS[side]
        if message.type == "handoff":
            kind = "handoff"
            text = f"{'회사' if side == 'company' else '업체'} 측 담당자에게 넘겼습니다."
        else:
            kind = "sent"
            what = josa(TYPE_LABELS.get(message.type, "메시지"), "을를")
            text = (
                f"{SYSTEM_LABEL}로 {what} 보냈습니다."
                if code
                else f"{josa(label, '이가')} {what} 보냈습니다."
            )
        self.activity(
            session_id,
            kind,
            text,
            stage=message.stage,
            actor=ACTORS[side],
            thread=thread,
            side=side,
            org_id=message.sender,
            shared=True,
            author_kind="system" if code else "agent",
            messageSeq=message.seq,
            messageType=message.type,
            evidenceCount=len(message.claims) if kind == "sent" else None,
        )

    def opened(self, session_id: str, thread: Any, vendor_name: str) -> None:
        self.note(
            session_id,
            f"{josa(vendor_name, '와과')} 대화를 엽니다.",
            stage=4,
            thread=thread,
            org_id=thread.emitter_org,
            shared=True,
        )

    @contextlib.contextmanager
    def turn(self, session_id: str, thread: Any, side: str, stage: int) -> Iterator[Turn]:
        """한 차례(회사 측 작성 또는 업체 측 답) 동안 draft.LIVE에 이 차례의 Turn을 둔다."""
        turn = Turn(self, session_id, thread, side, stage)
        token = draft.LIVE.set(turn)
        try:
            yield turn
        finally:
            draft.LIVE.reset(token)

    def recheck(self, session_id: str, thread: Any, codes: list[str] | None, stage: int) -> None:
        """회사 측이 업체 측 답을 다시 검증한다(codes가 None이면 검사 시작, 리스트면 결과)."""
        common = {
            "stage": stage,
            "actor": "validator",
            "thread": thread,
            "side": "vendor",
            "org_id": thread.emitter_org,
        }
        if codes is None:
            self.activity(
                session_id,
                "validating",
                "회사 측에서 받은 답의 숫자와 조항을 근거와 다시 대조하고 있습니다.",
                **common,
            )
        elif codes:
            reasons = reasons_of(codes)
            self.activity(
                session_id,
                "rejected",
                f"회사 측 재확인에서 돌려보냈습니다: {', '.join(reasons)}.",
                reasons=reasons,
                codes=list(dict.fromkeys(codes)),
                **common,
            )


def _tool_stage(name: str | None) -> int | None:
    spec = TOOLS.get(name or "")
    return spec.stage if spec else None


class Turn:
    """한 차례의 실시간 알림을 기록으로 옮긴다(draft.LIVE에 들어가는 함수).

    steps는 작성 쪽에서 받은 알림 수다. 업체 측 차례에 0이면 업체 측이 다른 프로세스에 있었다는 뜻이다.
    """

    def __init__(self, recorder: Recorder, session_id: str, thread: Any, side: str, stage: int):
        self.recorder, self.session_id, self.thread = recorder, session_id, thread
        self.side, self.stage = side, stage
        self.steps = 0
        self._attempt = 0

    def _activity(self, kind: str, text: str, **extra: Any) -> None:
        extra.setdefault("actor", ACTORS[self.side])
        self.recorder.activity(
            self.session_id,
            kind,
            text,
            stage=self.stage,
            thread=self.thread,
            side=self.side,
            org_id=_org_of(self.thread, self.side),
            **extra,
        )

    def announce(self) -> None:
        """작성 쪽 알림을 기다리지 않고 첫 초안을 쓰기 시작했다고 적는다(업체 측 차례)."""
        self._drafting(1)

    def _drafting(self, attempt: int) -> None:
        if attempt <= self._attempt:
            return
        self._attempt = attempt
        label = AGENT_LABELS[self.side]
        if attempt == 1:
            text = (
                f"{josa(label, '이가')} 메시지를 쓰고 있습니다."
                if self.side == "company"
                else f"{josa(label, '이가')} 답을 쓰고 있습니다."
            )
            self._activity("drafting", text, attempt=1)
        else:
            self._activity(
                "rewriting",
                f"{josa(label, '이가')} 근거를 다시 확인해 고쳐 쓰고 있습니다.",
                attempt=attempt,
            )

    def __call__(self, step: str, info: dict[str, Any]) -> None:
        self.steps += 1
        if step == "drafting":
            self._drafting(int(info.get("attempt") or 1))
        elif step == "validating":
            self._activity(
                "validating",
                "보내기 전에 숫자와 조항을 근거와 대조하고 있습니다.",
                actor="validator",
                attempt=info.get("attempt"),
            )
        elif step == "trace":
            self._trace(info)

    def _trace(self, event: dict[str, Any]) -> None:
        detail = event.get("detail") or {}
        if event.get("kind") == "tool_call":
            self.recorder.tool(
                self.session_id,
                detail.get("tool"),
                detail.get("status"),
                thread=self.thread,
                side=self.side,
                org_id=_org_of(self.thread, self.side),
            )
        elif event.get("kind") == "validation" and detail.get("ok") is False:
            codes = [
                str(r.get("code"))
                for r in detail.get("rejections") or []
                if isinstance(r, dict) and r.get("code")
            ]
            reasons = reasons_of(codes) or [DEFAULT_REASON]
            self._activity(
                "rejected",
                f"근거 확인에서 돌려보냈습니다: {', '.join(reasons)}.",
                actor="validator",
                attempt=detail.get("attempt"),
                reasons=reasons,
                codes=list(dict.fromkeys(codes)),
            )

    def replay(self, events: list[dict[str, Any]]) -> None:
        """다른 프로세스의 업체 측이 보낸 기록(reported_trace)으로 거부와 다시 쓰기를 채운다."""
        for event in events:
            detail = event.get("detail") or {}
            attempt = detail.get("attempt")
            if event.get("kind") == "validation" and isinstance(attempt, int) and attempt > 1:
                self._drafting(attempt)
            self._trace(event)
