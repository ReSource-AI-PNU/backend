from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.platform import service as svc
from app.platform.db import CompanyRow, RecipientRow, SessionRow, database
from app.platform.models import (
    AdvanceRequest,
    AnswerRequest,
    ApprovalRequest,
    CommandRequest,
    CommandResult,
    CompanyProfile,
    CreateSession,
    InboxItem,
    InviteRequest,
    MatchRequest,
    NegotiationRequest,
    NegotiationResult,
    PlatformState,
    ProgressResult,
    QuestionReply,
    ReplyRequest,
    Versioned,
)
from app.platform.security import Actor, actor, require_approver
from app.schemas import Question, Session

router = APIRouter(tags=["track-d"])
DB = Annotated[DBSession, Depends(database)]
Who = Annotated[Actor, Depends(actor)]


@router.get("/companies")
def list_companies(db: DB, who: Who, complex_id: str = "noksan") -> list[dict]:
    rows = db.scalars(select(CompanyRow).where(CompanyRow.complex_id == complex_id)).all()
    result = []
    for row in rows:
        c = CompanyProfile.model_validate(row.payload)
        if c.is_demo != who.is_demo:
            continue
        if c.visibility == "private" and c.id != who.company_id:
            continue
        if c.is_demo and not settings.platform_demo:
            continue
        data = c.model_dump(mode="json", by_alias=True)
        if c.id != who.company_id:
            data["supplies"] = [
                {k: v for k, v in s.items() if k != "monthlyKg"} for s in data["supplies"]
            ]
            if data["collector"]:
                data["collector"].pop("maxPerKg", None)
        result.append(data)
    return result


@router.get("/companies/me", response_model=CompanyProfile)
def my_company(db: DB, who: Who):
    return svc.company(db, who.company_id)


@router.put("/companies/{company_id}", response_model=CompanyProfile)
def put_company(company_id: str, body: CompanyProfile, db: DB, who: Who):
    if who.company_id != company_id or body.id != company_id:
        raise HTTPException(403, "자기 회사 프로필만 수정할 수 있습니다")
    require_approver(who)
    existing = db.get(CompanyRow, company_id)
    body.is_demo = bool(existing and existing.payload.get("isDemo"))
    if body.is_demo and not settings.platform_demo:
        raise HTTPException(422, "가상 기업 모드가 꺼져 있습니다")
    # Permit verification is an operator task; profile edits cannot certify themselves.
    if body.collector:
        prior = existing.payload.get("collector") if existing else None
        current = body.collector.model_dump(mode="json", by_alias=True)
        body.collector.permit_verified = bool(
            prior
            and prior.get("permitVerified")
            and {k: v for k, v in prior.items() if k != "permitVerified"}
            == {k: v for k, v in current.items() if k != "permitVerified"}
        )
    row = CompanyRow(
        id=body.id,
        complex_id=body.complex_id,
        owner_id=who.user_id,
        longitude=body.location[0],
        latitude=body.location[1],
        payload=body.model_dump(mode="json", by_alias=True),
    )
    db.merge(row)
    db.commit()
    return body


@router.post("/sessions", response_model=PlatformState, status_code=201)
def create_session(body: CreateSession, db: DB, who: Who):
    if body.company_id != who.company_id:
        raise HTTPException(403, "자기 회사에서만 진단을 만들 수 있습니다")
    c = svc.company(db, who.company_id)
    if c.is_demo and not settings.platform_demo:
        raise HTTPException(409, "가상 기업 모드가 꺼져 있습니다")
    timestamp = svc.now()
    questions = [
        Question(
            id=f"volume:{s.id}",
            step="classify",
            stream_id=s.id,
            text=f"{s.name} 월 발생량을 확인하세요",
            why="확인한 물량으로만 수거를 계획합니다",
            kind="number",
            unit="kg",
        )
        for s in body.streams
        if not s.monthly_kg_confirmed
    ]
    state = PlatformState(
        version=1,
        session=Session(
            id=svc.uid("ses"),
            org_id=c.id,
            owner_id=who.user_id,
            complex_id=c.complex_id,
            title=body.title,
            created_at=timestamp,
            updated_at=timestamp,
            status="in-progress",
            step="classify",
            streams=body.streams,
            questions=questions,
            corrections=[],
            run_log=[],
        ),
    )
    svc.event(state, "진단 세션 생성", "orchestrator")
    db.add(
        SessionRow(
            id=state.session.id,
            org_id=c.id,
            version=1,
            payload=state.model_dump(mode="json", by_alias=True),
        )
    )
    db.flush()
    svc.project(db, state)
    db.commit()
    return state


