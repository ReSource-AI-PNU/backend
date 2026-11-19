"""허가 확인 상태. 배정 후보(업체 · 폐기물 품목 · 처리 방식)마다 허가를 무엇으로 확인했는지 가른다.

"업체가 입력한 허가 품목이 이 폐기물과 맞는다"와 "담당자가 허가증 원문을 대조했다"는 다른 일이다. 앞의 것은
대화를 열지 정하는 문턱(common.permit_match)이고, 이 모듈은 그 허가를 실제로 무엇으로 확인했는지 가른다.

| 수준 | 화면 이름 | 판정 | 언제 |
|---|---|---|---|
| verified | 허가증 대조 완료 | 충족 | 배출 기업 담당자가 그 업체가 올린 허가증 원문에서 이 품목과 처리 방식을 대조해 맞다고 기록했다 |
| pending_review | 허가증 검토 대기 | 조건부 | 업체가 허가증을 올렸고(AI가 읽음, 업체가 값을 확인했어도) 담당자의 원문 대조 기록이 없다 |
| registered | 등록 품목 일치 · 허가증 원본 확인 필요 | 조건부 | 업체가 입력한 허가 품목, 공공 허가 명부, 예전 방식의 문서 값(원문 파일 없음)이 맞을 뿐 대조할 원문이 없다 |
| mismatch | 이 경로로 진행 불가 | 불충족(배정하지 않는다) | 원문 대조 결과 품목·처리 방식이 다르거나, 업체가 확인한 허가증 업종·공공 명부가 처리 방식과 어긋난다 |

규칙
- 대조 기록은 이 업체 · 이 폐기물(품목과 재질) · 이 처리 방식에만 쓴다. 다른 업체, 다른 품목·재질, 다른 처리 방식의
  기록이나 문서로 이 경로의 상태를 채우지 않는다. 같은 범위의 기록이 여럿이면 가장 나중 것을 쓴다.
- 문서 업로드 성공, AI 추출 성공, 업체의 추출값 확인, 공공 명부 후보 검색만으로는 대조 완료가 되지 않는다.
- 허가증의 상호나 소재지가 이 업체와 다르면 그 문서는 쓰지 않고(ignored에 이유를 남긴다), 다른 조직의 doc 근거도 세지 않는다.
- 공공 명부는 상호만으로 찾으므로 업체 차고지 좌표가 있어 같은 곳(SAME_PLACE_KM 안)으로 볼 수 있을 때만 쓰고, 맞아도
  "등록 품목 일치"에 머문다. 명부가 처리 방식과 어긋나면 진행 불가로 둔다(담당자가 원문으로 맞다고 대조하면 그 기록이 앞선다).
- 허가의 처리 방법은 업종으로 본다: 처리 방식이 소각·매립이면 처분업, 그 밖(재활용, 열분해)이면 재활용업이어야 한다.
- 문장에는 숫자를 쓰지 않는다(쪽, 날짜, 분류번호는 check·document 칸과 근거의 원문에 있다).
"""

from __future__ import annotations

import re
from typing import Any

from app.v3 import ids, public_vendors
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.judge.common import haversine_km, num
from app.v3.textutil import josa

LEVELS = ("verified", "pending_review", "registered", "mismatch")
LABELS = {
    "verified": "허가증 대조 완료",
    "pending_review": "허가증 검토 대기",
    "registered": "등록 품목 일치 · 허가증 원본 확인 필요",
    "mismatch": "이 경로로 진행 불가",
}
STATUS = {
    "verified": "met",
    "pending_review": "conditional",
    "registered": "conditional",
    "mismatch": "unmet",
}
SAME_PLACE_KM = 50.0  # 같은 상호라도 업체 차고지에서 이보다 먼 사업장은 다른 곳으로 본다
REGISTRY_SERIES = "허가명부"
SUBMIT = "폐기물처리업 허가증 사본(재활용이면 재활용 신고증명서도 됨)"


