"""진행기. 진단 한 건을 멈춤 지점까지 진행한다(docs/v3/RUNTIME.md 3~4장).

멈춤 지점은 awaiting_answers(담당자 답 대기), awaiting_approval(양쪽 승인 대기), confirmed다.
다시 부르면 이어서 진행하고, 이미 한 일은 되풀이하지 않는다. 진행 상태는 세션 레코드의
phase와 work에 남기고, 모든 메시지·도구 호출·검증 결과·handoff·승인은 실행 기록(events)에 남긴다.
"""

from __future__ import annotations

from typing import Any

from app.v3 import ids
from app.v3 import names as naming
from app.v3.a2a.message import PLACEHOLDER, Message
from app.v3.a2a.policy import (
    QuestionPolicy,
    can_open_thread,
    check_disclosure,
    permit_evidence,
)
from app.v3.a2a.thread import Thread, ThreadError
from app.v3.agents import questions
from app.v3.agents.company import CompanyAgent
from app.v3.a2a.guardrails import WEEKDAYS
from app.v3.agents.draft import (
    ACCEPT,
    ASK_USER,
    Outcome,
    Trace,
    make_decline,
    make_handoff,
    model_label,
    public_rejections,
)
from app.v3.agents.pool import safe_id
from app.v3.agents.vendor import public_profile
from app.v3.demo.seed import share_evidence, share_stream, share_vendor_terms
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.evidence.render import render_text
from app.v3.evidence.units import UnitError, convert
from app.v3.evidence.validator import (
    ALLOWED_TOKENS,
    name_tokens,
    validate_message,
    validate_text,
)
from app.v3.intake.grade_model import GradeModelError, grade_stream
from app.v3.judge import basis
from app.v3.judge.toolbox import TOOLS, call_tool

PHASES = ("identify", "discover", "converse", "propose", "approval", "done")
TERMS_TYPES = ("offer", "counter", "conditional")
MAX_STEPS = 400  # 대화 고리의 안전 상한. 스레드마다 12턴 상한이 먼저 걸린다
STAGE_KEYS = ("1", "2", "3", "4", "5", "6")
WORST = {"calculated": 0, "conditional": 1, "needs_information": 2}
# check_loading의 검사 키 → 함께 보여 줄 값(claim 키)
LOADING_CLAIMS = {
    "min_volume": ("totalKg", "minMonthlyKg"),
    "loading": ("pickupsPerMonth", "perPickupKg", "capacityKg", "perPickupM3", "capacityM3"),
    "loading_volume": ("pickupsPerMonth", "perPickupKg", "capacityKg"),
    "monthly_kg": (),
}


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
        "unresolved": [],
        "optimize": None,
        "result": None,
        "stages": dict.fromkeys(STAGE_KEYS, "waiting"),
    }


def memo_result(memo: list[dict[str, Any]], name: str, **arguments: Any) -> dict[str, Any] | None:
    for entry in memo or []:
        if entry["name"] == name and all(
            entry["arguments"].get(k) == v for k, v in arguments.items()
        ):
            return entry["result"]
    return None


def claim_in(result: dict[str, Any] | None, key: str | None, new_id: str) -> dict[str, Any] | None:
    """도구 결과에서 claim 하나를 꺼내 화면용 ID를 붙인다. 없으면 None(확인 필요)."""
    if not result or not key:
        return None
    for claim in result.get("claims") or []:
        if claim["id"] == key:
            # 사람이 읽는 이름과 구실은 도구의 claim 키에서 정한다
            return naming.annotate({**claim, "id": new_id}, key)
    return None


def permit_digest(stream: dict[str, Any], vendor: dict[str, Any]) -> str:
    """허가 품목 대조에 들어가는 값의 지문. 같으면 대조 결과도 같다."""
    return ids.digest([stream.get("wasteItem"), stream.get("material"), vendor.get("permits")])


def thread_names(store: Any, thread: Any) -> list[str]:
    """대화에 나오는 조직과 흐름의 이름. 검증기가 이름에 든 숫자를 수치로 세지 않게 넘긴다."""
    raw = thread.to_dict() if hasattr(thread, "to_dict") else thread
    names: list[str] = []
    for org_id in (raw.get("emitterOrgId"), raw.get("vendorOrgId")):
        try:
            names.append(store.get_org(org_id).get("name"))
        except KeyError:
            continue
    for stream_id in raw.get("streamIds") or []:
        try:
            names.append(store.get_stream(stream_id).get("name"))
        except KeyError:
            continue
    return [n for n in names if n]


