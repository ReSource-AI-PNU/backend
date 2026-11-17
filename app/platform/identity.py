"""Authenticated user identity comes from Auth.js BFF; roles come from this database."""

import hashlib
import hmac
import secrets
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.platform.db import CompanyRow, MemberRow, OrganizationRow, database
from app.platform.models import CompanyProfile
from app.platform.service import now, uid
from app.schemas import Contract, Organization, WasteStream

router = APIRouter(tags=["identity"])
DB = Annotated[Session, Depends(database)]


def principal(
    authorization: str = Header(default=""),
    x_user_id: str = Header(default=""),
    x_user_email: str = Header(default=""),
):
    if (
        not settings.platform_service_key
        or not hmac.compare_digest(authorization, f"Bearer {settings.platform_service_key}")
        or not x_user_id
    ):
        raise HTTPException(401, "인증된 서버의 로그인 정보가 필요합니다")
    return {"id": x_user_id, "email": x_user_email.strip().lower()}


User = Annotated[dict, Depends(principal)]


class OrganizationInput(Contract):
    name: str = Field(min_length=2, max_length=100)
    biz_no: str = Field(pattern=r"^\d{10}$")
    industry: str = Field(min_length=1, max_length=100)
    complex_id: str = Field(pattern=r"^[a-z0-9-]{1,100}$")
    role: Literal["배출", "처리업체", "수요처"]
    contact_title: str = Field(default="", max_length=100)
    waste_streams: list[WasteStream]
    location: (
        tuple[Annotated[float, Field(ge=-180, le=180)], Annotated[float, Field(ge=-90, le=90)]]
        | None
    ) = None


class Invite(Contract):
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=200)
    role: Literal["관리자", "승인권자", "담당자"]


class RoleUpdate(Contract):
    role: Literal["관리자", "승인권자", "담당자"]


class AcceptInvite(Contract):
    token: str = Field(min_length=32, max_length=200)


def membership(db, user_id, company_id, admin=False):
    row = db.scalar(
        select(MemberRow).where(MemberRow.user_id == user_id, MemberRow.company_id == company_id)
    )
    if not row or row.payload["status"] != "active" or (admin and row.role != "관리자"):
        raise HTTPException(403, "회사 멤버 권한이 없습니다")
    return row


@router.get("/identity")
def identity(db: DB, user: User):
    rows = db.scalars(select(MemberRow).where(MemberRow.user_id == user["id"])).all()
    return {
        "userId": user["id"],
        "memberships": [r.payload for r in rows],
        "organizations": [db.get(OrganizationRow, r.company_id).payload for r in rows],
    }


@router.post("/organizations", response_model=Organization)
def organization(body: OrganizationInput, db: DB, user: User):
    if not user["email"]:
        raise HTTPException(422, "이메일이 있는 로그인 계정이 필요합니다")
    row = db.scalar(select(OrganizationRow).where(OrganizationRow.owner_id == user["id"]))
    timestamp, cid = now(), row.id if row else uid("org")
    data = Organization(
        id=cid,
        owner_id=user["id"],
        created_at=row.payload["createdAt"] if row else timestamp,
        updated_at=timestamp,
        **body.model_dump(exclude={"location"}),
    )
    if row:
        row.biz_no, row.payload = body.biz_no, data.model_dump(by_alias=True, mode="json")
    else:
        db.add(
            OrganizationRow(
                id=cid,
                owner_id=user["id"],
                biz_no=body.biz_no,
                payload=data.model_dump(by_alias=True, mode="json"),
            )
        )
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "이미 등록된 사업자번호 또는 회사입니다") from exc
        mid = uid("mem")
        db.add(
            MemberRow(
                id=mid,
                company_id=cid,
                user_id=user["id"],
                email=user["email"],
                role="관리자",
                payload={
                    "id": mid,
                    "orgId": cid,
                    "userId": user["id"],
                    "email": user["email"],
                    "name": body.name,
                    "role": "관리자",
                    "owner": True,
                    "status": "active",
                    "invitedAt": timestamp,
                },
            )
        )
    # Coordinates must be provided by the company; do not assign a nearby demo factory.
    if body.location is not None:
        profile = CompanyProfile(
            id=cid,
            name=body.name,
            complex_id=body.complex_id,
            location=body.location,
            role=body.role,
        )
        existing = db.get(CompanyRow, cid)
        if existing:
            profile = CompanyProfile.model_validate(
                {
                    **existing.payload,
                    "name": body.name,
                    "complexId": body.complex_id,
                    "location": body.location,
                    "role": body.role,
                }
            )
        db.merge(
            CompanyRow(
                id=cid,
                owner_id=user["id"],
                complex_id=body.complex_id,
                longitude=profile.location[0],
                latitude=profile.location[1],
                payload=profile.model_dump(by_alias=True, mode="json"),
            )
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "이미 등록된 사업자번호 또는 회사입니다") from exc
    return data


