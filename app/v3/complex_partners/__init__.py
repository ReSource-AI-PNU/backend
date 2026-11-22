"""같은 산업단지의 자원순환 후보. 배출 기업과 같은 단지에서 그 폐기물을 받아 재생원료로 만드는 곳(takes)과,
재생원료가 된 뒤 그 원료를 쓸 수 있는 곳(uses)을 공공데이터로 찾는다.

- 자료는 둘이다. 한국산업단지공단 등록공장 현황(factories.v1.json, 회사명·단지명·생산품·주소)과 공공 허가 명부
  (public_vendors, 재활용업 허가). 등록공장은 생산품 문장으로 역할을 가리고(classify), 같은 상호가 허가 명부에
  있으면 허가 정보를 붙인다. 허가 명부에만 있고 단지 범위 안에 있는 재활용업체도 takes에 넣는다.
- 다른 배출 기업의 물량은 쓰지 않는다(설계안 v3 비목표: 이웃 기업 물량 모으기). 회사가 신고한 생산품과 허가만
  보이고, 그 회사에 아무것도 보내지 않는다. 단가·매입 기준을 모르므로 손익 계산과 배정에 넣지 않는다.
- 폐기물을 넘기려면 받는 곳에 그 폐기물의 재활용 허가(또는 신고)가 있어야 한다. 허가 명부에 없는 곳은
  "재활용 허가 확인 필요"로 둔다. uses는 폐기물을 바로 넘기는 곳이 아니라 재생원료의 수요처다.
- 진행기가 ④ 업체 찾기에서 find를 부르고, 결과 한 장의 최상위 complexPartners에 둔다(업체 뷰에는 실리지 않는다).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.v3 import ids, public_vendors
from app.v3.complex_partners import classify
from app.v3.evidence.model import Evidence

TOOL = "find_complex_partners"
_DIR = Path(__file__).resolve().parent
COMPLEXES_FILE = _DIR / "complexes.v1.json"
FACTORIES_FILE = _DIR / "factories.v1.json"
STATS_FILE = _DIR / "stats.v1.json"
TAKES_LIMIT = 10
USES_LIMIT = 8
NO_COMPLEX_REASON = "회사가 산업단지에 등록되어 있지 않아 찾지 않았습니다. 회사 정보에서 산업단지를 고르면 같은 단지의 후보를 찾습니다."
# 받는 곳의 허가 상태. listed: 허가 명부에 있고 처리 품목도 맞음, listed_check: 허가 명부에 있으나 처리 품목은
# 원본을 믿기 어려워 확인 필요, unlisted: 허가 명부에 없음(재활용 허가·신고 확인 필요)
_PERMIT_RANK = {"listed": 0, "listed_check": 1, "unlisted": 2}


@lru_cache(maxsize=1)
def complexes() -> list[dict[str, Any]]:
    return json.loads(COMPLEXES_FILE.read_text(encoding="utf-8"))["complexes"]


@lru_cache(maxsize=1)
def factories() -> dict[str, Any]:
    return json.loads(FACTORIES_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def stats() -> dict[str, Any]:
    """산업단지별 폐합성수지 발생·처리 집계(2024 전국 폐기물 발생 및 처리현황, scripts/v3/complex_partners/stats.py)."""
    return json.loads(STATS_FILE.read_text(encoding="utf-8"))


# 단지 집계에서 화면에 보일 칸과 이름. 단지 전체 합계라 회사 계산에는 쓰지 않는다
STAT_FIELDS = (
    ("generated", "발생량"),
    ("recycled", "재활용"),
    ("incinerated", "소각"),
    ("landfilled", "매립"),
)


def stats_evidence(complex_id: str) -> Evidence | None:
    """단지 집계 한 줄을 원문 그대로 발췌한 ref 근거. 그 단지의 집계가 없으면 None."""
    data = stats()
    row = data["complexes"].get(complex_id)
    if row is None:
        return None
    unit = data["unit"]
    parts = [f"{row['sheetName']} · {data['waste']}"] + [
        f"{name} {row[key]:,.2f} {unit}" for key, name in STAT_FIELDS
    ]
    parts.append(
        f"재활용 중 물질재활용 {row['materialRecycled']:,.2f} · 에너지회수 {row['energyRecovered']:,.2f}"
    )
    return Evidence(
        id=ids.ref_id("폐기물통계", f"{complex_id}-{data['waste']}", data["dataYear"]),
        kind="ref",
        label=f"{row['sheetName']} 폐합성수지 발생·처리 ({data['dataYear']})",
        excerpt=" | ".join(parts),
        source=data["source"],
        as_of=data["publishedOn"],
        meta={
            "url": data["url"],
            "sheet": data["sheet"],
            "dataYear": data["dataYear"],
            "note": data["note"],
            "evidenceClass": "official",
            "verificationStatus": "original_checked",
            "verifiedOn": "2026-11-22",
        },
    )


def complex_stats(registry: Any, complex_id: str) -> dict[str, Any] | None:
    """결과 한 장의 complexPartners.stats. 단지 전체 합계를 근거가 열리는 값으로 보인다(표시만)."""
    evidence = stats_evidence(complex_id)
    if evidence is None:
        return None
    data = stats()
    row = data["complexes"][complex_id]
    stored = registry.put(evidence).id
    return {
        "sheetName": row["sheetName"],
        "waste": data["waste"],
        "dataYear": data["dataYear"],
        "values": {
            key: {
                "id": f"complexStat_{key}",
                "kind": "number",
                "value": row[key],
                "unit": data["unit"],
                "evidence": [stored],
                "label": f"단지 폐합성수지 {name}",
                "evidenceClass": "official",
            }
            for key, name in STAT_FIELDS
        },
        "note": data["note"],
        "source": {"name": data["source"], "url": data["url"], "publishedOn": data["publishedOn"]},
    }


def _inside(complex_: dict[str, Any], lon: float, lat: float) -> bool:
    x0, y0, x1, y1 = complex_["bbox"]
    return x0 <= lon <= x1 and y0 <= lat <= y1


def complex_of(org: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """회사가 든 산업단지와 판단 근거("region" 지역 글, "location" 위치). 모르면 None.

    지역 글에 단지 이름이 있으면 그것을 먼저 쓴다(회사 정보에서 산업단지를 고르면 "부산 녹산국가산업단지"로
    저장된다). 없으면 위치가 단지 범위 안인지 보고(겹치면 중심이 가까운 단지), 그다음 지역 글의 다른 이름
    ("녹산산단" 등)을 본다.
    """
    text = re.sub(r"\s+", "", " ".join(str(org.get(k) or "") for k in ("region", "address")))
    for complex_ in complexes():
        if complex_["name"].replace(" ", "") in text:
            return complex_, "region"
    location = org.get("location")
    if location:
        lon, lat = float(location[0]), float(location[1])
        inside = [c for c in complexes() if _inside(c, lon, lat)]
        if inside:
            nearest = min(
                inside, key=lambda c: (c["center"][0] - lon) ** 2 + (c["center"][1] - lat) ** 2
            )
            return nearest, "location"
    for complex_ in complexes():
        if any(alias.replace(" ", "") in text for alias in complex_["aliases"]):
            return complex_, "region"
    return None


def _province(address: str) -> str:
    return (address or "").split(" ", 1)[0][:2]


def _registry_by_name() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for vendor in public_vendors.registry()["vendors"]:
        out.setdefault(public_vendors._norm(vendor["name"]), []).append(vendor)
    return out


def _permit(
    vendors: list[dict[str, Any]], codes: list[str]
) -> tuple[str, dict[str, Any] | None, list[str]]:
    """같은 상호의 허가 명부 줄에서 (상태, 고른 줄, 맞는 분류번호)."""
    best: tuple[int, dict[str, Any] | None, list[str]] = (2, None, [])
    for vendor in vendors:
        reliable = vendor.get("codesReliable", True)
        matched = [c for c in codes if c in vendor["codes"]] if reliable else []
        rank = 0 if matched else 1
        if reliable and not matched:
            continue  # 허가 품목이 믿을 만한데 이 재질이 없다: 이 폐기물의 재활용 허가가 아니다
        if rank < best[0]:
            best = (rank, vendor, matched)
    status = {0: "listed", 1: "listed_check", 2: "unlisted"}[best[0]]
    return status, best[1], best[2]


def _resin_match(found: list[str], wanted: list[str]) -> str | None:
    """생산품에 적힌 수지(found)와 흐름 재질(wanted). named: 같은 수지가 적혀 있음, unstated: 수지가 적혀 있지
    않거나 흐름 재질을 모름(확인 필요), None: 다른 수지만 적혀 있음(후보 아님)."""
    if not found or not wanted:
        return "unstated"
    return "named" if set(found) & set(wanted) else None


def search(
    stream: dict[str, Any],
    emitter: dict[str, Any],
    *,
    exclude_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """흐름 하나의 같은 단지 후보(근거 없이 원자료로). 화면용 모양은 find가 만든다.

    {"complex", "codes", "takes": [...], "uses": [...], "reason"}. takes·uses는 정렬했고 자르지 않았다.
    """
    where = complex_of(emitter)
    if where is None:
        return {"complex": None, "codes": [], "takes": [], "uses": [], "reason": NO_COMPLEX_REASON}
    complex_, basis = where
    codes = public_vendors.stream_codes(stream)
    if not codes:
        reason = (
            public_vendors.NON_PLASTIC_REASON
            if public_vendors.non_plastic(stream)
            else public_vendors.UNKNOWN_REASON
        )
        return {
            "complex": (complex_, basis),
            "codes": [],
            "takes": [],
            "uses": [],
            "reason": reason,
        }
    wanted = classify.families(stream.get("material"))
    skip = {public_vendors._norm(n) for n in (*exclude_names, emitter.get("name") or "") if n}
    by_name = _registry_by_name()
    takes: list[dict[str, Any]] = []
    uses: list[dict[str, Any]] = []
    seen_vendors: set[str] = set()
    for factory in factories()["factories"]:
        key = public_vendors._norm(factory["name"])
        if factory["complexId"] != complex_["id"] or key in skip:
            continue
        roles = factory["roles"]
        role = "takes" if "reprocess" in roles else "uses"
        detail = roles["reprocess" if role == "takes" else "use"]
        match = _resin_match(detail["resins"], wanted)
        if match is None:
            continue
        film_elsewhere = wanted and "PE" not in wanted and classify.film_only(detail["items"])
        if role == "uses" and match == "unstated" and film_elsewhere:
            continue  # 수지를 적지 않은 비닐·필름은 대개 PE라 다른 재질의 수요처로 두지 않는다
        item = {"factory": factory, "vendor": None, "match": match, "items": detail["items"]}
        if role == "takes":
            same = [
                v
                for v in by_name.get(key, [])
                if _province(v["address"]) == _province(factory["address"])
            ]
            status, vendor, matched = _permit(same, codes)
            if vendor is not None:
                seen_vendors.add(vendor["id"])
            takes.append({**item, "permit": status, "vendor": vendor, "matchedCodes": matched})
        else:
            uses.append(item)
    # 허가 명부에만 있는 단지 안 재활용업체(상호나 허가 품목이 플라스틱과 맞는 곳)
    for vendor in public_vendors.registry()["vendors"]:
        if vendor["id"] in seen_vendors or public_vendors._norm(vendor["name"]) in skip:
            continue
        if not _inside(complex_, vendor["lon"], vendor["lat"]):
            continue
        matched = public_vendors._vendor_match(vendor, codes)
        if matched is None:
            continue
        seen_vendors.add(vendor["id"])
        takes.append(
            {
                "factory": None,
                "vendor": vendor,
                "match": "unstated",
                "items": [],
                "permit": "listed" if matched else "listed_check",
                "matchedCodes": matched,
            }
        )

    def name_of(item: dict[str, Any]) -> str:
        return (item["factory"] or item["vendor"])["name"]

    # 같은 상호가 여러 줄(공장 여럿, 허가 명부 두 줄)이면 하나만 남긴다
    def unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        names: set[str] = set()
        return [
            r
            for r in rows
            if not (
                public_vendors._norm(name_of(r)) in names
                or names.add(public_vendors._norm(name_of(r)))
            )
        ]

    takes.sort(key=lambda r: (_PERMIT_RANK[r["permit"]], r["match"] != "named", name_of(r)))
    uses.sort(key=lambda r: (r["match"] != "named", name_of(r)))
    return {
        "complex": (complex_, basis),
        "codes": codes,
        "takes": unique(takes),
        "uses": unique(uses),
        "reason": None,
    }


def _factory_ref_id(factory: dict[str, Any]) -> str:
    return ids.ref_id("등록공장현황", factory["id"], factories()["asOf"])


def factory_evidence(factory: dict[str, Any]) -> Evidence:
    """등록공장 현황 한 줄. 조문처럼 원문을 그대로 발췌한다."""
    data = factories()
    return Evidence(
        id=_factory_ref_id(factory),
        kind="ref",
        label=f"{factory['name']} 등록공장 정보",
        excerpt=" · ".join(
            [
                factory["name"],
                factory["complexName"],
                f"생산품: {factory['product']}",
                factory["address"],
            ]
        ),
        source=data["source"],
        as_of=data["asOf"],
        meta={"url": data["url"], "dataset": data["dataset"], "registryVersion": data["version"]},
    )


def _candidate(registry: Any, row: dict[str, Any], role: str) -> dict[str, Any]:
    # outreach가 이 모듈을 불러오므로(sameComplex) 순환을 피해 여기서 불러온다
    from app.v3 import outreach

    factory, vendor = row["factory"], row.get("vendor")
    evidence = []
    if factory is not None:
        evidence.append(registry.put(factory_evidence(factory)).id)
    if vendor is not None:
        evidence.append(registry.put(outreach.ref_evidence(vendor)).id)
    base = factory or vendor
    out: dict[str, Any] = {
        "id": base["id"],
        "name": base["name"],
        "address": base["address"],
        "role": role,
        "product": factory["product"] if factory else None,
        "items": row["items"],
        "resinMatch": row["match"],
        "resins": (factory["roles"].get("reprocess" if role == "takes" else "use") or {}).get(
            "resins", []
        )
        if factory
        else [],
        "evidence": evidence,
    }
    if role == "takes":
        out["permit"] = {
            "status": row["permit"],
            "vendorId": vendor["id"] if vendor else None,
            "business": vendor["business"] if vendor else None,
            "phone": vendor.get("phone") if vendor else None,
            "agency": vendor.get("agency") if vendor else None,
            "matchedCodes": [
                {"code": c, "name": public_vendors.code_name(c)} for c in row["matchedCodes"]
            ],
        }
    return out


def find(
    registry: Any,
    emitter: dict[str, Any],
    streams: list[dict[str, Any]],
    *,
    exclude_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """흐름마다 같은 단지 후보. 결과 한 장의 complexPartners 모양이다."""
    data, permits = factories(), public_vendors.registry()
    where = complex_of(emitter)
    head: dict[str, Any] = {
        "complex": {"id": where[0]["id"], "name": where[0]["name"], "basis": where[1]}
        if where
        else None,
        "reason": None if where else NO_COMPLEX_REASON,
        "source": {
            "factories": {
                "name": data["source"],
                "url": data["url"],
                "asOf": data["asOf"],
                "note": data["note"],
            },
            "registry": {
                "name": permits["source"],
                "url": permits["url"],
                "asOfRange": permits.get("asOfRange"),
            },
        },
        "streams": [],
    }
    if where is None:
        return head
    head["stats"] = complex_stats(registry, where[0]["id"])
    for stream in streams:
        found = search(stream, emitter, exclude_names=exclude_names)
        head["streams"].append(
            {
                "streamId": stream["id"],
                "takes": [_candidate(registry, r, "takes") for r in found["takes"][:TAKES_LIMIT]],
                "uses": [_candidate(registry, r, "uses") for r in found["uses"][:USES_LIMIT]],
                "takesTotal": len(found["takes"]),
                "usesTotal": len(found["uses"]),
                "reason": found["reason"],
            }
        )
    return head


def vendor_ids(partners: dict[str, Any] | None) -> set[tuple[str, str]]:
    """(흐름 ID, 허가 명부 업체 ID): 같은 단지 후보 가운데 견적 요청서를 만들 수 있는 곳."""
    out = set()
    for entry in (partners or {}).get("streams") or []:
        for item in entry.get("takes") or []:
            vendor_id = (item.get("permit") or {}).get("vendorId")
            if vendor_id:
                out.add((entry["streamId"], vendor_id))
    return out


def recheck(
    stream: dict[str, Any],
    vendor_id: str,
    emitter: dict[str, Any],
    *,
    exclude_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """허가 명부 업체 하나가 지금 흐름 자료로도 같은 단지 takes 후보인지(견적 요청서를 만들기 전에 다시 본다).

    {"ok": bool, "reason": None | public_vendors.recheck와 같은 이유 | "not_in_complex"}. 순위는 보지 않는다.
    """
    if public_vendors.get(vendor_id) is None:
        return {"ok": False, "reason": "not_listed"}
    if public_vendors._norm(public_vendors.get(vendor_id)["name"]) in {
        public_vendors._norm(n) for n in exclude_names if n
    }:
        return {"ok": False, "reason": "registered"}
    if not public_vendors.stream_codes(stream):
        return {
            "ok": False,
            "reason": "non_plastic" if public_vendors.non_plastic(stream) else "unknown_material",
        }
    if complex_of(emitter) is None:
        return {"ok": False, "reason": "not_in_complex"}
    found = search(stream, emitter, exclude_names=exclude_names)
    if any((r.get("vendor") or {}).get("id") == vendor_id for r in found["takes"]):
        return {"ok": True, "reason": None}
    return {"ok": False, "reason": "codes"}


def same_complex_vendor_ids(emitter: dict[str, Any]) -> set[str]:
    """회사와 같은 단지에 있는 허가 명부 업체 ID(단지 범위 안이거나, 같은 단지 등록공장과 상호가 같음)."""
    where = complex_of(emitter)
    if where is None:
        return set()
    complex_ = where[0]
    names = {
        (public_vendors._norm(f["name"]), _province(f["address"]))
        for f in factories()["factories"]
        if f["complexId"] == complex_["id"]
    }
    return {
        v["id"]
        for v in public_vendors.registry()["vendors"]
        if _inside(complex_, v["lon"], v["lat"])
        or (public_vendors._norm(v["name"]), _province(v["address"])) in names
    }
