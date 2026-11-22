"""회사별 보정(개인화): 지난 검수에서 실제로 받은 단가가 합의 단가와 얼마나 달랐는지로 이번 예상 단가를 맞춘다.

관측 하나는 검수 한 건의 상대 차이 rel = (실제 단가 − 합의 단가) ÷ |합의 단가|다. 처리비(음수 단가)도 같은 식이라
값이 작을수록 배출 기업에 나쁘다. 등급이 낮게 나와 단가가 깎이면 이 차이에 그대로 들어간다.

계층형 축소 추정(사전 평균 0 = "계약대로 받는다")
- 폐기물 수준: m_s = Σ rel(이 회사·이 폐기물, 모든 업체) ÷ (n_s + k)
- 업체 수준:   m_sv = (Σ rel(이 업체) + k × m_s) ÷ (n_sv + k)
- 예상 단가:   계약 단가 + m_sv × |계약 단가|

기록이 적으면 계약 단가 쪽으로, 같은 업체 기록이 없으면 이 회사가 다른 업체에서 받은 기록 쪽으로 당긴다. k(보정 강도,
기록 몇 건 몫으로 계약 쪽을 믿는가)는 코드의 상수가 아니라 배출 기업 담당자가 "판정에 반영"을 승인한 기록(human 근거)의
값이다. 관측 하나의 rel은 [−1, 1]로 자른다(합의 단가가 아주 작을 때 한 건이 결과를 뒤집지 않게).

다른 배출 기업의 기록은 쓰지 않는다(사전 평균을 다른 회사의 평균으로 두지 않는 까닭이다).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

REL_LIMIT = 1.0

FORMULA_RATE = (
    "회사 기록 보정 비율 = (같은 업체 검수의 차이 합 + 보정 강도 × 이 폐기물 보정 비율) ÷ (같은 업체 검수 건수 + 보정 강도). "
    "이 폐기물 보정 비율 = 이 회사의 이 폐기물 검수 차이 합(모든 업체) ÷ (검수 건수 + 보정 강도). "
    "검수 차이 = (실제 단가 − 합의 단가) ÷ |합의 단가|"
)
FORMULA_PRICE = "계약 단가 + 회사 기록 보정 비율 × |계약 단가|"


def relative(actual: float, agreed: float) -> float | None:
    """검수 한 건의 상대 차이. 합의 단가가 0이면 견줄 수 없다."""
    if not math.isfinite(actual) or not math.isfinite(agreed) or abs(agreed) < 1e-9:
        return None
    return max(-REL_LIMIT, min(REL_LIMIT, (actual - agreed) / abs(agreed)))


def estimate(
    observations: Iterable[dict[str, Any]], strength: float, stream_id: str, vendor_id: str
) -> dict[str, Any] | None:
    """(stream_id, vendor_id)의 보정. 이 폐기물의 관측이 하나도 없으면 None(보정하지 않는다).

    observations: [{streamId, vendorOrgId, rel, evidence}]. 돌려주는 값: rate(비율), streamRate, records(같은 업체),
    streamRecords(이 폐기물 전체), evidence(쓴 관측의 근거 ID).
    """
    if not (strength > 0 and math.isfinite(strength)):
        raise ValueError("보정 강도는 0보다 큰 수여야 한다")
    mine = [o for o in observations if o.get("streamId") == stream_id and o.get("rel") is not None]
    if not mine:
        return None
    pair = [o for o in mine if o.get("vendorOrgId") == vendor_id]
    stream_rate = math.fsum(float(o["rel"]) for o in mine) / (len(mine) + strength)
    rate = (math.fsum(float(o["rel"]) for o in pair) + strength * stream_rate) / (
        len(pair) + strength
    )
    evidence = list(dict.fromkeys(e for o in mine for e in o.get("evidence") or []))
    return {
        "rate": rate,
        "streamRate": stream_rate,
        "records": len(pair),
        "streamRecords": len(mine),
        "evidence": evidence,
    }


def expected_price(price: float, rate: float) -> float:
    return price + rate * abs(price)


# ── 보정 강도 제안(운영 화면) ──────────────────────────────────────────
# 실제 검수가 이만큼 쌓여야 제안한다: 검수가 두 건 이상인 (배출 기업, 폐기물) 묶음 수와 전체 검수 수
SUGGEST_MIN_GROUPS = 5
SUGGEST_MIN_RECORDS = 15


def suggest_strength(groups: dict[Any, list[float]]) -> dict[str, Any]:
    """실제 검수 기록으로 보정 강도 k를 다시 정하는 제안(모멘트법 경험적 베이즈). 판정에 자동으로 쓰지 않는다.

    groups는 (배출 기업, 폐기물)마다 검수 상대 차이 목록이다. 묶음 안의 흔들림 σ²(검수 한 건이 얼마나 튀는가)과 묶음
    사이의 차이 τ²(회사·폐기물마다 실제로 얼마나 다른가)를 재서 k = σ² ÷ τ²를 낸다. 묶음 사이 차이가 작을수록 k가 커져
    계약 단가 쪽을 더 믿는다. 기록이 적거나 τ²가 0 이하이면 제안하지 않는다(그 까닭을 적는다).
    """
    records = sum(len(v) for v in groups.values())
    usable = {k: list(v) for k, v in groups.items() if len(v) >= 2}
    out: dict[str, Any] = {
        "records": records,
        "groups": len(usable),
        "minGroups": SUGGEST_MIN_GROUPS,
        "minRecords": SUGGEST_MIN_RECORDS,
        "withinVariance": None,
        "betweenVariance": None,
        "suggested": None,
    }
    if len(usable) < SUGGEST_MIN_GROUPS or records < SUGGEST_MIN_RECORDS:
        out["reason"] = (
            "실제 검수 기록이 아직 적어 제안하지 않습니다. 검수가 두 건 이상인 회사·폐기물 묶음 "
            f"{len(usable)}/{SUGGEST_MIN_GROUPS}개, 검수 {records}/{SUGGEST_MIN_RECORDS}건입니다."
        )
        return out
    means = {k: math.fsum(v) / len(v) for k, v in usable.items()}
    within = math.fsum((x - means[k]) ** 2 for k, v in usable.items() for x in v)
    within /= sum(len(v) - 1 for v in usable.values())
    grand = math.fsum(means.values()) / len(means)
    spread = math.fsum((m - grand) ** 2 for m in means.values()) / (len(means) - 1)
    between = spread - within * math.fsum(1 / len(v) for v in usable.values()) / len(usable)
    out["withinVariance"] = round(within, 6)
    out["betweenVariance"] = round(between, 6)
    if between <= 1e-12:
        out["reason"] = (
            "회사·폐기물 사이의 차이가 검수 한 건의 흔들림보다 작습니다. 회사별 보정이 거의 필요 없다는 뜻이라 "
            "보정 강도를 제안하지 않습니다(지금 값보다 크게 두면 계약 단가 쪽을 더 믿습니다)."
        )
        return out
    out["suggested"] = round(within / between, 2)
    out["reason"] = (
        "묶음 안 흔들림 ÷ 묶음 사이 차이로 낸 제안입니다. 판정에 자동으로 쓰지 않습니다. 바꾸려면 정책 승인을 거쳐 "
        "담당자 승인 화면의 제안값을 고칩니다."
    )
    return out
