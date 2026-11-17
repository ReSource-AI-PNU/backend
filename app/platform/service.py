from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.platform.adapters import AdapterError
from app.platform.db import CompanyRow, RecipientRow, SessionRow, StreamRow
from app.platform.models import CompanyProfile, PlatformState
from app.platform.routing import distance, route_plan
from app.schemas import Negotiation, NegotiationRound, PickupTerms, RunEvent


def now():
    return datetime.now(UTC).isoformat()


def uid(prefix):
    return f"{prefix}_{uuid4().hex[:16]}"


def company(db, company_id):
    row = db.get(CompanyRow, company_id)
    if not row:
        raise HTTPException(404, "기업을 찾을 수 없습니다")
    return CompanyProfile.model_validate(row.payload)


def load(db, session_id, who, owner=True):
    row = db.get(SessionRow, session_id)
    if not row or (owner and row.org_id != who.company_id):
        raise HTTPException(404, "진단을 찾을 수 없습니다")
    return PlatformState.model_validate(row.payload)


def save(db: DBSession, state, expected):
    state.version = expected + 1
    state.session.updated_at = now()
    result = db.execute(
        update(SessionRow)
        .where(SessionRow.id == state.session.id, SessionRow.version == expected)
        .values(version=state.version, payload=state.model_dump(mode="json", by_alias=True))
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "다른 사용자가 변경했습니다. 새로고침 후 다시 시도하세요")
    project(db, state)
    db.commit()
    return state


def project(db, state):
    """Maintain private stream rows and indexed recipients in the same transaction as CAS."""
    sid = state.session.id
    db.execute(delete(StreamRow).where(StreamRow.session_id == sid))
    db.execute(delete(RecipientRow).where(RecipientRow.session_id == sid))
    for stream in state.session.streams:
        db.add(
            StreamRow(
                session_id=sid,
                stream_id=stream.id,
                payload=stream.model_dump(mode="json", by_alias=True),
            )
        )
    for b in state.bundles:
        ids = {m.company_id for m in b.members if m.state in {"pending", "accepted", "declined"}}
        if b.ready:
            ids.add(b.collector_id)
        for cid in ids:
            db.add(RecipientRow(session_id=sid, company_id=cid, bundle_id=b.id))


def check_version(state, expected, mutable=True):
    if state.version != expected:
        raise HTTPException(409, "오래된 버전입니다. 새로고침하세요")
    if mutable and state.session.approval:
        raise HTTPException(409, "승인한 진단은 변경할 수 없습니다. 새 진단을 만드세요")


def event(state, title, agent="matching", actor="human", kind="result"):
    state.session.run_log = [
        *(state.session.run_log or []),
        RunEvent(id=uid("evt"), at=now(), agent=agent, actor=actor, kind=kind, title=title),
    ]


def safe_bundle(bundle, who):
    b = bundle.model_copy(deep=True)
    collector = b.collector_id == who.company_id
    for m in b.members:
        if m.company_id != who.company_id and not (collector and m.state in {"self", "accepted"}):
            m.kg = None
            m.location = None
            if not any(x.company_id == who.company_id and x.state == "self" for x in b.members):
                m.name = "다른 참여 기업"
    for trip in b.trips:
        for visit in trip.visits:
            if visit.company_id != who.company_id and not collector:
                visit.kg = None
    return b


def safe_state(state, who):
    s = state.model_copy(deep=True)
    s.bundles = [safe_bundle(b, who) for b in s.bundles]
    if s.session.matching:
        for b in s.session.matching.bundles:
            for m in b.group:
                if m.company_id != who.company_id:
                    m.kg = None
    return s


