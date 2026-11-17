"""Record an operator's manual permit review; this is not automatic government validation."""

import argparse

from sqlalchemy.orm import Session

from app.platform.db import CompanyRow, SourceRow, engine
from app.platform.models import CompanyProfile
from app.platform.service import now, uid


def verify(db, company_id, operator, evidence, confirm=False):
    if not confirm or not operator.strip() or not evidence.strip():
        raise ValueError("검토자·증빙·명시적 확인이 필요합니다")
    row = db.get(CompanyRow, company_id)
    if not row:
        raise ValueError("기업이 없습니다")
    profile = CompanyProfile.model_validate(row.payload)
    if not profile.collector or profile.is_demo:
        raise ValueError("실제 업체의 수거 조건을 먼저 등록하세요")
    policy = profile.collector
    if not policy.permits or not policy.materials or not policy.forms or not policy.routes:
        raise ValueError("허가 품목·재질·성상·처리 경로가 비었습니다")
    policy.evidence = evidence
    policy.permit_verified = True
    row.payload = profile.model_dump(mode="json", by_alias=True)
    db.add(
        SourceRow(
            id=uid("permit-review"),
            kind="permit-review",
            payload={
                "companyId": company_id,
                "operator": operator,
                "reviewedAt": now(),
                "evidence": evidence,
                "policy": policy.model_dump(mode="json", by_alias=True),
                "method": "manual-operator-review",
            },
        )
    )
    db.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("company_id")
    parser.add_argument("--operator", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    with Session(engine()) as db:
        verify(db, args.company_id, args.operator, args.evidence, args.confirm)
    print("운영자 수동 허가 검토 기록을 저장했습니다. 업체 조건 변경 시 다시 검토하세요.")
