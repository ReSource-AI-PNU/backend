"""⑥ 단가 협상: 합의 전에 회사 측이 한 번 더 나은 단가를 요청하고, 업체 측이 자기 범위 안에서 답한다(2026-11-19).

- 회사 측(ask_price): 받은 제안 단가에서 요청 폭(가정값 negotiation.askPct)만큼 나은 단가를 요청한다. "나은"은
  배출 기업 쪽으로 좋은 방향이다: 매입이면 더 받고, 처리비면 덜 낸다(단가 부호는 +매입, −처리비). 요청 단가는
  계산 근거로 남고 업체도 본다.
- 업체 측(reply_price): 요청 단가와 업체가 정한 단가 상한(가드레일 ceilingPriceKrwPerKg) 가운데 업체에 덜 불리한
  값을 내되, 처음 제안보다 나빠지지 않는다. 상한을 정하지 않은 업체는 처음 제안을 그대로 둔다. 상한 값 자체는
  계산 근거의 입력에 싣지 않는다(배출 기업에 업체 범위를 드러내지 않는다).
- 둘 다 결정적 계산이고 숫자는 원/kg이다. 모델은 이 값을 바꾸지 못한다.
"""

from __future__ import annotations

from typing import Any

from app.v3.judge.common import Recorder, num

ASK_KEY = "negotiation.askPct"


def _offer_view(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": claim.get("value"),
        "unit": claim.get("unit"),
        "evidence": list(claim.get("evidence") or [])[:1],
    }


def ask_price(registry, *, org_id, today, stream_id, offer, vendor_org):
    """회사 측 요청 단가. offer는 업체가 낸 단가 claim(원/kg)이다."""
    rec = Recorder(
        "negotiate_ask",
        registry,
        org_id,
        today,
        {"streamId": stream_id, "offer": _offer_view(offer)},
        [vendor_org],
    )
    price = num(offer.get("value"))
    pct = rec.basis(ASK_KEY)
    output = {"streamId": stream_id, "asked": None}
    if price is None or pct is None or not isinstance(pct.value, (int, float)):
        rec.check("ask", "conditional", "요청 폭이나 제안 단가를 몰라 단가를 요청하지 않았습니다.")
        return rec.result("needs_information", output)
    asked = round(price + abs(price) * float(pct.value) / 100)
    if asked == price:
        asked = price + 1  # 반올림으로 같아지면 한 단위만 더 요청한다
    claim = rec.number(
        "askedPrice",
        asked,
        "원/kg",
        "제안 단가 + |제안 단가| × 요청 폭(가정값). 매입이면 더 받고 처리비면 덜 내는 방향",
        [*(offer.get("evidence") or [])[:1], pct.id],
        label="요청 단가 계산",
    )
    output["asked"] = claim["id"]
    return rec.result("calculated", output)


def reply_price(registry, *, org_id, today, stream_id, asked, offered, ceiling, emitter_org):
    """업체 측 답 단가. asked는 회사 측 요청 claim, offered는 업체가 처음 낸 claim, ceiling은 가드레일 상한."""
    rec = Recorder(
        "negotiate_reply",
        registry,
        org_id,
        today,
        {"streamId": stream_id, "asked": _offer_view(asked), "offered": _offer_view(offered)},
        [emitter_org],
    )
    want, first, top = num(asked.get("value")), num(offered.get("value")), num(ceiling)
    output = {"streamId": stream_id, "price": None, "accepted": False, "improved": False}
    if want is None or first is None:
        rec.check("reply", "conditional", "요청 단가나 처음 제안을 몰라 답하지 않았습니다.")
        return rec.result("needs_information", output)
    best = first if top is None else max(first, min(want, top))
    claim = rec.number(
        "replyPrice",
        best,
        "원/kg",
        "회사 측 요청 단가를 업체가 정한 범위 안에서 받아들일 수 있는 만큼(처음 제안보다 나빠지지 않게)",
        [*(asked.get("evidence") or [])[:1], *(offered.get("evidence") or [])[:1]],
        label="협상 단가 계산",
    )
    output.update(price=claim["id"], accepted=best >= want, improved=best > first)
    return rec.result("calculated", output)