def refresh_bundle(db, b):
    b.candidate_kg = round(sum(m.kg for m in b.members if m.state != "declined"), 3)
    accepted = [m for m in b.members if m.state in {"self", "accepted"}]
    b.confirmed_kg = round(sum(m.kg for m in accepted), 3)
    available = (
        b.confirmed_kg / b.pickups_per_month if b.minimum_basis == "pickup" else b.confirmed_kg
    )
    b.ready = available >= b.min_kg and all(
        m.state in {"self", "accepted", "declined"} for m in b.members
    )
    try:
        b.trips = []
        if b.ready:
            from app.platform.models import Trip
            from app.platform.road import matrix

            depot = company(db, b.collector_id)
            p = depot.collector
            if (
                not p
                or not (p.permit_verified or (settings.platform_demo and depot.is_demo))
                or b.material not in p.materials
                or b.form not in p.forms
                or b.route not in p.routes
                or (b.form in {"film", "rigid"} and "폐플라스틱" not in p.permits)
                or p.min_kg != b.min_kg
                or p.truck_kg != b.truck_kg
                or p.minimum_basis != b.minimum_basis
            ):
                raise ValueError("업체 허가·처리 조건을 다시 검증하세요")
            start, end = depot.pickup_window.start, depot.pickup_window.end
            if b.pickup_slot == "오전":
                end = min(end, 720)
            elif b.pickup_slot == "오후":
                start = max(start, 720)
            if start >= end:
                raise ValueError("합의한 시간대에 수거할 수 없습니다")
            stops = []
            for m in accepted:
                window = company(db, m.company_id).pickup_window
                stops.append(
                    {
                        **m.model_dump(by_alias=True),
                        "kg": m.kg / b.pickups_per_month,
                        "window": (window.start, window.end),
                        "serviceMinutes": window.service_minutes,
                    }
                )

            def road_matrix(points):
                result = matrix(points)
                b.distance_basis = (
                    result[2] if result else "직선거리 추정 · 시속 30km 가정 · 실제 도로 경로 아님"
                )
                return result

            b.trips = [
                Trip.model_validate(t)
                for t in route_plan(
                    depot.location,
                    stops,
                    b.truck_kg,
                    shift=(start, end),
                    matrix_provider=road_matrix,
                )
            ]
    except (ValueError, AdapterError) as exc:
        b.ready, b.trips = False, []
        note = f"경로 보류: {exc}"
        if note not in b.excluded:
            b.excluded.append(note)


def build_bundle(db, state, req, who, excluded_ids=None, collector_id=None):
    from app.platform.models import Bundle, BundleMember

    own = company(db, who.company_id)
    st = next((s for s in state.session.streams if s.id == req.stream_id), None)
    if not st:
        raise HTTPException(404, "흐름을 찾을 수 없습니다")
    if not st.monthly_kg_confirmed or st.monthly_kg <= 0:
        raise HTTPException(409, "월 발생량을 먼저 확인하세요")
    material = st.classify.material.strip().upper()
    if not st.classify.single_material or "+" in material:
        raise HTTPException(409, "혼합 수지는 별도 적합성 검토가 필요합니다")
    # Never infer legal permits from the UI's broad packaging category.
    permit_item = "폐플라스틱" if req.form in {"film", "rigid"} else st.classify.item
    query = select(CompanyRow).where(CompanyRow.complex_id == own.complex_id)
    if db.bind.dialect.name == "postgresql":
        query = query.where(
            text(
                "ST_DWithin(location, "
                "CAST(ST_SetSRID(ST_MakePoint(:lon, :lat),4326) AS geography), :radius)"
            )
        )
        rows = db.scalars(
            query, {"lon": own.location[0], "lat": own.location[1], "radius": req.radius_m}
        ).all()
    else:
        rows = db.scalars(query).all()
    peers, collectors, excluded = [], [], []
    for row in rows:
        c = CompanyProfile.model_validate(row.payload)
        if c.is_demo != own.is_demo:
            continue
        if c.id in (excluded_ids or set()):
            continue
        if c.is_demo and not (settings.platform_demo and own.is_demo):
            continue
        if c.visibility == "private" and c.id != own.id:
            continue
        meters = distance(own.location, c.location)
        if meters > req.radius_m:
            continue
        if c.collector and c.role in {"처리업체", "수요처"}:
            p = c.collector
            verified = p.permit_verified or (settings.platform_demo and c.is_demo)
            if (not collector_id or collector_id == c.id) and (
                verified
                and permit_item in p.permits
                and material in p.materials
                and req.form in p.forms
                and req.route in p.routes
            ):
                collectors.append((meters, c.id, c))
            else:
                excluded.append(f"{c.name}: 허가·수지·성상·처리 경로 중 조건 불일치 또는 미검증")
        if c.id != own.id and c.role == "배출" and req.route == "bundle":
            kg = sum(
                s.monthly_kg
                for s in c.supplies
                if s.material.upper() == material and s.form == req.form and s.origin == st.origin
            )
            if kg > 0:
                peers.append((meters, c.id, c, kg))
    if not collectors:
        raise HTTPException(409, "허가와 처리 조건이 확인된 업체가 없습니다")
    collector = min(collectors, key=lambda x: (x[0], x[1]))[2]
    policy = collector.collector
    members = [
        BundleMember(
            company_id=own.id, name=own.name, kg=st.monthly_kg, location=own.location, state="self"
        )
    ]
    total = st.monthly_kg
    for _, _, c, kg in sorted(peers, key=lambda x: (x[0], x[1])):
        if total >= policy.min_kg:
            break
        members.append(
            BundleMember(company_id=c.id, name=c.name, kg=kg, location=c.location, state="not-sent")
        )
        total += kg
    b = Bundle(
        id=uid("bundle"),
        stream_id=st.id,
        material=material,
        origin=st.origin,
        route=req.route,
        collector_id=collector.id,
        collector_name=collector.name,
        min_kg=policy.min_kg,
        truck_kg=policy.truck_kg,
        members=members,
        candidate_kg=total,
        confirmed_kg=st.monthly_kg,
        ready=False,
        trips=[],
        excluded=excluded,
        is_demo=own.is_demo,
        form=req.form,
        radius_m=req.radius_m,
        minimum_basis=policy.minimum_basis,
    )
    refresh_bundle(db, b)
    return b