@router.get("/organizations/{cid}/members")
def members(cid: str, db: DB, user: User):
    membership(db, user["id"], cid)
    return [r.payload for r in db.scalars(select(MemberRow).where(MemberRow.company_id == cid))]


@router.post("/organizations/{cid}/members")
def invite(cid: str, body: Invite, db: DB, user: User):
    membership(db, user["id"], cid, admin=True)
    token, mid = secrets.token_urlsafe(32), uid("mem")
    row = MemberRow(
        id=mid,
        company_id=cid,
        email=body.email.lower(),
        role=body.role,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=int(time.time()) + 7 * 86400,
        payload={
            "id": mid,
            "orgId": cid,
            "email": body.email.lower(),
            "name": "",
            "role": body.role,
            "status": "invited",
            "invitedAt": now(),
        },
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "이미 가입했거나 초대한 이메일입니다") from exc
    return {"member": row.payload, "token": token}  # displayed once, never emailed automatically


@router.post("/membership/accept")
def accept(body: AcceptInvite, db: DB, user: User):
    digest = hashlib.sha256(body.token.encode()).hexdigest()
    row = db.scalar(select(MemberRow).where(MemberRow.token_hash == digest))
    if not row or row.email != user["email"] or (row.expires_at or 0) < time.time():
        raise HTTPException(404, "초대가 만료되었거나 로그인 이메일이 다릅니다")
    existing = db.scalar(
        select(MemberRow).where(
            MemberRow.user_id == user["id"], MemberRow.company_id == row.company_id
        )
    )
    if existing:
        raise HTTPException(409, "이미 이 회사의 멤버입니다")
    payload = {**row.payload, "userId": user["id"], "status": "active"}
    changed = db.execute(
        update(MemberRow)
        .where(MemberRow.id == row.id, MemberRow.token_hash == digest)
        .values(user_id=user["id"], token_hash=None, expires_at=None, payload=payload)
    )
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "이미 처리된 초대입니다")
    db.commit()
    return payload


@router.patch("/organizations/{cid}/members/{mid}")
def change_role(cid: str, mid: str, body: RoleUpdate, db: DB, user: User):
    membership(db, user["id"], cid, admin=True)
    row = db.get(MemberRow, mid)
    if not row or row.company_id != cid:
        raise HTTPException(404, "멤버가 없습니다")
    if row.payload.get("owner") or row.user_id == user["id"]:
        raise HTTPException(409, "소유자 또는 자기 역할은 변경할 수 없습니다")
    row.role, row.payload = body.role, {**row.payload, "role": body.role}
    db.commit()
    return row.payload


@router.delete("/organizations/{cid}/members/{mid}")
def remove_member(cid: str, mid: str, db: DB, user: User):
    membership(db, user["id"], cid, admin=True)
    row = db.get(MemberRow, mid)
    if not row or row.company_id != cid:
        raise HTTPException(404, "멤버가 없습니다")
    if row.payload.get("owner") or row.user_id == user["id"]:
        raise HTTPException(409, "소유자 또는 자기 자신은 제거할 수 없습니다")
    db.delete(row)
    db.commit()
    return {"removed": True}