def matched_permit(stream: dict, vendor: dict) -> dict | None:
    """이 폐기물의 품목·재질이 든 허가 품목(permit_match와 같은 규칙). 없으면 None."""
    for permit in vendor.get("permits") or []:
        materials = permit.get("materials") or []
        if permit.get("wasteItem") == stream.get("wasteItem") and (
            not materials or stream.get("material") in materials
        ):
            return permit
    return None


def needed_business(vendor: dict) -> str:
    """이 업체의 처리 방식에 필요한 허가 업종의 낱말. 소각·매립이면 "처분", 그 밖이면 "재활용"."""
    return business_for(vendor.get("process"))


def business_for(process: Any) -> str:
    text = str(process or "")
    return "처분" if any(word in text for word in ("소각", "매립")) else "재활용"


def process_of(vendor: dict) -> str:
    return str(vendor.get("process") or "재활용")


def _fits(business: Any, need: str) -> bool:
    return need in str(business or "").replace(" ", "")


def scope_of(stream: dict) -> str:
    """대조 범위의 품목 글: "폐플라스틱(PP)"."""
    item = str(stream.get("wasteItem") or "폐기물")
    material = stream.get("material")
    return f"{item}({material})" if material else item


def in_scope(record: dict, stream: dict, process: str) -> bool:
    """대조 기록(또는 그 범위)이 이 폐기물의 품목·재질과 이 처리 방식에 해당하는가."""
    materials = list(record.get("materials") or [])
    return (
        record.get("wasteItem") == stream.get("wasteItem")
        and (not materials or stream.get("material") in materials)
        and str(record.get("process") or "") == process
    )


# ── 문서의 주인(상호·소재지) ─────────────────────────────────────────

_DISTRICT = re.compile(r"([가-힣]{1,6}?(?:구|군))(?=\s|$|[,·)])")
_CITY = re.compile(r"([가-힣]{2,4}?)(?:광역시|특별시|특별자치시|시)(?=\s|$|[,·)])")


def _places(text: Any) -> set[str]:
    """주소 글에서 시·군·구 이름을 뽑는다("부산광역시 강서구 미음동" → {"부산", "강서구"})."""
    words = str(text or "").replace("(", " ").replace(")", " ")
    found = {m.group(1) for m in _DISTRICT.finditer(words)}
    found |= {m.group(1) for m in _CITY.finditer(words)}
    return {w for w in found if len(w) >= 2}


def holder_of(document: dict, vendor: dict) -> dict[str, Any]:
    """허가증에 적힌 상호·소재지가 이 업체와 같은가. 모르면 None.

    상호는 회사 형태 표기를 뗀 이름이 같아야 하고, 소재지는 문서의 구·군이 업체 주소·지역의 구·군과 하나라도 겹쳐야 한다.
    """
    holder = dict(document.get("holder") or {})
    name = holder.get("name")
    mine = vendor.get("name")
    name_ok = None
    if name and mine:
        name_ok = public_vendors._norm(str(name)) == public_vendors._norm(str(mine))
    site_ok = None
    there = _places(holder.get("address"))
    here = _places(" ".join(str(vendor.get(k) or "") for k in ("address", "region")))
    # 구·군이 양쪽에 있으면 그것으로, 없으면 시 이름으로 견준다. 어느 한쪽이라도 모르면 판단하지 않는다
    for pick in (lambda p: p.endswith(("구", "군")), lambda p: not p.endswith(("구", "군"))):
        a, b = {p for p in there if pick(p)}, {p for p in here if pick(p)}
        if a and b:
            site_ok = bool(a & b)
            break
    return {**holder, "nameMatches": name_ok, "siteMatches": site_ok}