@router.get("/sessions", response_model=list[PlatformState])
def sessions(db: DB, who: Who):
    rows = db.scalars(select(SessionRow).where(SessionRow.org_id == who.company_id)).all()
    rows = sorted(rows, key=lambda row: row.payload["session"]["createdAt"])
    return [svc.safe_state(PlatformState.model_validate(r.payload), who) for r in rows]


@router.get("/sessions/{session_id}", response_model=PlatformState)
def session(session_id: str, db: DB, who: Who):
    return svc.safe_state(svc.load(db, session_id, who), who)


@router.post("/sessions/{session_id}/answers", response_model=PlatformState)
def answer(session_id: str, body: AnswerRequest, db: DB, who: Who):
    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    if state.session.negotiations:
        raise HTTPException(409, "협상 후 물량 변경은 새 진단으로 진행하세요")
    if any(
        m.state in {"pending", "accepted", "declined"} for b in state.bundles for m in b.members
    ):
        raise HTTPException(409, "참여 제안 후에는 물량을 변경할 수 없습니다. 새 진단을 만드세요")
    stream = next((s for s in state.session.streams if s.id == body.stream_id), None)
    if not stream:
        raise HTTPException(404, "흐름을 찾을 수 없습니다")
    stream.monthly_kg, stream.monthly_kg_confirmed = body.monthly_kg, True
    stream.origin, stream.origin_source = body.origin, "human"
    for q in state.session.questions:
        if q.id == f"volume:{stream.id}":
            q.answer = str(body.monthly_kg)
    state.bundles = []
    state.session.feasibility = state.session.regulation = state.session.matching = None
    state.session.cost_carbon = state.session.proposal = None
    svc.event(state, f"{stream.name} 물량·출처 확인 · 후속 결과 초기화", "orchestrator")
    return svc.safe_state(svc.save(db, state, body.version), who)


@router.post("/sessions/{session_id}/matching", response_model=PlatformState)
def matching(session_id: str, body: MatchRequest, db: DB, who: Who):
    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    existing = [b for b in state.bundles if b.stream_id == body.stream_id]
    if any(m.state != "not-sent" and m.state != "self" for b in existing for m in b.members):
        raise HTTPException(409, "제안 후 재매칭은 새 진단으로 진행하세요")
    if any(n.stream_id == body.stream_id for n in state.session.negotiations or []):
        raise HTTPException(409, "협상 시작 후 재매칭할 수 없습니다")
    bundle = svc.build_bundle(db, state, body, who)
    state.bundles = [b for b in state.bundles if b.stream_id != body.stream_id] + [bundle]
    state.session.step = "matching"
    svc.event(state, "업체 조건 대조·수거 후보 계산 · 참여 동의 전 미확정", actor="ai")
    return svc.safe_state(svc.save(db, state, body.version), who)


def get_bundle(state, bundle_id):
    b = next((b for b in state.bundles if b.id == bundle_id), None)
    if not b:
        raise HTTPException(404, "묶음을 찾을 수 없습니다")
    return b


