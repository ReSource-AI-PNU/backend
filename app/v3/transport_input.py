"""업체 프로필의 운반 조건(transport) 입력 검사. PUT /vendors/{orgId}의 transport가 쓴다(계약 9장 "운반 조건").

    transport = {
      "basis": "included" | "separate" | "undecided",   # 노선 수거의 운반비: 단가에 포함 | 따로 받음 | 미정
      "paidBy": "vendor" | "emitter",                   # basis가 없으면 이것으로 정한다(vendor → 포함, emitter → 따로)
      "feePerPickupKrw" | "feePerMonthKrw": 숫자,        # 따로 받을 때 회당 또는 한 달 금액(하나만)
      "pickupsPerMonth": 숫자,                           # 회당 금액일 때 한 달 수거 횟수(모르면 비움)
      "offRoute": null | {                              # 수거 노선 밖 공장의 별도 수거 조건(judge/pickup.py가 읽는다)
        "status": "visit" | "unavailable",              # 따로 수거하러 감 | 수거 불가
        "basis", "paidBy", "feePerPickupKrw" | "feePerMonthKrw", "pickupsPerMonth"   # visit일 때만, 위와 같다
      },
      "evidence", "feeEvidence", "pickupsEvidence"      # 서버가 정한다(받아도 버린다)
    }

화면의 네 가지 답은 이렇게 저장된다: 수거 불가 = {status: unavailable}, 수거 가능·추가비 없음 = {status: visit,
basis: included}, 수거 가능·추가비 있음 = {status: visit, basis: separate, 금액, 횟수(모르면 비움)}, 비용 미정 =
{status: visit, basis: undecided}. 따로 받는 운반비는 공장(배출 기업)이 낸다.

값이 틀리면 TransportError(ValueError. 문장은 화면에 그대로 보일 한국어)다. service.update_vendor가 422로 바꾼다.
"""

from __future__ import annotations

import math
from typing import Any


class TransportError(ValueError):
    """운반 조건 입력이 틀렸다. 문장은 화면에 그대로 보인다."""


BASES = ("included", "separate", "undecided")
STATUSES = ("visit", "unavailable")
FEE_KEYS = ("feePerPickupKrw", "feePerMonthKrw")
BASE_KEYS = ("basis", "paidBy", *FEE_KEYS, "pickupsPerMonth")
SERVER_KEYS = ("evidence", "feeEvidence", "pickupsEvidence")
OFF_KEYS = ("status", *BASE_KEYS)
MAX_FEE = 10_000_000.0
MAX_PICKUPS = 100.0


def _number(value: Any, label: str, high: float) -> float | int:
    """0보다 크고 high 이하인 숫자면 받은 값 그대로(정수는 정수로) 준다."""
    ok = (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 < value <= high
    )
    if not ok:
        raise TransportError(f"{label}은 0보다 크고 {high:,.0f} 이하의 숫자여야 합니다.")
    return value


def _terms(value: dict[str, Any], where: str) -> dict[str, Any]:
    """운반비 조건 한 묶음(basis, paidBy, 금액, 횟수)을 고른 모양으로. where는 문장 앞머리다."""
    basis = value.get("basis")
    paid_by = value.get("paidBy")
    if paid_by is not None and paid_by not in ("vendor", "emitter"):
        raise TransportError(
            f"{where} 운반비를 누가 내는지(paidBy)는 vendor나 emitter로 적어 주세요."
        )
    if basis is None:
        basis = {"vendor": "included", "emitter": "separate"}.get(paid_by or "")
    if basis not in BASES:
        raise TransportError(
            f"{where} 운반비는 단가에 포함(included), 따로 받음(separate), 미정(undecided) 가운데 골라 주세요."
        )
    fees = {key: value[key] for key in FEE_KEYS if value.get(key) is not None}
    pickups = value.get("pickupsPerMonth")
    if basis != "separate":
        if fees or pickups is not None:
            raise TransportError(
                f"{where} 운반비가 단가에 포함이거나 미정이면 금액과 수거 횟수를 적지 않습니다."
            )
        return {"basis": basis, "paidBy": "vendor"} if basis == "included" else {"basis": basis}
    if paid_by == "vendor":
        raise TransportError(
            f"{where} 따로 받는 운반비는 공장이 냅니다. 업체가 내면 단가에 포함을 골라 주세요."
        )
    if len(fees) > 1:
        raise TransportError(
            f"{where} 따로 받는 운반비는 회당 금액(feePerPickupKrw)이나 한 달 금액(feePerMonthKrw) "
            "하나만 적어 주세요."
        )
    if not fees:
        # 따로 받는다는 것만 알고 금액은 모른다(매입 기준표에 "수거비 별도"만 적힌 경우). 금액 확인 필요로 둔다
        if pickups is not None:
            raise TransportError(f"{where} 수거 횟수는 회당 금액과 함께 적어 주세요.")
        return {"basis": "separate", "paidBy": "emitter"}
    key, amount = next(iter(fees.items()))
    label = "회당 운반비(원)" if key == "feePerPickupKrw" else "한 달 운반비(원)"
    out: dict[str, Any] = {
        "basis": "separate",
        "paidBy": "emitter",
        key: _number(amount, f"{where} {label}", MAX_FEE),
    }
    if pickups is not None:
        if key != "feePerPickupKrw":
            raise TransportError(f"{where} 한 달 금액이면 수거 횟수를 따로 적지 않습니다.")
        out["pickupsPerMonth"] = _number(pickups, f"{where} 한 달 수거 횟수", MAX_PICKUPS)
    return out