def usable_documents(vendor: dict) -> tuple[list[dict], list[dict]]:
    """이 업체가 올린 허가증 가운데 쓸 수 있는 것과 쓰지 않는 것({name, reason}).

    다른 조직이 올린 문서, 상호나 소재지가 이 업체와 다른 문서는 쓰지 않는다.
    """
    usable, ignored = [], []
    for document in vendor.get("permitDocuments") or []:
        if document.get("orgId") and document.get("orgId") != vendor.get("orgId"):
            ignored.append(
                {"name": document.get("name"), "reason": "다른 업체가 올린 허가증입니다."}
            )
            continue
        holder = holder_of(document, vendor)
        if holder["nameMatches"] is False:
            ignored.append(
                {
                    "name": document.get("name"),
                    "reason": "허가증의 상호가 이 업체와 달라 다른 업체의 허가증으로 보고 쓰지 않았습니다.",
                }
            )
            continue
        if holder["siteMatches"] is False:
            ignored.append(
                {
                    "name": document.get("name"),
                    "reason": "허가증의 소재지가 이 업체 사업장과 달라 다른 사업장의 허가증으로 보고 쓰지 않았습니다.",
                }
            )
            continue
        usable.append({**document, "holder": holder})
    return usable, ignored


def _document_name(registry, evidence_id: str) -> str | None:
    """doc 근거가 나온 문서 이름(meta.document). 모르면 None."""
    try:
        return (registry.get(evidence_id, None).meta or {}).get("document")
    except (EvidenceNotFound, EvidenceForbidden):  # 근거가 없으면 이름을 모르는 것으로 본다
        return None


def _own_doc(evidence_id: str, vendor: dict) -> bool:
    """이 업체 자신의 doc 근거인가(doc:<업체 ID>:...)."""
    return str(evidence_id).startswith(f"doc:{vendor.get('orgId')}:")


# ── 공공 허가 명부 ───────────────────────────────────────────────────


def registry_records(name: Any, near: Any = None) -> list[dict[str, Any]]:
    """상호가 같은 공공 명부 업체(같은 상호가 업종만 달리 여러 줄이면 모두). near는 [경도, 위도].

    상호만으로는 같은 업체인지 알 수 없으므로 near(업체 차고지)가 없으면 아무것도 돌려주지 않는다.
    """
    key = public_vendors._norm(str(name or ""))
    point = near if isinstance(near, (list, tuple)) and len(near) == 2 else None
    if not key or point is None or any(num(x) is None for x in point):
        return []
    found = [
        v
        for v in public_vendors.registry()["vendors"]
        if public_vendors._norm(v["name"]) == key
        and haversine_km(point, (v["lon"], v["lat"])) <= SAME_PLACE_KM
    ]
    return sorted(found, key=lambda v: v["id"])


def _registry_evidence(registry, records: list[dict[str, Any]]) -> str:
    """공공 명부의 그 업체 줄을 근거(ref)로 올린다. 공공데이터라 모두에게 열린다."""
    data = public_vendors.registry()
    first = records[0]
    codes = sorted({c for r in records for c in r.get("codes") or []})
    business = " · ".join(dict.fromkeys(r["business"] for r in records))
    named = [f"{c} {public_vendors.code_name(c) or ''}".strip() for c in codes]
    evidence = Evidence(
        id=ids.ref_id(REGISTRY_SERIES, first["id"], first.get("asOf") or "na"),
        kind="ref",
        label=f"공공 허가 명부: {first['name']} ({business})",
        excerpt=(
            f"상호 {first['name']} | 업종 {business} | 허가일 {first.get('permitDate') or '없음'} | "
            f"허가 기관 {first.get('agency') or '없음'} | 소재지 {first.get('address') or '없음'} | "
            f"처리 폐기물 분류번호 {', '.join(named) or '없음'} | 명부 기준일 {first.get('asOf') or '없음'}"
        ),
        source=f"{data['source']} ({data['url']})",
        as_of=first.get("asOf") or data["asOfRange"][-1],
        meta={
            "series": REGISTRY_SERIES,
            "registryIds": [r["id"] for r in records],
            "codes": codes,
            "codesReliable": all(r.get("codesReliable", True) for r in records),
            "agency": first.get("agency"),
        },
    )
    return registry.put(evidence).id


def _stale(as_of: Any, today: Any) -> bool:
    """기준일(YYYY-MM-DD)이 오늘보다 한 해 넘게 앞서는가. 판단 경로라 시계·날짜 모듈 없이 글자로 비교한다."""
    a, t = str(as_of or ""), str(today or "")
    if len(a) != 10 or len(t) != 10 or not t[:4].isdigit():
        return False
    return a < f"{int(t[:4]) - 1:04d}{t[4:]}"