@router.post("/sessions/{session_id}/advance", response_model=ProgressResult)
def advance(session_id: str, body: AdvanceRequest, db: DB, who: Who):
    from app.platform.adapters import AdapterError
    from app.platform.orchestrator import advance as run_stages
    from app.platform.stages import handler

    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    if state.session.approval:
        raise HTTPException(409, "승인한 진단은 변경할 수 없습니다")
    demo = settings.platform_demo and svc.company(db, who.company_id).is_demo
    handlers = {
        step: handler(step, state, demo)
        for step in ["feasibility", "regulation", "cost-carbon", "proposal"]
    }

    def match_stage(session):
        for d in session.feasibility.decisions:
            if d.route in {"bundle", "sell", "pyrolysis"} and not any(
                b.stream_id == d.stream_id for b in state.bundles
            ):
                stream = next(s for s in session.streams if s.id == d.stream_id)
                form = (
                    "film"
                    if "필름" in stream.name or stream.classify.material in {"LDPE", "LLDPE"}
                    else "rigid"
                )
                try:
                    state.bundles.append(
                        svc.build_bundle(
                            db,
                            state,
                            MatchRequest(
                                version=state.version,
                                stream_id=d.stream_id,
                                form=form,
                                route=d.route,
                            ),
                            who,
                        )
                    )
                except HTTPException as exc:
                    raise AdapterError(str(exc.detail)) from exc
        return svc.matching_contract(db, state, who)

    handlers["matching"] = match_stage
    blocked, reason = run_stages(state, body.step, handlers)
    svc.event(state, reason or f"{body.step}까지 실행 완료", "orchestrator", "ai")
    svc.save(db, state, body.version)
    return ProgressResult(state=svc.safe_state(state, who), blocked_step=blocked, reason=reason)


@router.post("/sessions/{session_id}/classify", response_model=PlatformState)
def classify(session_id: str, body: Versioned, db: DB, who: Who):
    from pydantic import ValidationError

    from app.platform.adapters import AdapterError
    from app.platform.stages import classify as classify_stage

    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    if state.bundles or state.session.approval:
        raise HTTPException(409, "매칭 이후 재식별은 새 진단으로 진행하세요")
    try:
        classify_stage(state)
    except (AdapterError, ValidationError, KeyError) as exc:
        raise HTTPException(502, "식별 서비스 연결 또는 데이터 계약을 확인하세요") from exc
    state.session.feasibility = state.session.regulation = state.session.matching = None
    state.session.cost_carbon = state.session.proposal = None
    svc.event(state, "식별 서비스 결과 수신", "classify", "ai")
    return svc.safe_state(svc.save(db, state, body.version), who)


@router.post("/sessions/{session_id}/questions", response_model=PlatformState)
def answer_question(session_id: str, body: QuestionReply, db: DB, who: Who):
    from app.platform.orchestrator import FIELDS, STEPS

    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    if state.session.approval or state.session.negotiations or state.bundles:
        raise HTTPException(409, "매칭 이후 질문 변경은 새 진단으로 진행하세요")
    question = next((q for q in state.session.questions if q.id == body.question_id), None)
    if not question:
        raise HTTPException(404, "질문이 없습니다")
    if question.id.startswith("volume:"):
        raise HTTPException(422, "물량·출처 확인 API를 사용하세요")
    if question.kind == "choice" and body.answer not in (question.options or []):
        raise HTTPException(422, "선택지 중에서 답하세요")
    if question.kind == "number":
        import math

        try:
            if not math.isfinite(float(body.answer)) or float(body.answer) < 0:
                raise ValueError()
        except ValueError as exc:
            raise HTTPException(422, "0 이상의 유한한 숫자가 필요합니다") from exc
    question.answer = body.answer
    for step in STEPS[max(1, STEPS.index(question.step)) :]:
        setattr(state.session, FIELDS[step], None)
    svc.event(state, f"확인 질문 답변: {question.id}", "orchestrator")
    return svc.safe_state(svc.save(db, state, body.version), who)


@router.post("/sessions/{session_id}/invites", response_model=PlatformState)
def invite(session_id: str, body: InviteRequest, db: DB, who: Who):
    require_approver(who)
    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    bundle = get_bundle(state, body.bundle_id)
    changed = False
    for m in bundle.members:
        if m.state == "not-sent":
            m.state, changed = "pending", True
    if not changed:
        return svc.safe_state(state, who)
    svc.event(state, "참여 제안 승인 · 내부 수신함에 등록(외부 메일 미발송)")
    return svc.safe_state(svc.save(db, state, body.version), who)


