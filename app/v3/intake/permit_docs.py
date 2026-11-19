"""업체가 올린 허가증을 "검토 대기" 문서로 남긴다. AI가 읽은 값은 담당자가 원문과 대조하기 전에는 확인이 아니다.

upload_document(kind="permit")가 부른다. 남기는 것은 문서 이름·올린 때·쪽 수, AI가 읽은 허가 품목·업종·상호·소재지와
그 값이 적힌 쪽과 원문 표기다. 파일 자체는 저장하지 않는다(원문 대조는 그 문서를 가진 담당자가 한다).
읽은 표기는 doc 근거 하나로 등록해 근거 패널에서 열리게 하고, source에 "AI가 읽은 표기(담당자 대조 전)"라고 적는다.
허가 확인 상태는 judge/permits.py가 이 기록과 담당자의 대조 기록(permitChecks)으로 가른다.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.intake import documents

KEEP = 5  # 업체 프로필에 남기는 허가증 수(나중 것부터)
QUOTE_MAX = 160


def _pages(layout: dict[str, Any]) -> list[tuple[int, str]]:
    """[(쪽 번호, 그 쪽의 글)]. Document Intelligence 결과면 pages[].lines, 글 파일이면 한 쪽으로 본다."""
    found = []
    for page in layout.get("pages") or []:
        number = page.get("pageNumber")
        lines = [str(line.get("content") or "") for line in page.get("lines") or []]
        if isinstance(number, int) and lines:
            found.append((number, "\n".join(lines)))
    if found:
        return found
    text = documents.layout_text(layout)
    if not text.strip():
        return []
    parts = text.split("\f")
    return [(i + 1, part) for i, part in enumerate(parts) if part.strip()]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _where(pages: list[tuple[int, str]], raw: str) -> tuple[int | None, str]:
    """원문 표기 raw가 적힌 쪽과, 그 쪽에서 raw가 든 줄(인용). 못 찾으면 (None, raw 앞부분)."""
    want = _compact(raw)[:24]
    if not want:
        return None, ""
    for number, text in pages:
        for line in text.splitlines():
            if want[:12] and want[:12] in _compact(line):
                return number, line.strip()[:QUOTE_MAX]
        if want in _compact(text):
            return number, raw.strip()[:QUOTE_MAX]
    return None, raw.strip()[:QUOTE_MAX]


def _value(layout: dict[str, Any], pattern: str) -> str | None:
    pair = documents._first_value(documents.key_values(layout), pattern)
    if not pair:
        return None
    return documents._cut_next_key(str(pair["value"] or "")).strip()[:120] or None


def read(
    registry: Any,
    org_id: str,
    filename: str,
    data: bytes,
    layout: dict[str, Any],
    extracted: dict[str, Any],
    *,
    uploaded_at: str,
    today: str,
    uploaded_by: str,
) -> dict[str, Any]:
    """허가증 한 건의 검토 대기 기록과 그 읽은 표기의 doc 근거.

    {"id", "orgId", "name", "uploadedAt", "uploadedBy", "pages", "items": [{wasteItem, materials, page, quote}],
     "business": {name, page, quote} | None, "holder": {name, address, page, quote}, "permitNumber",
     "evidence": [근거 ID], "readBy": "ai"}
    """
    pages = _pages(layout)
    digest = hashlib.sha256(data).hexdigest()[:12]
    items = []
    for entry in extracted.get("permits") or []:
        if not entry.get("wasteItem"):
            continue
        page, quote = _where(pages, str(entry.get("raw") or ""))
        items.append(
            {
                "wasteItem": entry["wasteItem"],
                "materials": list(entry.get("materials") or []),
                "page": page,
                "quote": quote,
            }
        )
    business = None
    raw_business = _value(layout, r"업종|영업의종류|영업종류|영업구분")
    named = documents.business_names(raw_business) if raw_business else None
    if named:
        page, quote = _where(pages, raw_business or "")
        business = {"name": named, "page": page, "quote": quote}
    name = _value(layout, r"상호|업체명")
    address = _value(layout, r"소재지|사업장주소|사업장소재지|주소")
    holder_page, holder_quote = _where(pages, name or address or "")
    holder = {"name": name, "address": address, "page": holder_page, "quote": holder_quote}
    lines = [f"{item['page'] or '?'}쪽 · {item['quote']}" for item in items if item.get("quote")]
    if business:
        lines.append(f"{business['page'] or '?'}쪽 · {business['quote']}")
    if name or address:
        lines.append(
            f"{holder_page or '?'}쪽 · 상호 {name or '읽지 못함'} · 소재지 {address or '읽지 못함'}"
        )
    evidence = registry.put(
        Evidence(
            id=ids.doc_id(org_id, "허가증-AI판독", digest),
            kind="doc",
            label=f"허가증(AI가 읽음, 대조 전): {filename}",
            excerpt="\n".join(lines) or "AI가 허가 품목을 읽지 못했습니다.",
            source=f"{filename} · AI가 읽은 표기(담당자 대조 전)",
            as_of=today,
            access=(org_id,),
            meta={
                "document": filename,
                "documentId": f"pd_{digest}",
                "readBy": "ai",
                "reviewed": False,
                "pages": [n for n, _ in pages],
            },
        )
    ).id
    return {
        "id": f"pd_{digest}",
        "orgId": org_id,
        "name": filename,
        "uploadedAt": uploaded_at,
        "uploadedBy": uploaded_by,
        "pages": [n for n, _ in pages],
        "items": items,
        "business": business,
        "holder": holder,
        "permitNumber": extracted.get("permitNumber"),
        "evidence": [evidence],
        "readBy": "ai",
    }


def remember(profile: dict[str, Any], document: dict[str, Any]) -> list[dict[str, Any]]:
    """업체 프로필의 permitDocuments에 넣는다. 같은 파일(같은 ID)이면 바꾸고, KEEP개 넘으면 오래된 것을 뺀다.

    오래된 문서를 빼도 그 문서로 한 대조 기록은 남지만 permits.latest_check가 세지 않는다(문서가 없으므로).
    """
    kept = [d for d in profile.get("permitDocuments") or [] if d.get("id") != document["id"]]
    kept.append(document)
    kept.sort(key=lambda d: str(d.get("uploadedAt") or ""))
    return kept[-KEEP:]


def document_of(profile: dict[str, Any], document_id: Any) -> dict[str, Any] | None:
    return next(
        (d for d in profile.get("permitDocuments") or [] if d.get("id") == document_id), None
    )
