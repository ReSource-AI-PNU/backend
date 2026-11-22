"""온보딩 도구. 업체 측 AI(대화)와 MCP 서버가 같은 도구를 쓴다.

도구는 초안(draft)만 고친다. 초안은 {키: {"value", "text", "source"}}이고 키는 FIELDS에 있는 것만 쓴다.
저장(확정) 도구는 없다. 담당자가 화면에서 고른 항목만 service.update_vendor로 업체 정보가 된다.

값의 출처는 둘이다.
- registry: 공공 허가 명부(전국폐기물처리업소표준데이터)의 그 업체 줄. 허가 품목(분류번호를 품목으로 옮김)과
  차고지 위치(명부의 좌표). 상호와 차고지가 명부와 맞으면 허가 확인 단계가 "등록 품목 일치"로 올라간다
  (judge/permits.registry_records).
- conversation: 담당자가 대화에서 한 말. 모델은 값과 함께 담당자의 말을 그대로 인용(quote)해야 하고,
  인용은 실제 담당자 메시지 안에 있어야 하며, 숫자 값은 인용 안의 숫자여야 한다. 맞지 않으면 초안에 넣지 않고
  이유를 모델에게 돌려준다(모델이 숫자를 지어내지 못하게 하는 장치).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from app.v3 import fleet as fleet_input
from app.v3 import public_vendors
from app.v3.a2a.guardrails import validate_policy
from app.v3.intake import criteria_llm

PROCESSES = ("재활용", "물질 재활용", "열분해", "소각", "매립")
WEEKDAYS = fleet_input.WEEKDAYS
CANDIDATES = 5

# 키 → (화면 이름, 구역, 대화로 받을 수 있는가)
FIELDS: dict[str, tuple[str, str, bool]] = {
    "permits": ("허가 품목", "permits", False),
    "criteria": ("매입 기준", "criteria", False),
    "fleet.depot": ("차고지 위치", "fleet", False),
    "fleet.serviceAreas": ("수거 가능 지역", "fleet", True),
    "fleet.maxPickupKm": ("최대 수거 거리", "fleet", True),
    "fleet.vehicleCount": ("차량 수", "fleet", True),
    "fleet.capacityKg": ("차량 한 대 적재 중량", "fleet", True),
    "fleet.costPerKm": ("km당 운행비", "fleet", True),
    "guardrails.days": ("수거 가능 요일", "guardrails", True),
    "guardrails.minPickupsPerMonth": ("한 달 최소 수거 횟수", "guardrails", True),
    "guardrails.floorPriceKrwPerKg": ("가장 낮게 부를 단가", "guardrails", True),
    "guardrails.ceilingPriceKrwPerKg": ("가장 높게 부를 단가", "guardrails", True),
    "guardrails.autoDecline": ("받지 않는 조건", "guardrails", True),
    "process": ("처리 방식", "process", True),
}
NUMBER_KEYS = {
    "fleet.maxPickupKm": "km",
    "fleet.vehicleCount": "대",
    "fleet.capacityKg": "kg",
    "fleet.costPerKm": "원/km",
    "guardrails.minPickupsPerMonth": "회",
    "guardrails.floorPriceKrwPerKg": "원/kg",
    "guardrails.ceilingPriceKrwPerKg": "원/kg",
}
LIST_KEYS = ("fleet.serviceAreas", "guardrails.days", "guardrails.autoDecline")

# 공공 명부의 처리 폐기물 분류번호 → 허가 품목(품목, 재질). 재질이 빈 목록이면 그 품목의 모든 재질이다
_PLASTIC = "폐플라스틱"
_CODE_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("51-03-00", _PLASTIC, ()),
    ("51-03-01", _PLASTIC, ()),
    ("51-03-03", _PLASTIC, ("PVC",)),
    ("51-03-06", _PLASTIC, ("EPS",)),
    ("51-03-07", "포장재", ()),
)
_PREFIX_ITEMS = (("51-29-", "금속 스크랩"), ("51-20-", "폐목재"))


# 단가의 부호(매입 +, 처리비 −)는 담당자의 말에 그 낱말이 있어야 정한다
_FEE_WORDS = ("처리비", "처리 비용", "처리단가", "처리 단가", "비용을 받", "돈을 받")
_BUY_WORDS = (
    "매입",
    "사들",
    "사 드",
    "사드",
    "사서",
    "값을 쳐",
    "쳐 드",
    "돈을 드",
    "드려요",
    "드립니다",
    "지급",
)
SIGN_UNCLEAR = "매입 단가인지 처리비인지 말씀에 없어 넣지 않았습니다."


class ToolError(ValueError):
    """도구 입력이 틀렸다. 문장은 모델에게 그대로 돌려준다."""


@dataclass
class Context:
    """도구 한 번의 호출 환경. draft는 도구가 고치고, 부른 쪽이 저장한다."""

    app: Any
    org_id: str
    draft: dict[str, dict[str, Any]]
    # 담당자가 이 대화에서 한 말. None이면 대화 밖(MCP)에서 부른 것이라 인용을 대조할 대화가 없다
    said: list[str] | None = None
    # 초안 출처에 적을 이름(MCP 키 이름 등). 없으면 기본 문구
    label: str | None = None


@dataclass(frozen=True)
class OnboardingTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[[Context, dict[str, Any]], dict[str, Any]]
    summary: Callable[[dict[str, Any], dict[str, Any]], str]


# ── 공공 명부 ──────────────────────────────────────────────────────────
def _norm(text: Any) -> str:
    return public_vendors._norm(str(text or "")).lower()


def _record_view(record: dict[str, Any], org_name: str) -> dict[str, Any]:
    codes = list(record.get("codes") or [])
    return {
        "registryId": record["id"],
        "name": record["name"],
        "business": record.get("business"),
        "address": record.get("address"),
        "agency": record.get("agency"),
        "permitDate": record.get("permitDate"),
        "asOf": record.get("asOf"),
        "codes": [f"{c} {public_vendors.code_name(c) or ''}".strip() for c in codes],
        "sameNameAsCompany": _norm(record["name"]) == _norm(org_name),
    }


def _org_name(ctx: Context) -> str:
    return str(ctx.app.store.get_org(ctx.org_id).get("name") or "")


def search_registry(query: str, region: str = "", org_name: str = "") -> list[dict[str, Any]]:
    """상호로 공공 명부를 찾는다. 같은 상호를 먼저, 그다음 상호에 검색어가 든 곳. region은 주소에 든 낱말."""
    key = _norm(query)
    if len(key) < 2:
        return []
    place = str(region or "").strip()
    found = []
    for record in public_vendors.registry()["vendors"]:
        name = _norm(record["name"])
        if key not in name and name not in key:
            continue
        if place and place not in str(record.get("address") or ""):
            continue
        found.append((0 if name == key else 1, len(name), record["id"], record))
    found.sort(key=lambda item: item[:3])
    return [_record_view(item[3], org_name or query) for item in found[:CANDIDATES]]


def _registry_record(registry_id: str) -> list[dict[str, Any]]:
    """그 줄과, 같은 상호·같은 주소로 업종만 달리 올라온 줄들."""
    rows = public_vendors.registry()["vendors"]
    first = next((r for r in rows if r["id"] == registry_id), None)
    if first is None:
        return []
    return [
        r
        for r in rows
        if r["id"] == first["id"]
        or (_norm(r["name"]) == _norm(first["name"]) and r.get("address") == first.get("address"))
    ]


def permits_from_codes(codes: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """(허가 품목 목록, 품목으로 옮기지 않은 분류번호). 같은 품목에 '모든 재질'이 있으면 재질별 줄은 뺀다."""
    items: dict[str, set[str] | None] = {}
    left = []
    for code in codes:
        hit = next(((item, mats) for c, item, mats in _CODE_ITEMS if c == code), None)
        if hit is None:
            hit = next(((item, ()) for p, item in _PREFIX_ITEMS if code.startswith(p)), None)
        if hit is None:
            left.append(code)
            continue
        item, mats = hit
        if not mats:
            items[item] = None
        elif items.get(item, set()) is not None:
            items[item] = {*(items.get(item) or set()), *mats}
    permits = [
        {"wasteItem": item, "materials": sorted(mats) if mats else []}
        for item, mats in sorted(items.items())
    ]
    return permits, left


def manual_permit_item(code: str, materials: list[str]) -> str | None:
    """목록 선택을 기존 분류에 연결하되 재질 범위는 담당자가 명시한 값으로 제한한다."""
    permits, unmapped = permits_from_codes([code])
    if unmapped or len(permits) != 1:
        return None
    item = permits[0]
    if item["materials"] and not set(materials) <= set(item["materials"]):
        return None
    if code == "51-03-01" and "PVC" in materials:
        return None
    return item["wasteItem"]


def permit_catalog() -> list[dict[str, str | None]]:
    return [
        {"code": code, "name": name, "wasteItem": manual_permit_item(code, [])}
        for code, name in public_vendors.code_names().items()
    ]


def _permits_text(permits: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{p['wasteItem']}({', '.join(p['materials'])})"
        if p["materials"]
        else f"{p['wasteItem']}(모든 재질)"
        for p in permits
    )


# ── 도구 ──────────────────────────────────────────────────────────────
def find_registry_vendor(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    org_name = _org_name(ctx)
    query = str(args.get("query") or org_name)
    candidates = search_registry(query, str(args.get("region") or ""), org_name)
    return {
        "query": query,
        "candidates": candidates,
        "note": (
            "공공 허가 명부는 지자체가 올린 것만 있어 명부에 없는 업체도 있습니다."
            if not candidates
            else "담당자가 자기 사업장이 맞다고 답한 줄만 use_registry_vendor로 초안에 넣습니다."
        ),
    }


def use_registry_vendor(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    registry_id = str(args.get("registryId") or "")
    records = _registry_record(registry_id)
    if not records:
        raise ToolError("그런 명부 ID가 없습니다. find_registry_vendor로 찾은 registryId를 쓰세요.")
    first = records[0]
    codes = sorted({c for r in records for c in r.get("codes") or []})
    business = " · ".join(
        dict.fromkeys(r.get("business") or "" for r in records if r.get("business"))
    )
    source = {
        "kind": "registry",
        "label": f"공공 허가 명부: {first['name']} ({business}, 기준일 {first.get('asOf') or '모름'})",
        "quote": None,
        "registryId": first["id"],
    }
    permits, left = permits_from_codes(codes)
    written = []
    if permits:
        ctx.draft["permits"] = {"value": permits, "text": _permits_text(permits), "source": source}
        written.append("permits")
    lon, lat = first.get("lon"), first.get("lat")
    if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
        ctx.draft["fleet.depot"] = {
            "value": [lon, lat],
            "text": f"{first.get('address') or '명부 주소'} (명부 좌표)",
            "source": source,
        }
        written.append("fleet.depot")
    same = _norm(first["name"]) == _norm(_org_name(ctx))
    return {
        "written": written,
        "permits": permits,
        "notMapped": [f"{c} {public_vendors.code_name(c) or ''}".strip() for c in left],
        "sameNameAsCompany": same,
        "note": (
            "상호와 차고지가 명부와 같으면 허가 확인이 '등록 품목 일치'로 올라갑니다. 허가증 원본 대조는 따로 필요합니다."
            if same
            else "회사 이름이 명부 상호와 달라 저장해도 명부 대조는 되지 않습니다. 허가 품목을 선택하거나 직접 입력하도록 안내하세요."
        ),
    }


def _digits(text: str) -> list[tuple[float, str]]:
    """인용 안의 숫자와 바로 뒤 단위 글자. '5톤' → (5, '톤'), '1,200원' → (1200, '원')."""
    out = []
    for match in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(만|천|톤|t|T|kg|㎏|km|원|대|회|번)?", text):
        try:
            number = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        out.append((number, match.group(2) or ""))
    # 우리말 수: "두 대", "세 번", "다섯 톤". 단위가 바로 뒤에 붙은 것만 읽는다("한 달"의 '한'은 수가 아니다)
    for match in _NATIVE.finditer(text):
        out.append((float(_NATIVE_NUMBERS[match.group(1)]), match.group(2)))
    return out


_NATIVE_NUMBERS = {
    "한": 1,
    "두": 2,
    "세": 3,
    "네": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}
_NATIVE = re.compile(r"(?<![가-힣])(다섯|여섯|일곱|여덟|아홉|한|두|세|네|열)\s*(대|번|회|톤)")


def _number_in(value: float, key: str, quote: str) -> bool:
    for number, unit in _digits(quote):
        options = {number}
        if unit == "만":
            options.add(number * 10_000)
        if unit == "천":
            options.add(number * 1_000)
        if key in ("fleet.capacityKg", "criteria.minMonthlyKg") and unit in ("톤", "t", "T"):
            options.add(number * 1_000)
        if any(abs(abs(value) - o) < 1e-6 for o in options):
            return True
    return False


def _squash(text: Any) -> str:
    return "".join(str(text or "").split())


def quoted(quote: str, said: str) -> bool:
    """인용이 담당자의 말에서 온 것인가.

    글자 그대로 들어 있거나, 모델이 조사 몇 개를 빼고 옮긴 정도(인용 글자의 90% 이상이 말과 순서대로 겹침)면
    맞다고 본다. 다만 인용 속 숫자는 하나도 빠짐없이 말에 있어야 한다(숫자를 바꿔 옮긴 인용은 거절).
    """
    q, s = _squash(quote), _squash(said)
    if not q:
        return False
    if q in s:
        return True
    said_numbers = {n for n, _ in _digits(said)}
    if any(n not in said_numbers for n, _ in _digits(quote)):
        return False
    matched = sum(
        block.size for block in SequenceMatcher(None, q, s, autojunk=False).get_matching_blocks()
    )
    return matched >= 0.9 * len(q)


def _days(value: Any, quote: str) -> list[str]:
    value = _as_list(value)
    if isinstance(value, list) and len(value) == 1 and len(value[0]) > 1 and "요일" not in value[0]:
        value = list(value[0])  # "월수금"
    if not isinstance(value, list) or not value:
        raise ToolError("수거 가능 요일은 ['월', '수'] 같은 목록이어야 합니다.")
    days = [str(d).strip()[:1] for d in value]
    if any(d not in WEEKDAYS for d in days):
        raise ToolError("요일은 월·화·수·목·금·토·일 가운데 한 글자로 적어 주세요.")
    said = _squash(quote)
    weekdays = any(w in said for w in ("평일", "주중", "월~금", "월-금", "월부터금"))
    for day in days:
        if day in said or (weekdays and day in WEEKDAYS[:5]):
            continue
        raise ToolError(f"인용한 말에 '{day}'요일이 없습니다. 담당자가 말한 요일만 넣으세요.")
    return list(dict.fromkeys(days))


def _as_list(value: Any) -> Any:
    """글 하나로 온 목록 값('금정구, 기장군')을 목록으로 바꾼다. 다른 모양은 그대로 둔다."""
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in re.split(r"[,、/·]", value) if part.strip()]
    return value


def _close(item: str, said: str) -> bool:
    """받지 않는 조건처럼 말을 줄여 옮긴 글: 인용과 겹치는 가장 긴 부분이 그 글의 3분의 2 이상이다."""
    a = _squash(item)
    if len(a) < 2:
        return False
    match = SequenceMatcher(None, a, said).find_longest_match(0, len(a), 0, len(said))
    return match.size >= max(2, len(a) * 2 / 3)


_PLACE_SUFFIX = re.compile(r"(특별자치시|특별자치도|광역시|특별시|전역|일대|지역|시|군|구|도)$")


def _place_in(item: str, said: str) -> bool:
    """지역 이름을 행정 단위만 붙여 옮긴 경우("김해" → "김해시", "부산 전역" → "부산광역시 전역")."""
    parts = [p for p in re.split(r"\s+", str(item).strip()) if p]
    cores = [_PLACE_SUFFIX.sub("", p) for p in parts]
    cores = [c for c in cores if len(c) >= 2]
    return bool(cores) and all(c in said for c in cores)


def _texts_in(key: str, value: Any, quote: str) -> list[str]:
    value = _as_list(value)
    if not isinstance(value, list) or not value:
        raise ToolError(f"{FIELDS[key][0]}은(는) 글 목록이어야 합니다.")
    items = [str(v).strip() for v in value if str(v).strip()]
    said = _squash(quote)
    for item in items:
        if (
            _squash(item) in said
            or (key in ("guardrails.autoDecline", "fleet.serviceAreas") and _close(item, said))
            or (key == "fleet.serviceAreas" and _place_in(item, said))
        ):
            continue
        raise ToolError(f"인용한 말에 '{item}'이(가) 없습니다. 담당자가 말한 낱말 그대로 넣으세요.")
    return items


def _check_value(key: str, value: Any, quote: str) -> tuple[Any, str]:
    """(검사한 값, 화면 글). 틀리면 ToolError."""
    label = FIELDS[key][0]
    if key in NUMBER_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"{label}은(는) 숫자여야 합니다.")
        if not _number_in(float(value), key, quote):
            raise ToolError(
                f"인용한 말에 {label} 값({value:g})이 없습니다. 담당자가 말한 숫자만 넣으세요."
            )
        if key == "fleet.vehicleCount" and float(value) != int(value):
            raise ToolError("차량 수는 정수여야 합니다.")
        shown = f"{value:,.0f}" if float(value) == int(value) else f"{value:,}"
        unit = NUMBER_KEYS[key]
        if key.startswith("guardrails.") and key.endswith("KrwPerKg"):
            fee = any(w in quote for w in _FEE_WORDS)
            buy = any(w in quote for w in _BUY_WORDS)
            if not fee and not buy:
                raise ToolError(SIGN_UNCLEAR)
            if fee and not buy and value > 0:
                raise ToolError(
                    "담당자가 처리비라고 했는데 매입 단가(양수)로 넣었습니다. 처리비는 음수로 넣으세요."
                )
            if buy and not fee and value < 0:
                raise ToolError("담당자가 매입이라고 했는데 처리비(음수)로 넣었습니다.")
            amount = f"{abs(value):,.0f}" if float(value) == int(value) else f"{abs(value):,}"
            return value, (
                f"kg당 {shown}원 (매입)" if value >= 0 else f"kg당 −{amount}원 (처리비 {amount}원)"
            )
        gap = "" if unit in ("대", "회") else " "
        return (int(value) if key == "fleet.vehicleCount" else value), f"{shown}{gap}{unit}"
    if key == "guardrails.days":
        days = _days(value, quote)
        return days, ", ".join(days)
    if key in LIST_KEYS:
        items = _texts_in(key, value, quote)
        return items, ", ".join(items)
    if key == "process":
        if value not in PROCESSES:
            raise ToolError(f"처리 방식은 {', '.join(PROCESSES)} 가운데 하나여야 합니다.")
        if _squash(value) not in _squash(quote) and not (value == "재활용" and "재활용" in quote):
            raise ToolError("인용한 말에 그 처리 방식이 없습니다.")
        return value, str(value)
    raise ToolError(f"{key}는 대화로 받는 항목이 아닙니다.")


def _validate_with_profile(ctx: Context, key: str, value: Any) -> None:
    """저장 경로와 같은 검사(fleet.clean_patch, 가드레일 범위)를 미리 돌린다."""
    section, _, name = key.partition(".")
    if section == "fleet":
        try:
            fleet_input.clean_patch({name: value})
        except ValueError as error:
            raise ToolError(str(error)) from None
    if section == "guardrails" and name in (
        "floorPriceKrwPerKg",
        "ceilingPriceKrwPerKg",
        "minPickupsPerMonth",
    ):
        try:
            saved = dict(ctx.app.store.get_vendor_profile(ctx.org_id).get("guardrails") or {})
        except Exception:  # noqa: BLE001 - 프로필이 없으면 빈 가드레일로 본다
            saved = {}
        drafted = {
            k.partition(".")[2]: v["value"]
            for k, v in ctx.draft.items()
            if k.startswith("guardrails.")
        }
        try:
            validate_policy({**saved, **drafted, name: value})
        except ValueError as error:
            raise ToolError(str(error)) from None


def propose_fields(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    fields = args.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ToolError("fields에 {key, value, quote} 목록을 넣으세요.")
    written, rejected = [], []
    for item in fields:
        if not isinstance(item, dict):
            rejected.append({"key": None, "reason": "항목은 {key, value, quote} 객체여야 합니다."})
            continue
        key, quote = str(item.get("key") or ""), str(item.get("quote") or "").strip()
        try:
            if key not in FIELDS or not FIELDS[key][2]:
                raise ToolError(
                    f"대화로 받을 수 있는 키가 아닙니다. 쓸 수 있는 키: {', '.join(k for k, f in FIELDS.items() if f[2])}"
                )
            if not quote:
                raise ToolError("담당자의 말을 quote에 그대로 인용하세요.")
            if ctx.said is not None and not any(quoted(quote, s) for s in ctx.said):
                raise ToolError(
                    "quote가 담당자가 이 대화에서 한 말에 없습니다. 담당자의 말을 그대로 옮기세요."
                )
            value, text = _check_value(key, item.get("value"), quote)
            _validate_with_profile(ctx, key, value)
        except ToolError as error:
            rejected.append({"key": key or None, "reason": str(error)})
            continue
        ctx.draft[key] = {
            "value": value,
            "text": text,
            "source": {
                "kind": "conversation" if ctx.said is not None else "agent",
                "label": ctx.label
                or ("담당자 답변" if ctx.said is not None else "연결한 에이전트가 제출"),
                "quote": quote,
            },
        }
        written.append(key)
    return {"written": written, "rejected": rejected}


# ── 매입 기준(대화) ───────────────────────────────────────────────────
# 재질 코드를 우리말로 말한 경우. 코드나 이 낱말이 담당자의 말에 있어야 그 재질로 넣는다
_MATERIAL_WORDS = {
    "PP": ("폴리프로필렌", "피피"),
    "PE": ("폴리에틸렌", "피이"),
    "HDPE": ("고밀도", "에이치디"),
    "LDPE": ("저밀도", "엘디"),
    "LLDPE": ("선형 저밀도", "선형저밀도"),
    "PET": ("페트",),
    "PS": ("폴리스티렌",),
    "ABS": ("에이비에스",),
    "PVC": ("염화비닐", "피브이씨"),
    "PC": ("폴리카보네이트",),
    "PA": ("나일론", "폴리아미드"),
}
_INCLUDED_WORDS = (
    "수거비 포함",
    "운반비 포함",
    "수거비는 포함",
    "운반비는 포함",
    "무료 수거",
    "무상 수거",
    "수거비 없",
)
_SEPARATE_WORDS = (
    "수거비 별도",
    "운반비 별도",
    "수거비는 별도",
    "운반비는 따로",
    "수거비는 따로",
    "수거비 따로",
)


def _material_in(material: str, quote: str) -> bool:
    upper = quote.upper()
    if re.search(rf"(?<![A-Z]){re.escape(material)}(?![A-Z])", upper):
        return True
    return any(word in quote for word in _MATERIAL_WORDS.get(material, ()))


def _price_in(value: float, quote: str) -> bool:
    """kg당 단가가 말에 있는가. "톤당 30만 원"처럼 톤으로 말했으면 1,000배 한 값이 말에 있어야 한다."""
    if _number_in(value, "criteria.price", quote):
        return True
    return "톤당" in quote and _number_in(value * 1_000, "criteria.price", quote)


def criterion_text(row: dict[str, Any]) -> str:
    head = " ".join(
        part
        for part in (
            row["material"],
            f"{row['grade']}등급" if row.get("grade") else None,
            row.get("form"),
        )
        if part
    )
    parts = [head]
    price = row.get("unitPriceKrwPerKg")
    if row.get("priceMode") == "negotiated":
        parts.append("단가 건별 협의")
    elif price is not None:
        amount = f"{abs(price):,.0f}" if float(price) == int(price) else f"{abs(price):,}"
        parts.append(f"kg당 {amount}원 " + ("매입" if price >= 0 else "처리비"))
    if row.get("minMonthlyKg") is not None:
        parts.append(f"한 달 최소 {row['minMonthlyKg']:,.0f} kg")
    if row.get("maxContaminationPct") is not None:
        parts.append(f"이물 {row['maxContaminationPct']:g}% 이하")
    if row.get("maxMoisturePct") is not None:
        parts.append(f"수분 {row['maxMoisturePct']:g}% 이하")
    if row.get("collectionFee"):
        parts.append("수거비 포함" if row["collectionFee"] == "included" else "수거비 별도")
    return " · ".join(parts)


def _number_arg(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{key}는 숫자여야 합니다.")
    return float(value)


def _criterion(row: dict[str, Any], quote: str) -> dict[str, Any]:
    """대화로 받은 매입 기준 한 줄을 검사한다. 값은 모두 담당자의 말(quote)에 있어야 한다."""
    material = str(row.get("material") or "").strip().upper()
    if material not in criteria_llm.MATERIAL_CODES:
        raise ToolError(f"재질은 {', '.join(criteria_llm.MATERIAL_CODES)} 가운데 하나여야 합니다.")
    if not _material_in(material, quote):
        raise ToolError(f"인용한 말에 재질 {material}이(가) 없습니다.")
    # 등급·형태는 고를 수 있는 칸이라, 말에 없으면 줄을 버리지 않고 그 칸만 비운다(지어낸 값은 넣지 않는다)
    grade = str(row.get("grade") or "").strip().upper().replace("등급", "") or None
    if grade is not None and (
        grade not in criteria_llm.GRADE_CODES
        or not re.search(rf"(?<![A-Z]){re.escape(grade)}(?![A-Z])", quote.upper())
    ):
        grade = None
    form = str(row.get("form") or "").strip() or None
    if form is not None and not (_squash(form) in _squash(quote) or _close(form, _squash(quote))):
        form = None
    negotiated = "협의" in quote
    price = _number_arg(row, "unitPriceKrwPerKg")
    if negotiated and price is None:
        mode = "negotiated"
    else:
        mode = "fixed"
        if price is None:
            raise ToolError(
                "kg당 단가가 없습니다. 단가를 묻거나, 건별 협의라고 하면 단가를 비우세요."
            )
        if not _price_in(price, quote):
            raise ToolError("인용한 말에 그 단가가 없습니다.")
        fee = any(w in quote for w in _FEE_WORDS)
        buy = any(w in quote for w in _BUY_WORDS)
        if not fee and not buy:
            raise ToolError(SIGN_UNCLEAR)
        if fee and not buy and price > 0:
            price = -price  # 처리비라고 말한 단가는 음수다
        if buy and not fee and price < 0:
            raise ToolError("담당자가 매입이라고 했는데 처리비(음수)로 넣었습니다.")
    out: dict[str, Any] = {
        "material": material,
        "grade": grade,
        "form": form,
        "priceMode": mode,
        "unitPriceKrwPerKg": price if mode == "fixed" else None,
    }
    for key, check in (
        ("minMonthlyKg", "criteria.minMonthlyKg"),
        ("maxContaminationPct", "criteria.pct"),
        ("maxMoisturePct", "criteria.pct"),
    ):
        value = _number_arg(row, key)
        if value is not None and not _number_in(value, check, quote):
            raise ToolError(f"인용한 말에 {key} 값({value:g})이 없습니다.")
        out[key] = value
    fee_kind = row.get("collectionFee") or None
    if fee_kind == "included" and not any(
        w in _squash(quote) for w in map(_squash, _INCLUDED_WORDS)
    ):
        fee_kind = None
    if fee_kind == "separate" and not any(
        w in _squash(quote) for w in map(_squash, _SEPARATE_WORDS)
    ):
        fee_kind = None
    out["collectionFee"] = fee_kind if fee_kind in ("included", "separate") else None
    try:
        criteria_llm.clean_rows([{**out, "manual": True}])
    except criteria_llm.RowError as error:
        raise ToolError(str(error).replace("1번째 줄: ", "")) from None
    return out


def propose_criteria(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ToolError("rows에 매입 기준 줄 목록을 넣으세요.")
    entry = ctx.draft.get("criteria") or {"value": [], "quotes": []}
    current = list(entry.get("value") or [])
    quotes = list(entry.get("quotes") or [])
    written, rejected = [], []
    for row in rows[:20]:
        if not isinstance(row, dict):
            rejected.append({"key": "criteria", "reason": "줄은 객체여야 합니다."})
            continue
        quote = str(row.get("quote") or "").strip()
        try:
            if not quote:
                raise ToolError("담당자의 말을 quote에 그대로 인용하세요.")
            if ctx.said is not None and not any(quoted(quote, s) for s in ctx.said):
                raise ToolError("quote가 담당자가 이 대화에서 한 말에 없습니다.")
            clean = _criterion(row, quote)
        except ToolError as error:
            material = str(row.get("material") or "").strip().upper() or None
            rejected.append({"key": "criteria", "reason": str(error), "material": material})
            continue
        same = (clean["material"], clean["grade"], _squash(clean["form"] or ""))
        kept = [
            (r, q)
            for r, q in zip(current, quotes + [""] * (len(current) - len(quotes)), strict=False)
            if (r["material"], r.get("grade"), _squash(r.get("form") or "")) != same
        ]
        current = [r for r, _ in kept] + [clean]
        quotes = [q for _, q in kept] + [quote]
        written.append(criterion_text(clean))
    if written:
        ctx.draft["criteria"] = {
            "value": current,
            "quotes": quotes,
            "text": "\n".join(criterion_text(r) for r in current),
            "source": {
                "kind": "conversation" if ctx.said is not None else "agent",
                "label": ctx.label
                or ("담당자 답변" if ctx.said is not None else "연결한 에이전트가 제출"),
                "quote": " / ".join(dict.fromkeys(quotes)),
            },
        }
    return {"written": ["criteria"] if written else [], "rows": written, "rejected": rejected}


def checks(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """업체 정보가 얼마나 찼는가. 허가 품목이나 매입 기준이 있어야 업체 탐색에 나온다."""
    fleet = profile.get("fleet") or {}
    guardrails = profile.get("guardrails") or {}
    docs = [d for d in profile.get("permitDocuments") or [] if d]
    return [
        {
            "key": "permits",
            "label": "허가 품목",
            "done": bool(profile.get("permits")),
            "note": "목록에서 선택하거나 직접 입력할 수 있습니다. 거래 확정에는 품목과 처리 방식의 원본 대조가 필요합니다."
            + (" 올린 허가증은 원본 대조를 기다립니다." if docs else ""),
        },
        {
            "key": "criteria",
            "label": "매입 기준표",
            "done": bool(profile.get("criteria")),
            "note": "대화로 말하거나 매입 기준을 직접 추가할 수 있습니다. 기준표 파일은 담당자가 확인한 줄만 저장합니다.",
        },
        {
            "key": "depot",
            "label": "차고지 위치",
            "done": bool(fleet.get("depot")),
            "note": "수거 거리와 운반비, 공공 명부 대조에 씁니다.",
        },
        {
            "key": "fleet",
            "label": "차량·수거 지역",
            "done": bool(
                fleet.get("capacityKg") or fleet.get("serviceAreas") or fleet.get("maxPickupKm")
            ),
            "note": "적재 중량과 수거 가능 지역으로 수거 횟수와 방문 가능 여부를 봅니다.",
        },
        {
            "key": "guardrails",
            "label": "AI가 지킬 범위",
            "done": bool(
                guardrails.get("days") or guardrails.get("floorPriceKrwPerKg") is not None
            ),
            "note": "수거 요일과 단가 하한 밖의 조건은 AI가 내놓지 않습니다.",
        },
    ]


def discoverable(profile: dict[str, Any]) -> bool:
    return bool(profile.get("permits") or profile.get("criteria")) and not profile.get("departed")


def profile_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    try:
        profile = ctx.app.store.get_vendor_profile(ctx.org_id)
    except Exception:  # noqa: BLE001 - 프로필이 없으면 빈 프로필로 본다
        profile = {}
    return {
        "company": _org_name(ctx),
        "checks": checks(profile),
        "discoverable": discoverable(profile),
        "draft": {key: entry["text"] for key, entry in ctx.draft.items()},
    }


def _found_summary(args: dict[str, Any], out: dict[str, Any]) -> str:
    n = len(out.get("candidates") or [])
    return f"공공 허가 명부에서 '{out.get('query')}' 찾기: " + (f"{n}곳" if n else "없음")


def _used_summary(args: dict[str, Any], out: dict[str, Any]) -> str:
    return "공공 허가 명부의 허가 품목과 위치를 초안에 넣음"


def _proposed_summary(args: dict[str, Any], out: dict[str, Any]) -> str:
    written = [FIELDS[k][0] for k in out.get("written") or []]
    rejected = len(out.get("rejected") or [])
    text = f"초안에 넣음: {', '.join(written)}" if written else "초안에 넣은 것 없음"
    return text + (f" · 말과 맞지 않아 뺀 것 {rejected}개" if rejected else "")


def _status_summary(args: dict[str, Any], out: dict[str, Any]) -> str:
    return "업체 정보 현황 확인"


_KEYS_FOR_MODEL = [k for k, f in FIELDS.items() if f[2]]
TOOLS: dict[str, OnboardingTool] = {
    tool.name: tool
    for tool in (
        OnboardingTool(
            "find_registry_vendor",
            "공공 허가 명부(전국폐기물처리업소표준데이터)에서 상호로 업체를 찾는다. query를 비우면 회사 이름으로 찾는다.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "상호(허가증에 적힌 이름)"},
                    "region": {"type": "string", "description": "주소에 든 낱말(예: 강서구). 선택"},
                },
                "additionalProperties": False,
            },
            find_registry_vendor,
            _found_summary,
        ),
        OnboardingTool(
            "use_registry_vendor",
            "담당자가 자기 사업장이 맞다고 확인한 명부 줄의 허가 품목과 위치를 초안에 넣는다.",
            {
                "type": "object",
                "properties": {"registryId": {"type": "string"}},
                "required": ["registryId"],
                "additionalProperties": False,
            },
            use_registry_vendor,
            _used_summary,
        ),
        OnboardingTool(
            "propose_fields",
            "담당자가 말한 값을 초안에 넣는다. 항목마다 담당자의 말을 quote에 그대로 인용한다. "
            "숫자는 인용 안의 숫자만 쓴다(톤은 kg로 바꿔도 된다). 처리비를 받는 단가는 음수다. "
            f"쓸 수 있는 key: {', '.join(_KEYS_FOR_MODEL)}",
            {
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string", "enum": _KEYS_FOR_MODEL},
                                "value": {
                                    "anyOf": [
                                        {"type": "number"},
                                        {"type": "string"},
                                        {"type": "array", "items": {"type": "string"}},
                                    ],
                                    "description": "숫자, 글, 또는 글 목록(요일·지역·조건)",
                                },
                                "quote": {"type": "string", "description": "담당자의 말 그대로"},
                            },
                            "required": ["key", "value", "quote"],
                        },
                    }
                },
                "required": ["fields"],
                "additionalProperties": False,
            },
            propose_fields,
            _proposed_summary,
        ),
        OnboardingTool(
            "propose_criteria",
            "담당자가 말한 매입 기준(재질, 등급, 형태, kg당 단가, 한 달 최소 물량, 이물·수분 상한, 수거비)을 "
            "초안에 넣는다. 줄마다 담당자의 말을 quote에 그대로 인용한다. 단가는 말에 매입이나 처리비가 있어야 "
            "넣고(처리비는 음수), 건별 협의면 단가를 비운다. 톤당 단가는 kg당으로 바꾼다. "
            f"재질 코드: {', '.join(criteria_llm.MATERIAL_CODES)}",
            {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "material": {
                                    "type": "string",
                                    "enum": list(criteria_llm.MATERIAL_CODES),
                                },
                                "grade": {"type": ["string", "null"]},
                                "form": {"type": ["string", "null"]},
                                "unitPriceKrwPerKg": {"type": ["number", "null"]},
                                "minMonthlyKg": {"type": ["number", "null"]},
                                "maxContaminationPct": {"type": ["number", "null"]},
                                "maxMoisturePct": {"type": ["number", "null"]},
                                "collectionFee": {
                                    "type": ["string", "null"],
                                    "enum": ["included", "separate", None],
                                },
                                "quote": {"type": "string"},
                            },
                            "required": ["material", "quote"],
                        },
                    }
                },
                "required": ["rows"],
                "additionalProperties": False,
            },
            propose_criteria,
            lambda args, out: (
                f"매입 기준 초안에 넣음: {len(out.get('rows') or [])}줄"
                + (
                    f" · 말과 맞지 않아 뺀 것 {len(out['rejected'])}줄"
                    if out.get("rejected")
                    else ""
                )
            ),
        ),
        OnboardingTool(
            "profile_status",
            "업체 정보에서 채운 것과 빈 것, 지금 초안을 본다.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            profile_status,
            _status_summary,
        ),
    )
}


def call_tool(ctx: Context, name: str, args: Any) -> tuple[dict[str, Any], str]:
    """(결과, 화면용 한 줄). 모르는 도구나 틀린 입력도 결과({"error"})로 돌려 모델이 고칠 수 있게 한다."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"그런 도구가 없습니다: {name}"}, "알 수 없는 도구 호출"
    payload = args if isinstance(args, dict) else {}
    try:
        out = tool.fn(ctx, payload)
    except ToolError as error:
        return {"error": str(error)}, f"{tool.name}: 입력 오류"
    return out, tool.summary(payload, out)


def llm_tools(names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """agents/llm.py가 받는 도구 목록({name, description, input_schema}). names를 주면 그 도구만."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS.values()
        if names is None or t.name in names
    ]
