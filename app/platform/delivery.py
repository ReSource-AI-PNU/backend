"""Freeze reviewed documents. Sending is a separate, explicit and idempotent action."""

import time

from fastapi import APIRouter, HTTPException
from sqlalchemy import and_, or_, select, update

from app.core.config import settings
from app.platform import service as svc
from app.platform.adapters import AdapterError, call
from app.platform.api import DB, Who
from app.platform.db import DeliveryRow
from app.platform.models import ApprovalRequest, PlatformState, ProposalApproval
from app.platform.security import require_approver
from app.schemas import Approval, SentTo

router = APIRouter(tags=["delivery"])


@router.post("/sessions/{sid}/approval", response_model=PlatformState)
def approve_proposal(sid: str, body: ProposalApproval, db: DB, who: Who):
    require_approver(who)
    state = svc.load(db, sid, who)
    svc.check_version(state, body.version)
    for b in state.bundles:
        svc.refresh_bundle(db, b)
    s = state.session
    if (
        not s.proposal
        or not s.cost_carbon
        or s.status != "awaiting-approval"
        or any(q.answer is None for q in s.questions)
        or any(not b.ready for b in state.bundles)
    ):
        raise HTTPException(409, "전체 단계와 확인 질문·참여 동의를 먼저 완료하세요")
    if body.route_id not in {r.id for r in s.cost_carbon.routes}:
        raise HTTPException(422, "검토한 시나리오만 승인할 수 있습니다")
    if any(n.status != "approved" for n in s.negotiations or []):
        raise HTTPException(409, "진행한 협상을 먼저 승인하거나 새 진단으로 진행하세요")
    s.approval = Approval(
        approved_by=who.user_id, approved_at=svc.now(), route_id=body.route_id, sent_to=[]
    )
    s.status = "approved"
    for doc in s.proposal.documents:
        if doc.external:
            db.add(
                DeliveryRow(
                    id=svc.uid("delivery"),
                    session_id=sid,
                    status="pending",
                    payload={
                        "document": doc.model_dump(mode="json", by_alias=True),
                        "approval": s.approval.model_dump(mode="json", by_alias=True),
                        "isDemo": svc.company(db, who.company_id).is_demo,
                    },
                )
            )
    svc.event(state, "이행안 승인 · 문서 동결 · 외부 발송은 별도 확인 필요", "proposal")
    return svc.safe_state(svc.save(db, state, body.version), who)


@router.get("/sessions/{sid}/deliveries")
def deliveries(sid: str, db: DB, who: Who):
    svc.load(db, sid, who)
    return [
        {
            "id": r.id,
            "status": r.status,
            "title": r.payload["document"]["title"],
            "to": r.payload["document"]["to"],
            "receipt": r.payload.get("receipt"),
            "note": r.payload.get("note", ""),
        }
        for r in db.scalars(select(DeliveryRow).where(DeliveryRow.session_id == sid))
    ]


@router.post("/sessions/{sid}/deliveries")
def send(sid: str, body: ApprovalRequest, db: DB, who: Who):
    require_approver(who)
    state = svc.load(db, sid, who)
    svc.check_version(state, body.version, mutable=False)
    if not state.session.approval:
        raise HTTPException(409, "이행안 승인이 필요합니다")
    url = settings.platform_stage_endpoints.get("dispatch")
    if not url:
        raise HTTPException(503, "C 트랙 발송 서비스 주소 설정이 필요합니다")
    rows = list(db.scalars(select(DeliveryRow).where(DeliveryRow.session_id == sid)))
    for row in rows:
        eligible = or_(
            DeliveryRow.status.in_(["pending", "uncertain"]),
            and_(DeliveryRow.status == "sending", DeliveryRow.lease_until < int(time.time())),
        )
        if row.status == "sent" or (row.status == "sending" and row.lease_until >= time.time()):
            continue
        if row.payload.get("isDemo"):
            raise HTTPException(409, "가상 문서는 외부 발송할 수 없습니다")
        claimed = db.execute(
            update(DeliveryRow)
            .where(DeliveryRow.id == row.id, eligible)
            .values(
                status="sending",
                lease_until=int(time.time() + settings.platform_adapter_timeout + 60),
            )
        )
        db.commit()  # durable claim before any network side effect
        if claimed.rowcount != 1:
            continue
        try:
            result = call(
                url,
                {
                    "contractVersion": "resource-dispatch-v1",
                    "deliveryId": row.id,
                    "sessionId": sid,
                    **row.payload,
                },
                settings.platform_stage_token,
                row.id,
            )
            if (
                result.get("status") != "sent"
                or not isinstance(result.get("receiptId"), str)
                or not result["receiptId"]
            ):
                raise AdapterError("발송 확인 영수증이 없습니다")
            row.status, row.payload = (
                "sent",
                {**row.payload, "receipt": result["receiptId"], "sentAt": svc.now()},
            )
        except AdapterError:
            # The remote server MUST deduplicate deliveryId. A timeout does not prove non-delivery.
            row.status, row.payload = (
                "uncertain",
                {**row.payload, "note": "발송 여부 미확인. 같은 발송 ID로만 재확인하세요."},
            )
        db.commit()
    # Reload the newest version so concurrent independent deliveries cannot overwrite one another.
    db.expire_all()
    state = svc.load(db, sid, who)
    sent = list(
        db.scalars(
            select(DeliveryRow).where(DeliveryRow.session_id == sid, DeliveryRow.status == "sent")
        )
    )
    state.session.approval.sent_to = [
        SentTo(name=r.payload["document"]["to"], kind=r.payload["document"]["kind"]) for r in sent
    ]
    svc.event(state, f"외부 서비스 발송 영수증 {len(sent)}건 확인", "proposal")
    svc.save(db, state, state.version)
    return deliveries(sid, db, who)