@router.get("/inbox", response_model=list[InboxItem])
def inbox(db: DB, who: Who):
    result = []
    allowed = select(RecipientRow.session_id).where(RecipientRow.company_id == who.company_id)
    rows = sorted(
        db.scalars(select(SessionRow).where(SessionRow.id.in_(allowed))),
        key=lambda row: row.payload["session"]["createdAt"],
    )
    for row in rows:
        state = PlatformState.model_validate(row.payload)
        for b in state.bundles:
            recipient = any(
                m.company_id == who.company_id and m.state in {"pending", "accepted", "declined"}
                for m in b.members
            )
            if recipient or (b.collector_id == who.company_id and b.ready):
                result.append(
                    InboxItem(
                        session_id=state.session.id,
                        version=state.version,
                        bundle=svc.safe_bundle(b, who),
                    )
                )
    return result


@router.post("/sessions/{session_id}/bundles/{bundle_id}/reply", response_model=InboxItem)
def reply(session_id: str, bundle_id: str, body: ReplyRequest, db: DB, who: Who):
    state = svc.load(db, session_id, who, owner=False)
    b = get_bundle(state, bundle_id)
    member = next(
        (
            m
            for m in b.members
            if m.company_id == who.company_id and m.state != "self" and m.state != "not-sent"
        ),
        None,
    )
    if not member:
        raise HTTPException(404, "받은 참여 제안을 찾을 수 없습니다")
    svc.check_version(state, body.version)
    if member.state in {"accepted", "declined"}:
        if member.state != body.decision or member.reason != body.reason:
            raise HTTPException(409, "이미 회신했습니다. 조건 변경은 새 제안으로 진행하세요")
        return InboxItem(
            session_id=session_id, version=state.version, bundle=svc.safe_bundle(b, who)
        )
    member.state, member.reason = body.decision, body.reason
    svc.refresh_bundle(db, b)
    state.session.matching = svc.matching_contract(db, state, None)
    state.session.cost_carbon = state.session.proposal = None
    svc.event(state, f"{member.name} 참여 {body.decision} · 확정 물량 재계산")
    svc.save(db, state, body.version)
    return InboxItem(session_id=session_id, version=state.version, bundle=svc.safe_bundle(b, who))


@router.post("/sessions/{session_id}/bundles/{bundle_id}/replace", response_model=PlatformState)
def replace_members(session_id: str, bundle_id: str, body: Versioned, db: DB, who: Who):
    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    b = get_bundle(state, bundle_id)
    if state.session.approval or any(
        n.status == "approved" and n.stream_id == b.stream_id
        for n in state.session.negotiations or []
    ):
        raise HTTPException(409, "승인 후에는 참여 기업을 변경할 수 없습니다")
    if b.ready:
        raise HTTPException(409, "이미 수거 조건을 충족했습니다")
    candidate = svc.build_bundle(
        db,
        state,
        MatchRequest(
            version=body.version,
            stream_id=b.stream_id,
            form=b.form,
            route=b.route,
            radius_m=b.radius_m,
        ),
        who,
        excluded_ids={m.company_id for m in b.members if m.state != "self"},
        collector_id=b.collector_id,
    )
    volume = sum(m.kg for m in b.members if m.state != "declined")
    target = b.min_kg * b.pickups_per_month if b.minimum_basis == "pickup" else b.min_kg
    for m in candidate.members[1:]:
        if volume >= target:
            break
        b.members.append(m)
        volume += m.kg
    svc.refresh_bundle(db, b)
    state.session.negotiations = [
        n for n in state.session.negotiations or [] if n.stream_id != b.stream_id
    ]
    state.session.matching = svc.matching_contract(db, state, None)
    state.session.cost_carbon = state.session.proposal = None
    svc.event(state, "대체 참여 후보 검색 · 새 후보는 제안 승인과 동의 필요")
    return svc.safe_state(svc.save(db, state, body.version), who)


