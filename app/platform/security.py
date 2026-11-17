import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.platform.db import CompanyRow, database


@dataclass(frozen=True)
class Actor:
    user_id: str
    company_id: str
    role: str
    is_demo: bool = False


def actor(
    db: Annotated[Session, Depends(database)],
    authorization: str = Header(default=""),
    x_user_id: str = Header(default=""),
    x_company_id: str = Header(default=""),
    x_member_role: str = Header(default=""),
    x_demo_actor: str = Header(default="false"),
):
    """Identity is forwarded ONLY by the authenticated server-side BFF, never the browser."""
    key = settings.platform_service_key
    if not key or not hmac.compare_digest(authorization, f"Bearer {key}"):
        raise HTTPException(401, "인증된 프런트 서버 연결이 필요합니다")
    if not x_user_id or not x_company_id:
        raise HTTPException(403, "사용자·회사·역할 정보가 필요합니다")
    if settings.platform_demo and x_demo_actor == "true":
        company = db.get(CompanyRow, x_company_id)
        if (
            company
            and company.payload.get("isDemo")
            and x_member_role in {"member", "approver", "admin"}
        ):
            return Actor(x_user_id, x_company_id, x_member_role, is_demo=True)
    from app.platform.identity import membership

    member = membership(db, x_user_id, x_company_id)
    return Actor(
        x_user_id,
        x_company_id,
        {"관리자": "admin", "승인권자": "approver", "담당자": "member"}[member.role],
    )


def require_approver(who: Actor):
    if who.role not in {"approver", "admin"}:
        raise HTTPException(403, "승인권자 이상만 승인할 수 있습니다")
