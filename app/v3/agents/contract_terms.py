"""Shared document terms. Never infer a grade ladder or an unknown transport fee."""

from __future__ import annotations

from app.v3.judge.common import num

INSPECTION_HOLD = (
    "반입 검수에서 합의한 품질 조건을 벗어나면 반입을 보류하고 양쪽 담당자가 다시 협의합니다. "
    "등록·합의되지 않은 등급 단가, 위약금, 반품 비용을 자동 적용하지 않습니다."
)


def inspection_fallback(profile: dict, stream: dict) -> dict | None:
    """Only a vendor-selected, existing, material/form-matching fallback is usable."""
    selected = profile.get("inspectionFallbackCriterionId")
    if not selected:
        return None
    for row in profile.get("criteria") or []:
        if (
            row.get("id") == selected
            and row.get("material") == stream.get("material")
            and row.get("form") == stream.get("form")
            and row.get("grade")
            and num(row.get("unitPriceKrwPerKg")) is not None
            and row.get("evidence")
        ):
            return row
    return None


def transport_text(row: dict) -> str:
    pickup = row.get("pickup") or {}
    if row.get("transportPending") or pickup.get("pending"):
        return "운반비 미정 · 월 손익에 미확인 비용 미반영 · 확정 전 견적과 부담 주체 확인 필요"
    payer = row.get("transportPaidBy")
    if payer == "vendor":
        return "운반비 단가 포함 · 받는 곳 부담 · 보내는 곳 별도 운반비 없음"
    if payer == "emitter":
        return "운반비 별도 · 보내는 곳 부담 · 확인된 운반비를 월 손익에 반영"
    return "운반비 부담 주체와 금액 확인 필요 · 무료 운반으로 확정하지 않음"
