"""현행 처리비를 차량·횟수 단위나 월 정액으로 받아 kg당 처리비로 바꾼다.

2026-11-19 배출 사업장 설문에서 "처리비를 차량·횟수 단위로 계약해서 kg 단가는 모른다"는 답이 나왔다. kg당 처리비가
없으면 현행 대비 월 손익을 견줄 기준이 혼합 소각 가정값으로 바뀌므로, 담당자가 아는 꼴(회당 금액 × 월 횟수, 월 정액,
연 금액)로 받는다.

- kg당 처리비 = 월 처리비 ÷ 월 발생량. 월 발생량이 없으면 바꾸지 못한다(ValueError, 담당자에게 보일 문장).
- 담당자가 적은 값은 human 근거(원/월), 환산은 calc 근거(원/kg. 입력은 그 human 근거와 월 발생량 근거)다.
  둘 다 배출 기업만 본다(현행 처리비는 업체에 열지 않는다).
- 글 답("회당 30만원, 한 달에 2번", "월 60만원", "1년에 720만원")은 parse_billing이 읽는다. "kg당 250원",
  "톤당 25만원"처럼 무게 단가로 적은 답은 여기서 읽지 않는다(amounts.read_amount가 읽는다).

지금 재활용 업체에 팔고 돈을 받는 경우(2026-11-19 설문: "지금 처리: 재활용 업체에 판다")는 현행 처리를
{"sale": true, "salePriceKrwPerKg": 받는 kg당 금액}으로 적는다. 계산은 signed_cost로 처리비의 반대 부호(−판매 단가)를
쓰므로 현행 월 손익이 +판매 단가 × 월 발생량이 되고, 배정의 "지금 방식 그대로"도 그 값을 넘는 제안만 고른다.
"""

from __future__ import annotations

import re
from typing import Any

from app.v3 import ids
from app.v3.agents.amounts import _ONE_PERIOD, _PER_KG, _PER_TON, _numbers, _text
from app.v3.evidence.model import Evidence

KINDS = ("per_trip", "monthly")
SALE_METHOD = "재활용 업체 판매"
# 배출 기업이 팔아서 돈을 받는다는 말. "업체가 250원 받는다"(처리비를 낸다)와 헷갈리지 않게 '받는다'만으로는 보지 않는다
_SALE = re.compile(r"판매|판다|팝니다|팔아|팔고|팔면|팔리|매각|돈을\s*받|받고\s*팔")
_TRIP = re.compile(
    r"회당|번당|회에|번에|한\s*번|차당|대당|한\s*차|차\s*한\s*대|트럭|차량|수거\s*한\s*번|1\s*회"
)
_MONTH = re.compile(r"월|달|매달|매월|month", re.IGNORECASE)
_YEAR = re.compile(r"년|연간|(?<![가-힣])연(?![가-힣])|year", re.IGNORECASE)
_COUNT_AFTER = re.compile(r"^\s*(?:회|번|차례|대|트럭)")
_MONEY_AFTER = re.compile(r"^\s*(?:원|krw|won)", re.IGNORECASE)
ASK_COUNT = "한 달에 몇 번 수거하는지도 함께 적어 주세요. 예: 회당 30만원, 월 2회"


def says_sale(text: Any) -> bool:
    """담당자 답이 "팔아서 돈을 받는다"는 뜻인지."""
    return isinstance(text, str) and bool(_SALE.search(text))


def is_sale(current: dict[str, Any] | None) -> bool:
    return bool((current or {}).get("sale"))


def sale_method(method: Any) -> str:
    """판매로 적은 현행 처리의 방법 이름. 비었거나 소각·매립처럼 돈을 내는 처리 이름이면 SALE_METHOD로 둔다."""
    text = str(method or "").strip()
    if not text or any(word in text for word in ("소각", "매립")):
        return SALE_METHOD
    return text


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def signed_cost(current: dict[str, Any] | None) -> float | None:
    """현행 kg당 처리비. 내는 돈은 +, 팔아서 받는 돈은 −판매 단가. 적은 값이 없으면 None."""
    current = current or {}
    if is_sale(current):
        price = current.get("salePriceKrwPerKg")
        return -float(price) if _number(price) else None
    cost = current.get("unitCostKrwPerKg")
    return float(cost) if _number(cost) else None


def _won(value: float) -> str:
    return f"{value:,.0f}원"


def describe(billing: dict[str, Any]) -> str:
    """사람이 읽는 계약 꼴. 예: "회당 300,000원 × 월 2회(월 600,000원)", "월 정액 600,000원"."""
    if billing["kind"] == "per_trip":
        trips = billing["tripsPerMonth"]
        trips_text = f"{trips:g}회"
        return (
            f"회당 {_won(billing['krwPerTrip'])} × 월 {trips_text}(월 {_won(monthly_krw(billing))})"
        )
    return f"월 정액 {_won(billing['krwPerMonth'])}"


def monthly_krw(billing: dict[str, Any]) -> float:
    if billing["kind"] == "per_trip":
        return float(billing["krwPerTrip"]) * float(billing["tripsPerMonth"])
    return float(billing["krwPerMonth"])