def _off_route(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TransportError("노선 밖 수거 조건(offRoute)은 객체로 보내 주세요.")
    unknown = sorted(set(value) - set(OFF_KEYS) - set(SERVER_KEYS))
    if unknown:
        raise TransportError(f"노선 밖 수거 조건에 쓸 수 없는 항목입니다: {', '.join(unknown)}.")
    status = value.get("status")
    if status not in STATUSES:
        raise TransportError(
            "노선 밖 공장을 따로 수거하러 가는지(status)는 visit(수거 가능)이나 unavailable(수거 불가)로 적어 주세요."
        )
    if status == "unavailable":
        if any(value.get(key) is not None for key in BASE_KEYS):
            raise TransportError("수거 불가면 운반비 조건을 적지 않습니다.")
        return {"status": "unavailable"}
    return {"status": "visit", **_terms(value, "노선 밖 수거의")}


def clean_patch(value: Any) -> dict[str, Any]:
    """PUT /vendors의 transport를 검사해 바꿀 내용을 준다.

    {"base": 노선 수거 조건} (최상위에 basis·paidBy·금액이 있을 때만), {"offRoute": 조건 | None(지움)}
    (offRoute 키가 있을 때만). transport가 null이면 바꾸지 않는다({}). 서버가 정하는 근거 키는 버린다.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TransportError("운반 조건(transport)은 객체로 보내 주세요.")
    unknown = sorted(set(value) - set(BASE_KEYS) - set(SERVER_KEYS) - {"offRoute"})
    if unknown:
        raise TransportError(f"운반 조건에 쓸 수 없는 항목입니다: {', '.join(unknown)}.")
    out: dict[str, Any] = {}
    if any(value.get(key) is not None for key in BASE_KEYS):
        out["base"] = _terms(value, "노선 수거의")
    if "offRoute" in value:
        out["offRoute"] = _off_route(value["offRoute"])
    return out


def view(section: Any) -> dict[str, Any] | None:
    """조건 한 묶음을 견주는 모양(근거 키 없이). 예전 프로필의 {"paidBy": "vendor"}도 basis를 채워 견준다."""
    if not isinstance(section, dict):
        return None
    shown = {k: section.get(k) for k in OFF_KEYS if section.get(k) is not None}
    if "basis" not in shown and shown.get("paidBy") in ("vendor", "emitter"):
        shown["basis"] = "included" if shown["paidBy"] == "vendor" else "separate"
    if shown.get("basis") == "separate":
        shown.setdefault("paidBy", "emitter")
    for key in (*FEE_KEYS, "pickupsPerMonth"):
        if key in shown:
            shown[key] = float(shown[key])
    return shown or None


def same(old: Any, new: Any) -> bool:
    return view(old) == view(new)


def _won(value: float) -> str:
    return f"{value:,.0f}원"


def describe(section: dict[str, Any], *, off_route: bool) -> str:
    """근거 원문에 남길 문장. 담당자가 고른 그대로를 적는다."""
    if off_route and section.get("status") == "unavailable":
        return "수거 노선 밖 공장은 수거하러 가지 않습니다(수거 불가)."
    head = (
        "수거 노선 밖 공장도 따로 수거하러 갑니다. 추가 운반비는"
        if off_route
        else "노선 수거의 운반비는"
    )
    basis = section.get("basis")
    if basis == "included":
        return (
            f"{head} 따로 받지 않습니다(매입 단가에 포함)."
            if off_route
            else f"{head} 매입 단가에 포함합니다."
        )
    if basis == "undecided":
        return f"{head} 아직 정하지 않았습니다."
    if section.get("feePerMonthKrw") is None and section.get("feePerPickupKrw") is None:
        return f"{head} 단가와 따로 받고 공장이 냅니다. 금액은 정하지 않았습니다."
    if section.get("feePerMonthKrw") is not None:
        return f"{head} 한 달 {_won(float(section['feePerMonthKrw']))}이고 공장이 냅니다."
    text = f"{head} 회당 {_won(float(section['feePerPickupKrw']))}이고 공장이 냅니다."
    pickups = section.get("pickupsPerMonth")
    if pickups is not None:
        return f"{text} 한 달 수거 횟수는 {float(pickups):g}회입니다."
    return f"{text} 한 달 수거 횟수는 정하지 않았습니다."