class Orchestrator:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.store = app.store
        self.registry = app.registry
        self.policy = QuestionPolicy()

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
        if session["status"] in ("confirmed", "awaiting_approval"):
            return session
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
            paused = steps[session["phase"]](session)
            if paused:
                session["status"] = "awaiting_answers"
                self._save(session)
                return session
            session["phase"] = PHASES[PHASES.index(session["phase"]) + 1]
            self._save(session)
        if session["phase"] == "approval":
            session["status"] = "awaiting_approval"
        elif session["status"] == "running":
            session["status"] = "idle"
        self._save(session)
        return session

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
            self._ask(session, batch)
            return True
        work["unresolved"] = [
            {"streamId": q["streamId"], "field": q["field"]}
            for q in self.policy.unresolved(pending, history)
        ]
        work["stages"]["1"] = "needs_check" if missing or work["unresolved"] else "done"
        return False

    def _ask(self, session: dict[str, Any], batch: list[dict[str, Any]]) -> None:
        for question in batch:
            question = {"id": self.store.next_id("q"), **question}
            self.store.put_question(session["id"], question)
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

    def answer(self, session_id: str, question_id: str, value: Any) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        question = next(
            (q for q in self.store.list_questions(session_id) if q["id"] == question_id), None
        )
        if question is None:
            raise OrchestratorError("QUESTION_NOT_FOUND", "그런 질문이 없습니다.", 404)
        if question.get("answered"):
            return session  # 같은 답을 두 번 보내도 한 번만 적는다
        try:
            parsed = questions.parse_answer(question["field"], value)
            recorded = self.policy.record_answer(question, parsed)
        except ValueError as error:
            raise OrchestratorError("INVALID_ANSWER", str(error), 422) from error
        stream = self.store.get_stream(question["streamId"], session["emitterOrgId"])
        stream = questions.apply_answer(
            self.registry, stream, question["field"], parsed, self.today
        )
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
    def _discover(self, session: dict[str, Any]) -> bool:
        work, org_id = session["work"], session["emitterOrgId"]
        emitter = self.store.get_org(org_id)
        streams = self.store.list_streams(org_id)
        profiles = {p["orgId"]: p for p in self.store.list_vendor_profiles()}
        for profile in profiles.values():  # 허가증은 대조하기 전에 연다
            for permit in profile.get("permits") or []:
                for evidence_id in permit.get("evidence") or []:
                    share_evidence(self.registry, evidence_id, org_id)
        publics = {v: public_profile(p, fleet_cost=False) for v, p in profiles.items()}
        work["stages"].update({"3": "running", "4": "running"})

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
                    f"{name}와 대화를 엽니다(허가 품목 일치).",
                    thread.id,
                    vendorOrgId=vendor_id,
                    streamIds=list(thread.stream_ids),
                )
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
                    reason = f"업체의 허가 품목에 {names}이(가) 없어 대화를 열지 않습니다."
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
            route = next((r for r in ("sell", "treat") if r in routes[sid]), "keep")
            required = self._tool(
                session,
                "check_requirements",
                {"stream": stream, "route": route},
                summary_ids={"streamId": sid, "route": route},
            )
            work["requirements"][sid] = required
            states.append(required["status"])
        work["stages"]["3"] = "needs_check" if any(s != "calculated" for s in states) else "done"

        for thread in opened:  # ④ 연 스레드마다 문의
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
        state = session["work"]["threadState"]
        if thread.closed:
            state[thread.id] = "closed"
        elif message.sender == thread.emitter_org:
            state[thread.id] = "awaiting_vendor"
        else:
            state[thread.id] = "awaiting_company"

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
        work["offers"].pop(thread.id, None)  # 닫힌 대화의 제안은 더 쓰지 않는다
        freed = work["assigned"].pop(thread.id, None)
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
        if reassign:
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
        self, session, thread: Thread, failed: list[str], evidence: list[str], *, reassign=True
    ) -> bool:
        """허가 품목이 맞지 않음을 연 뒤에 알았다. 회사 측이 근거와 함께 decline으로 닫는다."""
        if thread.closed or thread.blocked:
            return False
        reason = "업체의 허가 품목이 이 대화의 흐름과 맞지 않아 근거와 함께 대화를 닫습니다."
        message = make_decline(
            thread_id=thread.id,
            sender=thread.emitter_org,
            recipient=thread.vendor_org,
            reason=reason,
            stage=4,
            stream_ids=tuple(s for s in thread.stream_ids if s in failed),
            evidence=evidence,
            excluded=[{"streamId": sid, "reason": "허가 품목이 맞지 않습니다."} for sid in failed],
        )
        try:
            thread.check(message)
        except ThreadError:  # 턴 상한 등으로 decline을 보낼 수 없으면 담당자에게 넘긴다
            return self._handoff(session, thread, thread.emitter_org, reason, reassign=reassign)
        self._append(session, thread, message, "company_agent")
        work = session["work"]
        for sid in failed:
            entry = {
                "streamId": sid,
                "vendorOrgId": thread.vendor_org,
                "reason": "허가 품목이 맞지 않습니다.",
                "stage": 4,
            }
            if entry not in work["excluded"]:
                work["excluded"].append(entry)
        return self._release(session, thread, reassign=reassign, cause="permit")

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
        vendors = [public_profile(profiles[t.vendor_org], fleet_cost=False) for t in live]
        arguments: dict[str, Any] = {"streams": streams, "vendors": vendors}
        if exclude:
            arguments["exclude"] = exclude
        optimized = self._tool(
            session,
            "optimize_assignment",
            arguments,
            summary_ids={
                "streamIds": [s["id"] for s in streams],
                "vendorOrgIds": [v["orgId"] for v in vendors],
                "reason": "handoff",
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
                    "다른 대화가 담당자에게 넘어가 최적안을 다시 계산했습니다. "
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
                    "최적안을 다시 계산해 이 대화에 배정된 흐름이 바뀌었습니다. 승인을 다시 받습니다.",
                    thread.id,
                    status="agreed",
                    streamIds=new,
                )
            work["assigned"][thread.id] = new
            if sorted(new) != sorted(old):
                work.setdefault("agreedBasis", {})[thread.id] = self._basis(session, thread)
                self._loading_for(session, thread, new, streams, profiles)
                self._verify(session, thread)
        work["result"] = self._result(session, streams)
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
            "합의한 대화가 허가 품목 불일치로 닫혀 그 업체를 빼고 최적안을 다시 계산했습니다."
            if cause == "permit"
            else "합의한 대화가 담당자에게 넘어가 그 업체를 빼고 최적안을 다시 계산했습니다.",
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
            vendor=public_profile(profiles[thread.vendor_org], fleet_cost=False),
            memo=session["work"]["threadTools"].setdefault(thread.id, []),
        )
        self._flush(session["id"], trace, thread.id, session["emitterOrgId"])

    def _send(self, session, thread: Thread, outcome: Outcome, trace: Trace, *, stage: int) -> None:
        """회사 측이 쓴 결과를 스레드에 붙인다. 두 번 거부됐으면 handoff로 넘긴다."""
        self._flush(session["id"], trace, thread.id, thread.emitter_org)
        if outcome.kind == "message" and outcome.message is not None:
            self._append(session, thread, outcome.message, "company_agent")
            return
        reason = outcome.reason or "회사 측 에이전트가 메시지를 쓰지 못해 담당자에게 넘깁니다."
        self._handoff(session, thread, thread.emitter_org, reason, stage=stage, outcome=outcome)

    def _vendor_context(self, session, thread: Thread) -> dict[str, Any]:
        emitter = self.store.get_org(thread.emitter_org)
        streams = []
        for sid in thread.stream_ids:
            stream = self.store.get_stream(sid)
            stream.pop("currentTreatment", None)  # 현행 계약 단가는 업체에 열지 않는다
            streams.append(stream)
        return {
            "sessionId": session["id"],
            "today": self.today,
            "thread": thread.to_dict(),
            "streams": streams,
            "emitter": {
                "id": emitter["id"],
                "name": emitter.get("name"),
                "location": emitter.get("location"),
            },
        }

    def _receive(self, session, thread: Thread, reply: dict[str, Any]) -> None:
        """업체 측 답을 회사 측에서 다시 검사한 뒤에만 스레드에 붙인다."""
        work = session["work"]
        raw_meta = reply.get("meta") if isinstance(reply.get("meta"), dict) else {}
        self._flush(session["id"], list(raw_meta.get("trace") or []), thread.id, thread.vendor_org)
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
        self._event(
            session["id"],
            "validator",
            "validation",
            "받은 답을 회사 측에서 다시 검증: "
            + ("통과" if not problems else "거부(" + ", ".join(p["code"] for p in problems) + ")"),
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
                "업체 측 답이 회사 측 근거 검증을 통과하지 못해 담당자에게 넘깁니다.",
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
                self._decline_for_permit(session, thread, failed, evidence)
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
                        "대화가 턴 상한에 닿아 담당자에게 넘깁니다.",
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
                "업체 측 에이전트를 부르지 못했습니다.",
                thread.id,
                error=type(error).__name__,
            )
            self._handoff(
                session,
                thread,
                thread.emitter_org,
                "업체 측 에이전트에 닿지 못해 담당자에게 넘깁니다.",
                stage=4,
            )
            return
        self._receive(session, thread, reply)

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
        outcome, trace = agent.compose(
            thread,
            "reply",
            streams=streams,
            vendor=public_profile(profiles[thread.vendor_org], fleet_cost=False),
            vendor_org=self.store.get_org(thread.vendor_org),
            memo=work["threadTools"].setdefault(thread.id, []),
            state={
                "offered": offered,
                "excluded": excluded,
                "cannotAskUser": bool(work["cannotAsk"].get(thread.id)),
            },
        )
        if outcome.kind == ACCEPT:
            self._flush(session["id"], trace, thread.id, thread.emitter_org)
            work["threadState"][thread.id] = "settled"
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
            history = self.store.list_questions(session["id"])
            batch = self.policy.next_batch(questions.ordered(wanted), history)
            if batch:
                self._ask(session, batch)
                work["threadState"][thread.id] = "waiting_user"
            elif work["cannotAsk"].get(thread.id):
                self._handoff(
                    session,
                    thread,
                    thread.emitter_org,
                    "담당자에게 더 물을 수 없는 값을 다시 물으려 해 담당자에게 넘깁니다.",
                    stage=4,
                )
            else:  # 질문 한도를 다 썼거나 이미 물은 값이다. 확인 필요로 두고 답하게 한다
                work["cannotAsk"][thread.id] = True
                for question in wanted:
                    entry = {"streamId": question["streamId"], "field": question["field"]}
                    if entry not in work["unresolved"]:
                        work["unresolved"].append(entry)
            return
        self._send(session, thread, outcome, trace, stage=5)

    # ── ⑤⑥ 비용·탄소 비교와 최적안 ────────────────────────────────────
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
                self._decline_for_permit(session, thread, failed, evidence, reassign=False)
                live.remove(thread)
        work["stages"]["5"] = "running"
        costs = []
        for thread in live:
            offered = sorted(work["offers"][thread.id])
            # 대화 중에 담당자 답으로 흐름이 바뀌었으면 경로 판단부터 다시 한다(같은 입력이면 다시 부르지
            # 않는다). 결과 한 장의 확인할 것과 민감도는 이 판단에서 나온다
            calls = [("judge_route", {"streamId": sid}) for sid in offered]
            calls += [("cost_carbon", {"streamId": sid}) for sid in offered]
            calls.append(("check_loading", {"streamIds": offered}))
            results, trace = agent.run_tools(
                calls,
                streams=[by_id[s] for s in thread.stream_ids],
                vendor=public_profile(profiles[thread.vendor_org], fleet_cost=False),
                memo=work["threadTools"].setdefault(thread.id, []),
            )
            self._flush(session["id"], trace, thread.id, org_id)
            costs += [r.get("status") for r in results if r.get("tool") != "judge_route"]
        work["stages"]["5"] = (
            "needs_check"
            if not live or "needs_information" in costs or "error" in costs
            else "done"
        )

        vendors = [public_profile(profiles[t.vendor_org], fleet_cost=False) for t in live]
        exclude = [
            {"streamId": s["id"], "vendorOrgId": t.vendor_org}
            for t in live
            for s in streams
            if s["id"] not in work["offers"][t.id]
        ]
        arguments: dict[str, Any] = {"streams": streams, "vendors": vendors}
        if exclude:
            arguments["exclude"] = exclude
        optimized = self._tool(
            session,
            "optimize_assignment",
            arguments,
            summary_ids={
                "streamIds": [s["id"] for s in streams],
                "vendorOrgIds": [v["orgId"] for v in vendors],
            },
        )
        work["optimize"] = optimized
        thread_of = {t.vendor_org: t for t in live}
        assigned: dict[str, list[str]] = {}
        for item in optimized["output"].get("assignments") or []:
            thread = thread_of.get(item.get("vendorOrgId"))
            if thread is not None:
                assigned.setdefault(thread.id, []).append(item["streamId"])
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
            work.setdefault("agreedBasis", {})[thread.id] = self._basis(session, thread)
            self._loading_for(session, thread, assigned[thread.id], streams, profiles)
            self._verify(session, thread)
        work["result"] = self._result(session, streams)
        work["stages"]["6"] = (
            "needs_check" if work["result"]["status"] == "needs_information" else "done"
        )
        if not assigned:
            session["phase"] = "approval"  # 다음 단계(done)로 넘어가게 한다
        return False

    def _verify(self, session, thread: Thread) -> list[str]:
        """합의한 스레드의 메시지를 다시 검증한다. 실패가 있으면 승인을 막는다."""
        failed: list[str] = []
        for message in thread.messages:
            result = validate_message(
                message, self.registry, today=self.today, names=thread_names(self.store, thread)
            )
            failed += [f"{thread.id}_m{message.seq}: {code}" for code in result.codes]
            # 대화 뒤에 근거가 바뀌었거나 사라졌으면 그 메시지의 근거 확인은 실패다
            if message.meta.get("evidenceMarks", {}) != self._fingerprints(message):
                failed.append(f"{thread.id}_m{message.seq}: EVIDENCE_CHANGED")
        basis = (session["work"].get("agreedBasis") or {}).get(thread.id)
        changed = basis is not None and basis != self._basis(session, thread)
        if changed:
            # 합의한 뒤에 흐름 자료(재질, 물량, 측정값 등)가 바뀌었다. 앞선 합의와 승인은 이전 자료 기준이다
            failed.append(f"{thread.id}: STREAM_CHANGED")
        if failed:
            reason = "근거 확인에 실패한 항목이 있어 승인할 수 없습니다: " + ", ".join(
                dict.fromkeys(failed)
            )
            if changed:
                reason += ". 합의한 뒤에 흐름 자료가 바뀌었습니다. 진단을 새로 진행해 주세요."
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
            )
        else:
            current = next(
                (a for a in self.store.list_approvals(session["id"]) if a["threadId"] == thread.id),
                None,
            )
            if current and current["blocked"]:
                self.store.block_approval(session["id"], thread.id, None)
        return failed

    def _basis(self, session, thread: Thread) -> str:
        """합의한 내용의 지문: 배정된 흐름의 자료와 그 흐름에 받은 조건.

        현행 처리 단가는 업체와 나눈 조건이 아니므로 넣지 않는다.
        """
        work = session["work"]
        assigned = sorted(work["assigned"].get(thread.id) or [])
        offers = work["offers"].get(thread.id) or {}
        streams = []
        for sid in assigned:
            stream = dict(self.store.get_stream(sid))
            stream.pop("currentTreatment", None)
            streams.append(stream)
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
                    if any(c["key"] == check["key"] for c in checks):
                        continue
                    if check["status"] != "met" or check["key"] == "current_assumed":
                        checks.append(
                            {k: check.get(k) for k in ("key", "status", "text", "evidence")}
                        )
            if offer:
                if offer["type"] == "conditional":
                    status = "conditional"
                known = {c["id"]: c for c in self._thread_claims(threads[vendor_id])}
                for text in offer["conditions"]:
                    if text.startswith(f"{by_id[sid].get('name')}:"):
                        cited = [
                            known[m.group(1)]
                            for m in PLACEHOLDER.finditer(text)
                            if m.group(1) in known
                        ]
                        checks.append(
                            {
                                "key": "vendor_condition",
                                "status": "conditional",
                                "text": text,  # 자리표시자는 claims로 그린다
                                "claims": cited,
                                "evidence": [e for c in cited for e in c["evidence"]],
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
                            "evidence": list(check.get("evidence") or []),
                        }
                    )
                    if check["status"] != "met" and status == "calculated":
                        status = "conditional"
                used.append(loading)
            if vendor_id is None and sid in handed:
                # 이 흐름을 맡기로 합의한 대화가 담당자에게 넘어갔다. 현행 유지를 "확정"으로 보이지 않게 한다
                status = "needs_information"
                checks.append(
                    {
                        "key": "handoff",
                        "status": "conditional",
                        "text": (
                            f"{names.get(handed[sid]['vendorOrgId']) or '업체'}의 허가 품목이 맞지 "
                            "않게 되어 합의한 대화를 닫고 이 흐름의 배정을 뺐습니다. 확인 필요."
                            if handed[sid].get("cause") == "permit"
                            else f"{names.get(handed[sid]['vendorOrgId']) or '업체'}와 합의한 대화가 "
                            "담당자에게 넘어가 이 흐름의 배정을 뺐습니다. 확인 필요."
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
                        "status": "conditional",
                        "text": f"확인되지 않은 값({', '.join(dict.fromkeys(pending))})이 있어 "
                        "업체 배정을 확정하지 못했습니다. 확인 필요.",
                        "evidence": [],
                    }
                )
            alternatives = []
            for other_id, thread in sorted(threads.items()):
                other = (work["offers"].get(thread.id) or {}).get(sid)
                if other_id == vendor_id or not other:
                    continue
                other_route = tool_of(other_id, "judge_route", streamId=sid)
                alternatives.append(
                    {
                        "vendorOrgId": other_id,
                        "vendorName": names.get(other_id),
                        "route": (other_route or {}).get("output", {}).get("route"),
                        "unitPrice": {
                            **other["claim"],
                            "id": f"{tag}_alt_{safe_id(other_id)}_unitPrice",
                        },
                        "monthlyAfter": claim_in(
                            other_route,
                            "monthlyAfter",
                            f"{tag}_alt_{safe_id(other_id)}_monthlyAfter",
                        ),
                        "note": "조건부 제안"
                        if other["type"] == "conditional"
                        else "선택되지 않은 제안",
                    }
                )
            entry = {
                "streamId": sid,
                "vendorOrgId": vendor_id,
                "vendorName": names.get(vendor_id) if vendor_id else None,
                "threadId": threads[vendor_id].id if vendor_id in threads else None,
                "route": item["route"],
                "status": status,
                "unitPrice": {**offer["claim"], "id": f"{tag}_unitPrice"} if offer else None,
                "monthlyAfter": claim_in(
                    optimized, item.get("monthlyAfter"), f"{tag}_monthlyAfter"
                ),
                "monthlyNow": claim_in(optimized, item.get("monthlyNow"), f"{tag}_monthlyNow"),
                "difference": claim_in(optimized, item.get("difference"), f"{tag}_difference"),
                "carbon": claim_in(optimized, item.get("carbon"), f"{tag}_carbon"),
                "carbonValue": claim_in(
                    cost, (cost or {}).get("output", {}).get("kau"), f"{tag}_carbonValue"
                ),
                "currentAssumed": bool(item.get("currentAssumed")),
                "checks": checks,
                "alternatives": alternatives,
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
                    "carbon": None,
                    "carbonValue": None,
                    "currentAssumed": False,
                    "checks": [
                        {
                            "key": "skipped",
                            "status": "conditional",
                            "text": item["reason"],
                            "evidence": [],
                        }
                    ],
                    "alternatives": [],
                }
            )
        totals = optimized["output"].get("totals") or {}
        assumptions: list[str] = []
        for source in (optimized, *[u for u in used if u]):
            for claim in source.get("claims") or []:
                assumptions += [e for e in claim["evidence"] if e.startswith("assumption:")]
        for done in work["identify"].values():
            assumptions += done["grade"].get("assumptions") or []
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
        return {
            "status": max(statuses, key=lambda s: WORST[s]),
            "assignments": assignments,
            "totals": {
                key: claim_in(optimized, totals.get(key), f"total_{key}")
                for key in ("monthlyNow", "monthlyAfter", "difference", "carbon")
            },
            "assumptions": self._assumptions(sorted(dict.fromkeys(assumptions)), org_id),
            "sensitivity": sensitivity,
            "referencePrices": reference,
            "carbonInObjective": bool(optimized["output"].get("carbonInObjective")),
            "evidence": list(optimized.get("evidence") or []),
        }

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
            # 합의한 뒤에 업체의 허가 품목이 바뀌어 맞지 않게 됐다. 확정하지 않고 근거와 함께 닫는다
            self._decline_for_permit(session, thread, failed, evidence)
            self._save(session)
            raise OrchestratorError(
                "PERMIT_MISMATCH",
                "업체의 허가 품목이 흐름과 맞지 않게 되어 승인하지 않고 대화를 닫았습니다.",
            )
        if self._verify(session, thread):
            self._save(session)
            raise OrchestratorError(
                "APPROVAL_BLOCKED", "근거 확인에 실패한 항목이 있어 승인할 수 없습니다."
            )
        side = thread.side_of(org_id)
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
        if both:
            self._confirm(session, thread)
        agreed = [t for t in self._threads(session_id) if t.id in session["work"]["assigned"]]
        if agreed and all(t.status == "confirmed" for t in agreed):
            session["status"], session["phase"] = "confirmed", "done"
        elif not agreed:
            session["status"], session["phase"] = "idle", "done"
        self._save(session)
        return session

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
        outcome, trace = self._company(session).compose(
            thread,
            "confirm",
            streams=streams,
            vendor=public_profile(profile, fleet_cost=False),
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
            vendor=public_profile(profile, fleet_cost=False),
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
                f"{document['title']} 검증 " + ("통과" if result.ok else "거부"),
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
                f"{document['title']}을(를) 만들었습니다.",
                thread.id,
                documentId=document["id"],
                document=document["kind"],
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

        def build(kind: str, title: str, lines_for) -> dict[str, Any]:
            claims: list[dict[str, Any]] = []

            def use(claim: dict[str, Any] | None, claim_id: str) -> str | None:
                if not claim or not claim.get("evidence"):
                    return None
                if all(c["id"] != claim_id for c in claims):
                    claims.append({**claim, "id": claim_id})
                return "{{" + claim_id + "}}"

            lines = [
                title,
                f"보내는 곳: {emitter.get('name')} ({emitter['id']})",
                f"받는 곳: {vendor.get('name')} ({vendor['id']})",
                f"작성일: {self.today}",
                f"대화: {thread.id}",
                "",
                *lines_for(use),
                "",
                "이 문서는 앱 안에서만 쓰는 문서이며 계약서나 관청 제출 서류가 아닙니다.",
            ]
            return {
                "kind": kind,
                "threadId": thread.id,
                "title": f"{title} · {vendor.get('name')}",
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
            lines = ["아래 흐름의 수거와 처리를 요청합니다."]
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
                lines.append(f"{number}. {stream.get('name')}: {' / '.join(parts)}")
                for text in (offer or {}).get("conditions") or []:
                    head = f"{stream.get('name')}:"
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
                lines.append(f"한 번 수거량(확정한 흐름을 합친 값): {per_kg}")
            for number, sid in enumerate(assigned, 1):
                facts, tag = stream_facts(sid), safe_id(sid)
                stream = facts["stream"]
                kg = use(facts["kg"], f"{tag}_monthlyKg")
                lines.append(
                    f"{number}. {stream.get('name')}: 월 {kg} 반출"
                    if kg
                    else f"{number}. {stream.get('name')}: 반출 물량 확인 필요"
                )
                cost = memo_result(memo, "cost_carbon", streamId=sid)
                carbon = use(claim_in(cost, "carbonDifference", "x"), f"{tag}_carbon")
                if carbon:
                    lines.append(f"   탄소 변화(현행 혼합 소각 대비, 배출계수는 가정값): {carbon}")
                route = memo_result(memo, "judge_route", streamId=sid)
                for check in (route or {}).get("checks") or []:
                    if check["status"] == "conditional":
                        lines.append(f"   반입 전 확인: {check['text']}")
            lines.append("반입 검수 결과가 조건과 다르면 앱에서 다시 협의합니다.")
            return lines

        return [
            build("request", "수거·처리 요청서", request_lines),
            build("plan", "이행 계획서", plan_lines),
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


def vendor_model(app: Any, session: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """뷰에 붙일 업체 측 모델 표시. 실제로 답을 쓴 구현이 있으면 그것을, 없으면 쓰게 될 구현을 적는다."""
    configured = dict(profile.get("model") or {})
    used = (session.get("work") or {}).get("vendorModels", {}).get(profile["orgId"])
    if used:
        return {**used, "configured": configured, "used": True}
    llm = app.llm_for("vendor", profile["orgId"], configured or None)
    return {**model_label(llm), "configured": configured, "used": False}