@router.post("/sessions/{session_id}/negotiations", response_model=NegotiationResult)
def negotiation(session_id: str, body: NegotiationRequest, db: DB, who: Who):
    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    b = get_bundle(state, body.bundle_id)
    svc.refresh_bundle(db, b)
    if not b.ready:
        raise HTTPException(409, "참여 회신과 최소 수거 물량을 먼저 충족해야 합니다")
    if any(
        n.stream_id == b.stream_id and n.status == "approved"
        for n in state.session.negotiations or []
    ):
        raise HTTPException(409, "이미 승인된 합의안입니다")
    policy = svc.company(db, b.collector_id).collector
    peer = settings.platform_a2a_peers.get(b.collector_id)
    if peer:
        from app.platform.adapters import AdapterError, a2a_negotiate

        try:
            n = a2a_negotiate(b, body, peer, f"{session_id}:{b.id}:{body.version}")
        except AdapterError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif settings.platform_demo and b.is_demo:
        n = svc.negotiate(b, body, policy)
    else:
        raise HTTPException(503, "업체 A2A 주소와 인증정보를 설정하세요")
    if n.agreed and b.minimum_basis == "pickup" and b.confirmed_kg / n.agreed.per_month < b.min_kg:
        n.status, n.agreed, n.note = "failed", None, "회차별 물량이 업체 최소 수거량보다 작습니다"
    state.session.negotiations = [
        x for x in state.session.negotiations or [] if x.stream_id != b.stream_id
    ] + [n]
    svc.event(
        state,
        f"{'외부' if peer else '모의'} 상대 에이전트 협상 · {n.status} · 사람 승인 필요",
        "negotiation",
        "ai",
    )
    svc.save(db, state, body.version)
    return NegotiationResult(negotiation=n, state=svc.safe_state(state, who))


@router.post(
    "/sessions/{session_id}/negotiations/{negotiation_id}/approve", response_model=PlatformState
)
def approve(session_id: str, negotiation_id: str, body: ApprovalRequest, db: DB, who: Who):
    require_approver(who)
    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    n = next((n for n in state.session.negotiations or [] if n.id == negotiation_id), None)
    if not n or n.status != "agreed" or not n.agreed or not svc.within(n.agreed, n.guardrails):
        raise HTTPException(409, "가드레일 안의 미승인 합의안이 없습니다")
    b = next(b for b in state.bundles if b.stream_id == n.stream_id)
    if not b.ready:
        raise HTTPException(409, "현재 묶음 조건을 다시 확인하세요")
    n.status, n.approved_by, n.approved_at = "approved", who.user_id, svc.now()
    n.note = "협상 합의안 승인 완료 · 외부 계약·발송은 별도 이행안 승인 필요"
    b.pickups_per_month = n.agreed.per_month
    b.pickup_slot = n.agreed.slot
    svc.refresh_bundle(db, b)
    if not b.ready:
        raise HTTPException(409, "회차별 최소 수거 물량을 충족하지 않습니다")
    b.schedule = f"{n.agreed.day}요일 {n.agreed.slot} · 월 {n.agreed.per_month}회"
    svc.event(state, "사람이 협상 합의안 승인 · 외부 계약 미체결", "negotiation")
    return svc.safe_state(svc.save(db, state, body.version), who)