def _registry(registry, stream, vendor, need, process, evidence, today):
    """(공공 명부가 어긋나면 그 문장 | None, 맞으면 그 문장 | None, 명부 업종)."""
    depot = (vendor.get("fleet") or {}).get("depot")
    records = registry_records(vendor.get("name"), depot)
    if not records:
        return None, None, None
    evidence.append(_registry_evidence(registry, records))
    listed = " · ".join(dict.fromkeys(r["business"] for r in records))
    reliable = all(r.get("codesReliable", True) for r in records)
    held = {c for r in records for c in r.get("codes") or []}
    wanted = public_vendors.stream_codes(stream)
    if not any(_fits(r["business"], need) for r in records):
        return (
            f"공공 허가 명부의 업종({listed})에 이 업체의 처리 방식({process})에 필요한 {need}업이 없습니다.",
            None,
            listed,
        )
    if reliable and wanted and not any(c in held for c in wanted):
        return (
            f"공공 허가 명부({listed})의 처리 폐기물 분류에 이 폐기물이 없습니다.",
            None,
            listed,
        )
    agency = next((r.get("agency") for r in records if r.get("agency")), None)
    where = f"{josa(agency, '이가')} 올린 " if agency else ""
    stale = (
        " 명부의 기준일이 한 해 넘게 지났습니다." if _stale(records[0].get("asOf"), today) else ""
    )
    return (
        None,
        (
            f"차고지 근처의 같은 상호가 {where}공공 허가 명부({listed})에 있습니다.{stale} "
            "명부는 같은 업체인지와 허가 범위를 확정하지 못하는 후보 자료입니다."
        ),
        listed,
    )


# ── 판정 ─────────────────────────────────────────────────────────────


def _todo(stream: dict, need: str, process: str) -> dict[str, Any]:
    return {
        "submit": SUBMIT,
        "items": [
            f"영업대상 폐기물에 {josa(scope_of(stream), '이가')} 들어 있는지",
            f"업종이 {need}업이고 처리 방법이 {process}인지",
        ],
    }


def _check_view(record: dict) -> dict[str, Any]:
    keys = (
        "id",
        "documentId",
        "document",
        "page",
        "wasteItem",
        "materials",
        "process",
        "itemFound",
        "processFits",
        "itemQuote",
        "processQuote",
        "checkedBy",
        "checkedAt",
        "evidence",
    )
    return {k: record.get(k) for k in keys}


def _document_view(document: dict) -> dict[str, Any]:
    keys = ("id", "name", "uploadedAt", "pages", "items", "business", "holder", "evidence")
    return {k: document.get(k) for k in keys}


def _who(record: dict) -> str:
    """문장에 쓰는 대조한 쪽. 계정 이름은 문장에 넣지 않는다(업체도 이 문장을 본다. 이름은 check.checkedBy에만)."""
    by = record.get("checkedBy") or {}
    return f"{by.get('orgName') or '배출 기업'} 담당자"


def latest_check(vendor: dict, stream: dict, process: str) -> dict | None:
    """이 업체 · 이 폐기물 · 이 처리 방식의 대조 기록 가운데 가장 나중 것.

    이 업체가 올려 지금 쓸 수 있는 허가증(usable_documents)으로 한 기록만 센다. 다른 업체의 기록이나
    상호·소재지가 다른 문서로 한 기록은 세지 않는다.
    """
    documents = {d.get("id") for d in usable_documents(vendor)[0]}
    found = [
        c
        for c in vendor.get("permitChecks") or []
        if in_scope(c, stream, process)
        and c.get("vendorOrgId") == vendor.get("orgId")
        and c.get("documentId") in documents
    ]
    found.sort(key=lambda c: (str(c.get("checkedAt") or ""), str(c.get("id") or "")))
    return found[-1] if found else None


def _result(level: str, text: str, evidence: list[str], **extra) -> dict[str, Any]:
    return {
        "level": level,
        "label": LABELS[level],
        "status": STATUS[level],
        "text": text,
        "evidence": list(dict.fromkeys(evidence)),
        **extra,
    }


