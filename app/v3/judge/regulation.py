"""③ 조항 검색, 요건 판정, 허가 품목 대조. 조항은 law 근거의 원문에서만 따온다."""

from __future__ import annotations

import re

from app.v3.judge.common import Recorder, permit_match

_SPLIT = re.compile(r"(?<=[.。다])\s+|\n+")
ROUTE_TOPICS = {"sell": ["재활용", "위탁"], "treat": ["처리", "위탁"], "keep": ["소각", "처리"]}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,·/()]+", str(text)) if len(t) >= 2]


def _snippet(excerpt: str, tokens: list[str]) -> str:
    """원문에서 낱말이 든 문장을 글자 그대로 잘라 낸다. 없으면 첫 문장."""
    parts = [p for p in _SPLIT.split(excerpt) if p.strip()]
    for part in parts:
        if any(t in part for t in tokens):
            return part.strip()
    return (parts[0] if parts else excerpt).strip()


def _search(rec: Recorder, query: str, limit: int):
    tokens = _tokens(query)
    scored = []
    for law in rec.registry.list(kinds=["law"], org_id=rec.org_id):
        if law.expired(rec.today):
            continue
        haystack = f"{law.label} {law.excerpt}"
        score = sum(haystack.count(t) for t in tokens)
        if score > 0:
            scored.append((-score, law.id, law))
    scored.sort(key=lambda s: s[:2])
    return [(law, _snippet(law.excerpt, tokens)) for _, _, law in scored[:limit]]


def _has_law(rec: Recorder) -> bool:
    return bool(rec.registry.list(kinds=["law"], org_id=rec.org_id))


def search_clauses(registry, *, org_id, today, query, limit=5):
    rec = Recorder("search_clauses", registry, org_id, today, {"query": query, "limit": limit})
    if not _has_law(rec):
        rec.check("law_index", "conditional", "법령 근거가 적재되지 않았습니다. 확인 필요.")
        return rec.result("needs_information", {"results": []})
    results = []
    for i, (law, quote) in enumerate(_search(rec, query, int(limit))):
        claim = rec.clause(f"clause{chr(ord('A') + i)}", quote, law.id)
        results.append(
            {
                "evidenceId": law.id,
                "label": law.label,
                "source": law.source,
                "asOf": law.as_of,
                "claim": claim["id"],
            }
        )
    return rec.result("calculated", {"results": results})


def check_requirements(registry, *, org_id, today, stream, route, topics=None):
    """관련 조항을 찾아 보여 준다. 충족 여부는 스스로 단정하지 않고 담당자 확인으로 남긴다."""
    args = {"stream": stream, "route": route, "topics": topics}
    rec = Recorder("check_requirements", registry, org_id, today, args)
    output = {"streamId": stream.get("id"), "route": route, "items": []}
    if not _has_law(rec):
        rec.check("law_index", "conditional", "법령 근거가 적재되지 않았습니다. 확인 필요.")
        return rec.result("needs_information", output)
    wanted = list(topics or [stream.get("wasteItem"), *ROUTE_TOPICS.get(route, [])])
    index = 0
    for topic in [t for t in wanted if t]:
        hits = _search(rec, str(topic), 1)
        if not hits:
            rec.check(
                f"topic:{topic}",
                "conditional",
                f"'{topic}' 관련 조항을 찾지 못했습니다. 확인 필요.",
            )
            output["items"].append({"topic": topic, "claim": None})
            continue
        law, quote = hits[0]
        claim = rec.clause(f"clause{chr(ord('A') + index)}", quote, law.id)
        index += 1
        rec.check(
            f"topic:{topic}",
            "conditional",
            f"'{topic}' 관련 조항이 있습니다. 적용 여부는 담당자 확인이 필요합니다.",
            None,
            [law.id],
        )
        output["items"].append({"topic": topic, "claim": claim["id"], "evidenceId": law.id})
    return rec.result("conditional", output)


def match_permit(registry, *, org_id, today, stream, vendor):
    args = {"stream": stream, "vendor": vendor}
    rec = Recorder("match_permit", registry, org_id, today, args, [vendor.get("orgId")])
    matched, evidence = permit_match(stream, vendor)
    output = {
        "streamId": stream.get("id"),
        "vendorOrgId": vendor.get("orgId"),
        "matched": matched,
        "openThread": matched is True,
        "permitEvidence": rec.visible(evidence),
    }
    if matched is None:
        rec.check("permit", "conditional", "업체 허가 품목이 올라와 있지 않습니다. 확인 필요.")
        output["reason"] = "허가증이 확인되기 전에는 대화를 열지 않습니다."
        return rec.result("needs_information", output)
    if matched:
        rec.check("permit", "met", "허가 품목에 이 흐름의 품목과 재질이 있습니다.", None, evidence)
        output["reason"] = "허가 품목이 맞습니다."
    else:
        rec.check("permit", "unmet", "허가 품목에 이 흐름의 품목·재질이 없습니다.", None, evidence)
        output["reason"] = "허가 품목이 맞지 않아 대화를 열지 않습니다."
    return rec.result("calculated", output)