def matching_contract(db, state, who):
    from app.schemas import BundleGroup, MatchingResult, StreamPartner

    own = company(db, state.session.org_id)
    bundles, partners = [], []
    for b in state.bundles:
        if b.route != "bundle":
            partners.append(
                StreamPartner(
                    stream_id=b.stream_id,
                    route=b.route,
                    company_id=b.collector_id,
                    name=b.collector_name,
                    distance_m=distance(own.location, company(db, b.collector_id).location),
                    location=own.complex_id,
                    note="허가 품목·수지·성상 대조 결과 · 계약 미확정",
                )
            )
            continue
        group = [m for m in b.members if m.state in {"self", "accepted"}]
        policy = company(db, b.collector_id).collector
        bundles.append(
            BundleGroup(
                stream_id=b.stream_id,
                mode="순회 수거",
                item=next(s.classify.item for s in state.session.streams if s.id == b.stream_id),
                min_kg=b.min_kg,
                truck_kg=b.truck_kg,
                fill_rate=b.confirmed_kg / b.truck_kg,
                solo_fill_rate=group[0].kg / b.truck_kg,
                group=[
                    {
                        "companyId": m.company_id,
                        "name": m.name,
                        "kg": m.kg,
                        "distanceM": distance(own.location, m.location),
                        "isSelf": m.state == "self",
                    }
                    for m in group
                ],
                total_kg=b.confirmed_kg,
                collector={
                    "companyId": b.collector_id,
                    "name": b.collector_name,
                    "distanceM": distance(own.location, company(db, b.collector_id).location),
                    "permits": policy.permits,
                },
                excluded=[],
                schedule=b.schedule,
                cost_share=[
                    {
                        "companyId": m.company_id,
                        "name": m.name,
                        "sharePct": m.kg / b.confirmed_kg * 100,
                    }
                    for m in group
                ],
                visit_order=[v.company_id for t in b.trips for v in t.visits],
                route_m=sum(t.distance_m for t in b.trips),
            )
        )
    return MatchingResult(
        complex_id=own.complex_id, self_location=own.location, bundles=bundles, partners=partners
    )


def within(t, g):
    return t.per_kg >= g.min_per_kg and t.day in g.days and t.per_month >= g.min_per_month


def negotiate(bundle, request, policy):
    """Deterministic local counterparty adapter. No network A2A delivery is claimed."""
    from app.schemas import CompanyRef

    g, requested = request.guardrails, request.requested
    if not within(requested, g):
        raise HTTPException(422, "최초 제안부터 가드레일 안에 있어야 합니다")
    rounds = []

    def add(by, terms, message, accept=False):
        if len(rounds) >= g.max_rounds:
            return False
        rounds.append(
            NegotiationRound(
                by=by,
                at=now(),
                terms=terms,
                message=message,
                within=within(terms, g),
                accept=accept,
            )
        )
        return True

    add("ours", requested, "가드레일 안에서 수거 조건을 제안합니다")
    days = [d for d in g.days if d in policy.days]
    result = Negotiation(
        id=uid("neg"),
        stream_id=bundle.stream_id,
        counterparty=CompanyRef(company_id=bundle.collector_id, name=bundle.collector_name),
        guardrails=g,
        requested=requested,
        rounds=rounds,
        status="failed",
        started_at=now(),
        note="모의 상대 에이전트 · 합의 불가 또는 최대 라운드 도달 · 사람 확인 필요",
    )
    if not days:
        return result
    counter = PickupTerms(
        day=days[0],
        slot=requested.slot,
        per_kg=min(requested.per_kg, policy.max_per_kg),
        per_month=min(requested.per_month, policy.max_per_month),
    )
    if (
        add("theirs", counter, "업체 등록 조건으로 생성한 모의 회신")
        and within(counter, g)
        and add("ours", counter, "사람의 최종 승인을 요청합니다", True)
    ):
        result.status, result.agreed = "agreed", counter
        result.note = "모의 협상 합의안 · 사람 승인 전 미확정"
    result.rounds = rounds
    return result