def permit_level(registry, stream: dict, vendor: dict, today: Any = None) -> dict[str, Any] | None:
    """이 업체로 이 폐기물을 보낼 때의 허가 확인 상태. 허가 품목이 맞지 않거나 없으면 None.

    vendor에는 permits(허가 품목), process(처리 방식), name·address·region(상호·주소), fleet.depot(차고지),
    permitDocuments(업체가 올린 허가증), permitChecks(이 배출 기업 담당자의 대조 기록)를 싣는다.
    돌려주는 칸: level, label, status(met|conditional|unmet), text, evidence, source(check|document|registry|typed),
    wasteItem, materials, process, need(필요 업종 낱말), business(문서·명부의 업종), check, document, ignored, todo.
    """
    permit = matched_permit(stream, vendor)
    if permit is None:
        return None
    process = process_of(vendor)
    need = needed_business(vendor)
    base = {
        "wasteItem": stream.get("wasteItem"),
        "materials": [stream["material"]] if stream.get("material") else [],
        "process": process,
        "need": f"{need}업",
        "business": permit.get("business"),
        "check": None,
        "document": None,
        "ignored": [],
        "todo": _todo(stream, need, process),
    }
    usable, ignored = usable_documents(vendor)
    base["ignored"] = ignored
    typed = [e for e in permit.get("evidence") or [] if not str(e).startswith("doc:")]
    # 이 업체 자신의 doc 근거만 센다. 쓰지 않기로 한 허가증(다른 업체·사업장)에서 확인한 값도 세지 않는다
    skipped = {x.get("name") for x in ignored}
    own_docs = [
        e
        for e in permit.get("evidence") or []
        if _own_doc(e, vendor) and _document_name(registry, e) not in skipped
    ]
    evidence = [*typed, *own_docs]

    record = latest_check(vendor, stream, process)
    if record is not None:
        cited = [*evidence, *([record["evidence"]] if record.get("evidence") else [])]
        document = next((d for d in usable if d.get("id") == record.get("documentId")), None)
        if document is not None:
            cited += document.get("evidence") or []
        extra = {
            **base,
            "source": "check",
            "check": _check_view(record),
            "document": _document_view(document) if document else None,
        }
        if record.get("itemFound") and record.get("processFits"):
            extra["todo"] = None
            return _result(
                "verified",
                f"{josa(_who(record), '이가')} 허가증 원문에서 {josa(scope_of(stream), '와과')} "
                f"{josa(f'처리 방식 {process}({need}업)', '을를')} 대조해 맞다고 기록했습니다. "
                "이 확인은 이 폐기물과 처리 방식에만 해당합니다.",
                cited,
                **extra,
            )
        reasons = []
        if not record.get("itemFound"):
            reasons.append(f"허가증에 {josa(scope_of(stream), '이가')} 없습니다")
        if not record.get("processFits"):
            reasons.append(
                f"추천한 {josa(f'처리 방식 {process}({need}업)', '이가')} 허가증의 업종·처리 방법과 다릅니다"
            )
        return _result(
            "mismatch",
            f"{josa(_who(record), '이가')} 허가증 원문과 대조한 결과 {', '.join(reasons)}. "
            "이 경로로는 진행하지 않습니다.",
            cited,
            **extra,
        )

    # 대조 기록이 없다. 업체가 확인한 허가증 업종이나 공공 명부가 처리 방식과 어긋나면 맡기지 않는다
    business = permit.get("business") if own_docs else None
    if business and not _fits(business, need):
        return _result(
            "mismatch",
            f"업체가 확인한 허가증의 업종({business})이 이 업체의 처리 방식({process})에 필요한 {need}업이 "
            "아닙니다. 담당자가 허가증 원문으로 맞다고 대조하면 다시 계산합니다.",
            evidence,
            **{**base, "source": "document"},
        )
    found = list(evidence)
    against, registered, listed = _registry(registry, stream, vendor, need, process, found, today)
    if against:
        return _result(
            "mismatch",
            f"{against} 담당자가 허가증 원문으로 맞다고 대조하기 전에는 이 폐기물을 맡기지 않습니다.",
            found,
            **{**base, "source": "registry", "business": listed},
        )

    if usable:
        document = max(usable, key=lambda d: str(d.get("uploadedAt") or ""))
        cited = [*found, *(document.get("evidence") or [])]
        read = next(
            (
                item
                for item in document.get("items") or []
                if in_scope({**item, "process": process}, stream, process)
            ),
            None,
        )
        shown = document.get("business") or {}
        says = (
            f"AI가 읽은 허가 품목에 {josa(scope_of(stream), '이가')} 있습니다"
            if read
            else f"AI가 읽은 값에서 {josa(scope_of(stream), '을를')} 찾지 못했습니다"
        )
        if shown.get("name") and not _fits(shown.get("name"), need):
            says += f". AI가 읽은 업종({shown['name']})이 {josa(f'처리 방식 {process}', '와과')} 다를 수 있습니다"
        return _result(
            "pending_review",
            f"업체가 허가증을 올렸습니다. {says}. 담당자가 원문에서 품목과 처리 방법을 대조하기 전에는 "
            "확정하지 않습니다.",
            cited,
            **{**base, "source": "document", "document": _document_view(document)},
        )
    if own_docs:
        # 예전 방식(업체가 문서에서 읽은 값을 확인만 함)의 근거. 원문 파일도 담당자의 대조 기록도 없다
        return _result(
            "registered",
            "업체가 허가증 문서에서 읽어 확인한 허가 품목이 이 폐기물과 맞습니다. 다만 대조할 허가증 원문이 "
            "올라와 있지 않고 담당자의 원문 대조 기록도 없습니다. 허가증 사본을 받아 품목과 처리 방법을 "
            "대조해야 합니다.",
            found,
            **{**base, "source": "document"},
        )
    head = "업체가 입력한 허가 품목이 이 폐기물과 맞습니다."
    if registered:
        return _result(
            "registered",
            f"{head} {registered} 허가증 원본으로 품목과 처리 방법을 대조해야 합니다.",
            found,
            **{**base, "source": "registry", "business": listed},
        )
    return _result(
        "registered",
        f"{head} 허가증 원본은 아직 없습니다. 허가증 사본을 받아 품목과 처리 방법을 대조해야 합니다.",
        found,
        **{**base, "source": "typed"},
    )