def check(billing: Any) -> dict[str, Any]:
    """화면이 보낸 계약 꼴을 검사해 정리한다. 틀리면 ValueError(담당자에게 보일 문장)."""
    if not isinstance(billing, dict) or billing.get("kind") not in KINDS:
        raise ValueError("계약 방식은 회당(per_trip)이나 월 정액(monthly)이어야 합니다.")

    def positive(name: str, label: str) -> float:
        value = billing.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{label}은 0보다 큰 숫자여야 합니다.")
        return float(value)

    if billing["kind"] == "per_trip":
        return {
            "kind": "per_trip",
            "krwPerTrip": positive("krwPerTrip", "회당 금액"),
            "tripsPerMonth": positive("tripsPerMonth", "한 달 수거 횟수"),
        }
    return {"kind": "monthly", "krwPerMonth": positive("krwPerMonth", "월 처리비")}


def parse_billing(value: Any) -> dict[str, Any] | None:
    """글 답에서 회당·월 정액·연 금액 계약을 읽는다. 무게 단가 답이거나 금액이 없으면 None.

    회당 금액은 있는데 한 달 횟수가 없으면 ValueError(ASK_COUNT)로 횟수를 함께 적어 달라고 한다.
    """
    if not isinstance(value, str):
        return None
    text = _ONE_PERIOD.sub(
        lambda m: m.group(1), _text(value)
    )  # "1년에", "한 달에"의 1·한은 세지 않는다
    if _PER_KG.search(text) or _PER_TON.search(text):
        return None
    money: list[float] = []
    counts: list[tuple[float, str]] = []
    for number, match in _numbers(text):
        after = text[match.end() :]
        before = text[max(0, match.start() - 8) : match.start()]
        if _MONEY_AFTER.match(after):
            money.append(number)
        elif _COUNT_AFTER.match(after):
            counts.append((number, before))
    if len(money) != 1:
        return None
    amount = money[0]
    monthly_counts = [n for n, before in counts if _MONTH.search(before)]
    yearly_counts = [
        n for n, before in counts if _YEAR.search(before) and not _MONTH.search(before)
    ]
    trip = bool(_TRIP.search(text))
    if trip or counts:
        if monthly_counts:
            trips = monthly_counts[0]
        elif yearly_counts:
            trips = yearly_counts[0] / 12
        elif len(counts) == 1 and counts[0][0] != 1 and _MONTH.search(text):
            trips = counts[0][0]
        else:
            raise ValueError(ASK_COUNT)
        if trips <= 0:
            raise ValueError(ASK_COUNT)
        return {"kind": "per_trip", "krwPerTrip": amount, "tripsPerMonth": trips}
    if _MONTH.search(text):
        return {"kind": "monthly", "krwPerMonth": amount}
    if _YEAR.search(text):
        return {"kind": "monthly", "krwPerMonth": amount / 12}
    return None


def to_treatment(
    registry: Any,
    stream: dict[str, Any],
    billing: dict[str, Any],
    today: str,
    *,
    method: str | None,
    source: str,
) -> dict[str, Any]:
    """계약 꼴을 kg당 처리비로 바꾼 현행 처리(currentTreatment). 월 발생량이 없으면 ValueError."""
    kg = stream.get("monthlyKg")
    if isinstance(kg, bool) or not isinstance(kg, (int, float)) or kg <= 0:
        raise ValueError(
            "월 발생량을 알아야 kg당 처리비로 바꿀 수 있습니다. 월 발생량을 먼저 적어 주세요."
        )
    billing = check(billing)
    org_id, sid = stream["orgId"], stream["id"]
    name = stream.get("name") or sid
    monthly = monthly_krw(billing)
    said = registry.put(
        Evidence(
            id=ids.human_id(org_id, f"{sid}-현행처리비-계약"),
            kind="human",
            label=f"{name} 현행 처리비({describe(billing)})",
            value=monthly,
            unit="원/월",
            excerpt=f"{source}: {name} 현행 처리비 {describe(billing)}",
            source=source,
            as_of=today,
            access=(org_id,),
            meta={"ownerOrgId": org_id, "streamId": sid, "billing": billing},
        )
    )
    kg_evidence = list(stream.get("monthlyKgEvidence") or [])
    per_kg = round(monthly / float(kg), 1)
    formula = f"월 처리비 {monthly:,.0f}원 ÷ 월 발생량 {float(kg):,.0f}kg"
    calc = registry.put(
        Evidence(
            id=ids.calc_id(
                "current_cost_per_kg", {"stream": sid, "billing": billing, "kg": kg}, "unitCost"
            ),
            kind="calc",
            label=f"{name} 현행 처리비 kg당 환산 계산",
            value=per_kg,
            unit="원/kg",
            excerpt=formula,
            source="현행 처리비 환산",
            as_of=today,
            access=(org_id,),
            meta={
                "tool": "current_cost_per_kg",
                "formula": formula,
                "inputEvidence": [said.id, *kg_evidence],
            },
        )
    )
    return {
        "method": method or "현행",
        "unitCostKrwPerKg": per_kg,
        "billing": billing,
        "evidence": [calc.id, said.id, *kg_evidence],
    }
