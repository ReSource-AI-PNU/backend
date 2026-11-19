"""업체 프로필의 노선·차량(fleet) 입력 검사. PUT /vendors/{orgId}의 fleet이 쓴다(계약 9장 "노선·차량").

판단 도구가 계산에 쓰는 값은 depot(차고지 [경도, 위도]), capacityKg(차량 한 대의 적재 중량),
capacityM3(적재 부피), costPerKm(km당 운행 단가)다. vehicleCount(차량 수)와 routes(노선: 이름, 운행 요일,
수거 지역·산업단지)는 업체가 밝히는 운영 정보로 저장하고 업체 화면에 보이지만 아직 계산에 쓰지 않는다
(vendor.public_profile이 판단 도구 입력에서 뺀다. 계산 근거 ID가 바뀌지 않게 하려는 것이다).

serviceAreas(수거 가능 지역: 시·군·구나 산업단지 이름)와 maxPickupKm(차고지에서 수거하러 가는 최대 거리)는
업체가 "어디까지 수거하러 갈 수 있는지" 밝히는 값이다(2026-11-19 멘토링: 업체에게 받을 정보에 수거 가능 지역을 기록).
판단 도구 입력에는 싣지 않고, 결과의 "수거 가능 지역" 확인 항목과 계약 검토(agents/reviewer.py)가 쓴다.

값이 틀리면 FleetError(ValueError. 문장은 화면에 그대로 보일 한국어)다. service.update_vendor가 422 INVALID_VALUE로 바꾼다.
"""

from __future__ import annotations

from typing import Any


class FleetError(ValueError):
    """노선·차량 입력이 틀렸다. 문장은 화면에 그대로 보인다."""


WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
NUMBERS = {
    # 키: (이름, 최소, 최소 포함 여부, 최대)
    "capacityKg": ("차량 적재 중량(kg)", 0.0, False, 100_000.0),
    "capacityM3": ("차량 적재 부피(m3)", 0.0, False, 1_000.0),
    "costPerKm": ("km당 운행 단가(원)", 0.0, True, 1_000_000.0),
    "maxPickupKm": ("최대 수거 거리(km)", 0.0, False, 1_000.0),
}
MAX_VEHICLES = 1_000
MAX_ROUTES = 30
MAX_PLACES = 30
ROUTE_KEYS = ("name", "days", "regions", "complexes")
KEYS = ("depot", *NUMBERS, "vehicleCount", "routes", "serviceAreas")
# 계산에 쓰지 않는 운영 정보(판단 도구 입력에서 뺀다). 수거 가능 지역은 확인 항목과 계약 검토만 쓴다
INFO_KEYS = ("vehicleCount", "routes", "serviceAreas", "maxPickupKm")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _depot(value: Any) -> list[float]:
    shown = "차고지(depot)는 [경도, 위도] 두 숫자여야 합니다."
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise FleetError(shown)
    lng, lat = _number(value[0]), _number(value[1])
    if lng is None or lat is None:
        raise FleetError(shown)
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise FleetError("차고지의 경도는 -180~180, 위도는 -90~90 사이여야 합니다.")
    return [lng, lat]


def _bounded(key: str, value: Any) -> int | float:
    """범위 안의 숫자면 받은 값 그대로(정수는 정수로) 준다."""
    label, low, inclusive, high = NUMBERS[key]
    number = _number(value)
    above = number is not None and (number >= low if inclusive else number > low)
    if number is None or not above or number > high:
        floor = f"{low:g} 이상" if inclusive else f"{low:g}보다 크고"
        raise FleetError(f"{label}은 {floor} {high:,.0f} 이하의 숫자여야 합니다.")
    return value


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_VEHICLES:
        raise FleetError(f"차량 수(vehicleCount)는 1 이상 {MAX_VEHICLES:,} 이하의 정수여야 합니다.")
    return value


def _texts(value: Any, label: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_PLACES:
        raise FleetError(f"{label}은 {MAX_PLACES}개 이하의 글 목록이어야 합니다.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > limit:
            raise FleetError(f"{label}의 항목은 {limit}자 이하의 글이어야 합니다.")
        if item.strip() not in out:
            out.append(item.strip())
    return out


def _days(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise FleetError("노선의 운행 요일(days)을 하나 이상 골라 주세요.")
    wrong = [d for d in value if d not in WEEKDAYS]
    if wrong:
        raise FleetError("운행 요일은 월, 화, 수, 목, 금, 토, 일 가운데에서 골라 주세요.")
    return [d for d in WEEKDAYS if d in value]  # 요일 순서로, 겹침 없이


def _route(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FleetError("노선(routes)의 항목은 객체여야 합니다.")
    unknown = sorted(set(value) - set(ROUTE_KEYS))
    if unknown:
        raise FleetError(f"노선에 쓸 수 없는 항목입니다: {', '.join(unknown)}.")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 50:
        raise FleetError("노선 이름(name)은 50자 이하로 적어 주세요.")
    regions = _texts(value.get("regions"), "수거 지역(regions)", 50)
    complexes = _texts(value.get("complexes"), "산업단지(complexes)", 100)
    if not regions and not complexes:
        raise FleetError(
            "노선마다 수거 지역(regions)이나 산업단지(complexes)를 하나 이상 적어 주세요."
        )
    return {
        "name": name.strip(),
        "days": _days(value.get("days")),
        "regions": regions,
        "complexes": complexes,
    }


def _routes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ROUTES:
        raise FleetError(f"노선(routes)은 {MAX_ROUTES}개 이하의 목록이어야 합니다.")
    routes = [_route(item) for item in value]
    names = [r["name"] for r in routes]
    if len(set(names)) != len(names):
        raise FleetError("노선 이름이 겹칩니다. 노선마다 다른 이름을 적어 주세요.")
    return routes


def clean_patch(value: Any) -> dict[str, Any]:
    """PUT /vendors의 fleet을 검사해 바꿀 값 {키: 값}을 준다. 값이 None이면 그 항목을 지운다.

    fleet이 null이면 바꾸지 않는다({}). evidence는 서버가 정하는 값이라 받아도 버린다(GET으로 받은
    fleet을 그대로 돌려보내도 된다). 그 밖에 모르는 항목은 오류다.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FleetError("노선·차량(fleet)은 객체로 보내 주세요.")
    unknown = sorted(set(value) - set(KEYS) - {"evidence"})
    if unknown:
        raise FleetError(
            f"노선·차량에 쓸 수 없는 항목입니다: {', '.join(unknown)}. 쓸 수 있는 항목: {', '.join(KEYS)}."
        )
    out: dict[str, Any] = {}
    for key in KEYS:
        if key not in value:
            continue
        item = value[key]
        if item is None:
            out[key] = None
        elif key == "depot":
            out[key] = _depot(item)
        elif key in NUMBERS:
            out[key] = _bounded(key, item)
        elif key == "vehicleCount":
            out[key] = _count(item)
        elif key == "serviceAreas":
            out[key] = _texts(item, "수거 가능 지역(serviceAreas)", 50)
        else:
            out[key] = _routes(item)
    return out