def context(
    profile: dict[str, Any], org: dict[str, Any] | None, checker: str | None
) -> dict[str, Any]:
    """판단 도구에 넘기는 업체 프로필에 더할 허가 확인 자료. 없는 것은 키도 싣지 않는다(계산 근거 ID가 바뀌지 않게).

    - permitDocuments: 업체가 올린 허가증(AI가 읽은 값, 대조 전)
    - permitChecks: checker(이 진단의 배출 기업)의 담당자가 남긴 대조 기록만. 다른 배출 기업의 기록은 쓰지 않는다
    - address·region: 허가증의 소재지와 견줄 업체 주소(허가증이 있을 때만)
    """
    out: dict[str, Any] = {}
    documents = list(profile.get("permitDocuments") or [])
    if documents:
        out["permitDocuments"] = documents
        for key in ("address", "region"):
            if (org or {}).get(key):
                out[key] = org[key]
    checks = [
        c
        for c in profile.get("permitChecks") or []
        if checker and (c.get("checkedBy") or {}).get("orgId") == checker
    ]
    if checks:
        out["permitChecks"] = checks
    return out


def short_view(level: dict[str, Any] | None) -> dict[str, Any] | None:
    """화면과 승인 카드에 싣는 허가 상태(근거 ID 목록은 그대로)."""
    if level is None:
        return None
    keys = (
        "level",
        "label",
        "status",
        "text",
        "evidence",
        "source",
        "wasteItem",
        "materials",
        "process",
        "need",
        "business",
        "check",
        "document",
        "ignored",
        "todo",
    )
    return {k: level.get(k) for k in keys}