@router.post("/sessions/{session_id}/commands", response_model=CommandResult)
def command(session_id: str, body: CommandRequest, db: DB, who: Who):
    import re

    from app.agents.economics_adapter import evaluate_streams
    from app.demo.synthetic import CONFIG

    state = svc.load(db, session_id, who)
    svc.check_version(state, body.version)
    text = body.text.strip()
    route_words = {
        "재투입": "reuse",
        "매각": "sell",
        "묶음": "bundle",
        "열분해": "pyrolysis",
        "소각": "incinerate",
    }
    route = next((value for word, value in route_words.items() if word in text), None)
    streams = [
        s
        for s in state.session.streams
        if s.id in text
        or any(token in text for token in re.split(r"[\s·]+", s.name) if len(token) >= 2)
    ]
    locked = bool(state.session.negotiations) or any(
        m.state in {"pending", "accepted", "declined"} for b in state.bundles for m in b.members
    )
    svc.event(state, text, "orchestrator", "human", "command")
    if any(word in text for word in ["승인", "발송", "보내", "계약"]):
        message = "승인과 발송은 명령으로 실행하지 않습니다. 승인 버튼을 사용하세요."
    elif route and any(word in text for word in ["바꿔", "변경"]):
        if locked:
            message = "참여 제안·협상 후 경로 변경은 새 진단으로 진행하세요."
        elif len(streams) != 1:
            message = "변경할 흐름을 하나만 지정하세요. 흐름 이름 또는 ID가 필요합니다."
        elif not settings.platform_demo or not svc.company(db, who.company_id).is_demo:
            from pydantic import ValidationError

            from app.platform.adapters import AdapterError
            from app.platform.stages import handler
            from app.schemas import Correction

            candidate = state.model_copy(deep=True)
            candidate.session.corrections.append(
                Correction(
                    id=svc.uid("correction"),
                    at=svc.now(),
                    by=who.user_id,
                    step="feasibility",
                    stream_id=streams[0].id,
                    field="route",
                    before=next(
                        (
                            d.route or ""
                            for d in (
                                state.session.feasibility.decisions
                                if state.session.feasibility
                                else []
                            )
                            if d.stream_id == streams[0].id
                        ),
                        "",
                    ),
                    after=route,
                    rationale=text,
                )
            )
            try:
                result = handler("feasibility", candidate)(candidate.session)
                decision = next(d for d in result.decisions if d.stream_id == streams[0].id)
                if result.status == "needs_information" or decision.route != route:
                    raise AdapterError("요청한 경로에 필요한 정보·검토가 부족합니다")
                state.session = candidate.session
                state.session.feasibility = result
                state.session.regulation = state.session.matching = None
                state.session.cost_carbon = state.session.proposal = None
                state.bundles = []
                message = "B 서비스 검토 결과로 경로를 변경했습니다. 후속 단계를 다시 실행하세요."
            except (AdapterError, ValidationError, KeyError, StopIteration):
                message = (
                    "경로를 변경하지 않았습니다. B 서비스 연결·판정 결과·확인 질문을 검토하세요."
                )
        else:
            previous = {
                d.stream_id: d.route
                for d in (state.session.feasibility.decisions if state.session.feasibility else [])
                if d.route
            }
            previous[streams[0].id] = route
            result = evaluate_streams(state.session.streams, CONFIG, previous)
            if result.status == "needs_information":
                message = "이 경로에 필요한 정보가 부족해 변경하지 않았습니다."
            else:
                state.session.feasibility = result
                state.session.regulation = state.session.matching = None
                state.session.cost_carbon = state.session.proposal = None
                state.bundles = []
                message = f"{streams[0].name} 경로를 {route}로 변경했습니다. 후속 검토가 필요합니다(가상 계산)."
    elif "다시" in text and any(word in text for word in ["규정", "매칭", "비용", "이행"]):
        if locked:
            message = "참여 제안·협상 후 재계산은 새 진단으로 진행하세요."
        else:
            from app.platform.orchestrator import FIELDS, STEPS

            target = next(
                value
                for word, value in {
                    "규정": "regulation",
                    "매칭": "matching",
                    "비용": "cost-carbon",
                    "이행": "proposal",
                }.items()
                if word in text
            )
            for step in STEPS[STEPS.index(target) :]:
                setattr(state.session, FIELDS[step], None)
            if STEPS.index(target) <= STEPS.index("matching"):
                state.bundles = []
            message = f"{target} 이후 결과를 초기화했습니다. 단계 실행 API로 다시 검토하세요."
    elif any(word in text for word in ["요약", "상황", "지금"]):
        message = (
            f"흐름 {len(state.session.streams)}개 · 묶음 {len(state.bundles)}개 · "
            f"수거 조건 충족 {sum(b.ready for b in state.bundles)}개"
        )
    else:
        message = "지원 명령: 현재 상황 요약, 필름은 매각으로 바꿔 줘, 규정 다시 검토해 줘."
    svc.event(state, message, "orchestrator", "ai")
    return CommandResult(
        message=message, state=svc.safe_state(svc.save(db, state, body.version), who)
    )
