"""가입하지 않은 처리 업체 명부(공공데이터)에서 폐기물을 받을 수 있는 업체 후보를 찾는다.

- 명부는 scripts/v3/public_vendors/build.py가 공공데이터포털 "전국폐기물처리업소표준데이터"로 만든다(재활용업만).
- 후보는 허가받은 처리 폐기물 분류번호(폐기물관리법 시행규칙 별표 4)가 그 폐기물의 분류에 맞고, 배출 기업에서
  직선거리로 가까운 순이다. 가입 업체처럼 AI가 대화하지 않으며, 단가·매입 기준을 모르므로 손익 계산과 배정에
  넣지 않는다. 담당자가 직접 견적을 요청할 수 있도록 연락처와 허가 정보만 보인다.
- 후보에는 「2024 전국 폐기물 발생 및 처리현황」 결과표의 2024년 처리량·처리능력을 붙인다(treatment2024,
  상호·시군구로 확실히 맞을 때만. 규칙은 treatment.py). 표시용이며 순위·손익 계산·배정에는 쓰지 않는다.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.v3.judge.common import haversine_km
from app.v3.public_vendors import treatment

_DIR = Path(__file__).resolve().parent
REGISTRY_FILE = _DIR / "registry.v1.json"
CODES_FILE = _DIR / "waste_codes.v1.json"
RADIUS_KM = 60.0
LIMIT = 5
CHECK_LIMIT = 3

# 폐기물 분류번호. 51-03-00은 "폐합성고분자화합물 전체"로 올린 곳이다.
PLASTIC_ALL = "51-03-00"
PLASTIC = "51-03-01"  # 폐합성수지류(폐염화비닐수지류는 제외한다)
PVC = "51-03-03"  # 폐폴리염화비닐수지류
FOAM = "51-03-06"  # 폐발포합성수지
PACKAGING = "51-03-07"  # 플라스틱폐포장재
# 처리폐기물정보를 믿기 어려운 업체(codesReliable=false)는 상호에 플라스틱을 다루는 말이 있을 때만 "확인 필요" 후보로 둔다
PLASTIC_NAME = re.compile(
    r"수지|플라|폴리|리사이클|재생|합성|비닐|케미|화학|PP|PE|PET", re.IGNORECASE
)
_FOAM_WORDS = ("발포", "EPS", "스티로폼")
_PACKAGING_WORDS = ("포장", "필름", "비닐", "랩")
# 플라스틱이 아닌 재질 낱말과 품목. 이 명부 검색(폐합성고분자화합물 분류번호)으로 찾지 않는다
_NON_PLASTIC_WORDS = (
    "알루미늄",
    "구리",
    "스테인리스",
    "황동",
    "금속",
    "고철",
    "비철",
    "철",
    "목재",
    "종이",
    "유리",
)
_NON_PLASTIC_ITEMS = ("금속 스크랩", "폐목재")
UNKNOWN_REASON = "재질이 확인되지 않아 찾지 않았습니다."
NON_PLASTIC_REASON = (
    "플라스틱이 아니어서 찾지 않았습니다. 이 명부 검색은 폐합성수지를 처리하는 재활용업만 다룹니다."
)


@lru_cache(maxsize=1)
def registry() -> dict[str, Any]:
    return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def code_names() -> dict[str, str]:
    return json.loads(CODES_FILE.read_text(encoding="utf-8"))["codes"]


def code_name(code: str) -> str | None:
    """분류번호의 이름. 이 판에 없는 번호는 상위 분류(앞 다섯 자리) 이름을 쓴다."""
    names = code_names()
    if code in names:
        return names[code]
    if code[:5] in names:
        return names[code[:5]]
    return None


def non_plastic(stream: dict) -> bool:
    """재질이나 품목이 플라스틱이 아니다(알루미늄, 금속 스크랩, 폐목재 등)."""
    material = str(stream.get("material") or "")
    return str(stream.get("wasteItem") or "") in _NON_PLASTIC_ITEMS or any(
        word in material for word in _NON_PLASTIC_WORDS
    )


def stream_codes(stream: dict) -> list[str]:
    """흐름이 해당하는 분류번호(우선순위 순). 플라스틱이 아니거나 재질을 모르면 빈 목록이다."""
    material = str(stream.get("material") or "").upper()
    text = " ".join(str(stream.get(k) or "") for k in ("form", "name", "item", "detail"))
    if not material or non_plastic(stream):
        return []
    if "PVC" in material:
        return [PVC, PLASTIC_ALL]
    if material in ("EPS",) or any(w in text for w in _FOAM_WORDS):
        return [FOAM, PLASTIC_ALL]
    codes = [PLASTIC, PLASTIC_ALL]
    if stream.get("origin") == "PCR" and any(w in text for w in _PACKAGING_WORDS):
        codes.insert(1, PACKAGING)
    return codes


def _norm(name: str) -> str:
    return re.sub(r"\(주\)|（주）|주식회사|유한회사|[\s()（）㈜]", "", name or "")


def _vendor_match(vendor: dict, codes: list[str]) -> list[str] | None:
    """업체가 이 분류번호의 후보인지. 맞는 번호 목록(빈 목록이면 상호로 고른 "확인 필요" 후보), 아니면 None."""
    reliable = vendor.get("codesReliable", True)
    matched = [c for c in codes if c in vendor["codes"]] if reliable else []
    if not matched and (reliable or not PLASTIC_NAME.search(vendor["name"])):
        return None
    return matched


def search(
    stream: dict,
    origin: tuple[float, float] | None,
    *,
    exclude_names: tuple[str, ...] = (),
    radius_km: float = RADIUS_KM,
    limit: int = LIMIT,
) -> dict[str, Any]:
    """흐름 하나의 미가입 업체 후보. origin은 배출 기업 좌표 [경도, 위도](조직의 location과 같은 순서)다.

    배출 기업 좌표가 없거나 분류번호를 정할 수 없으면 후보 없이 이유를 적는다.
    """
    data = registry()
    meta = {
        "source": data["source"],
        "url": data["url"],
        "asOfRange": data.get("asOfRange"),
        "radiusKm": radius_km,
    }
    codes = stream_codes(stream)
    if not codes:
        return {
            **meta,
            "codes": [],
            "candidates": [],
            "reason": NON_PLASTIC_REASON if non_plastic(stream) else UNKNOWN_REASON,
        }
    if origin is None:
        return {
            **meta,
            "codes": codes,
            "candidates": [],
            "reason": "회사 위치가 없어 찾지 않았습니다.",
        }
    taken = {_norm(n) for n in exclude_names if n}
    found = []
    for vendor in data["vendors"]:
        if _norm(vendor["name"]) in taken:
            continue
        matched = _vendor_match(vendor, codes)
        if matched is None:
            continue
        km = haversine_km(origin, (vendor["lon"], vendor["lat"]))
        if km > radius_km:
            continue
        # 허가 정보로 맞는 곳(confirmed)을 먼저, 그 안에서는 가까운 순
        tier = 0 if matched else 1
        found.append(
            (tier, km, codes.index(matched[0]) if matched else len(codes), vendor, matched)
        )
    found.sort(key=lambda item: item[:3])
    # 같은 상호가 업종만 달리 두 줄로 올라온 경우(예: 중간재활용업과 종합재활용업) 하나만 남긴다
    seen: set[str] = set()
    found = [
        item
        for item in found
        if not (_norm(item[3]["name"]) in seen or seen.add(_norm(item[3]["name"])))
    ]
    confirmed = [item for item in found if item[0] == 0][:limit]
    # 확인 필요 후보는 허가 정보로 맞는 곳보다 가까울 때만 몇 곳 덧붙인다(가까운 곳이 원본 품질 문제로 빠지지 않게)
    farthest = confirmed[-1][1] if confirmed else radius_km
    check = [item for item in found if item[0] == 1 and item[1] < farthest][:CHECK_LIMIT]
    candidates = [
        {
            "id": vendor["id"],
            "name": vendor["name"],
            "address": vendor["address"],
            "business": vendor["business"],
            "distanceKm": round(km, 1),
            # confirmed: 허가받은 처리 폐기물 번호가 맞음 / check: 원본의 번호를 믿기 어려워 상호로만 고름(처리 품목 확인 필요)
            "match": "confirmed" if matched else "check",
            "matchedCodes": [{"code": c, "name": code_name(c)} for c in matched],
            "codes": [{"code": c, "name": code_name(c)} for c in vendor["codes"]]
            if vendor.get("codesReliable", True)
            else [],
            "permitDate": vendor.get("permitDate"),
            "phone": vendor.get("phone"),
            "agency": vendor.get("agency"),
            "asOf": vendor.get("asOf"),
            # 2024년 처리 실적(결과표 한 줄). 확실히 맞는 줄이 없으면 None
            "treatment2024": treatment.summary(vendor),
        }
        for _, km, _, vendor, matched in [*confirmed, *check]
    ]
    return {
        **meta,
        "codes": [{"code": c, "name": code_name(c)} for c in codes],
        "candidates": candidates,
        "total": len(found),
        "confirmedTotal": sum(1 for item in found if item[0] == 0),
    }


def recheck(
    stream: dict,
    vendor_id: str,
    origin: tuple[float, float] | None,
    *,
    exclude_names: tuple[str, ...] = (),
    radius_km: float = RADIUS_KM,
) -> dict[str, Any]:
    """후보 하나가 지금 흐름 자료로도 후보인지(견적 요청서를 만들기 전에 다시 본다).

    search와 같은 조건(분류번호, 상호, 가입 여부, 반경)으로 본다. 순위(상위 몇 곳)는 보지 않는다.
    {"ok": bool, "reason": None | "not_listed" | "non_plastic" | "unknown_material" | "codes" | "registered"
    | "no_origin" | "too_far", "match": "confirmed" | "check" | None, "matchedCodes": [...]}
    """
    vendor = get(vendor_id)

    def fail(reason: str) -> dict[str, Any]:
        return {"ok": False, "reason": reason, "match": None, "matchedCodes": []}

    if vendor is None:
        return fail("not_listed")
    codes = stream_codes(stream)
    if not codes:
        return fail("non_plastic" if non_plastic(stream) else "unknown_material")
    matched = _vendor_match(vendor, codes)
    if matched is None:
        return fail("codes")
    if _norm(vendor["name"]) in {_norm(n) for n in exclude_names if n}:
        return fail("registered")
    if origin is None:
        return fail("no_origin")
    if haversine_km(origin, (vendor["lon"], vendor["lat"])) > radius_km:
        return fail("too_far")
    return {
        "ok": True,
        "reason": None,
        "match": "confirmed" if matched else "check",
        "matchedCodes": [{"code": c, "name": code_name(c)} for c in matched],
    }


def get(vendor_id: str) -> dict[str, Any] | None:
    for vendor in registry()["vendors"]:
        if vendor["id"] == vendor_id:
            return vendor
    return None
