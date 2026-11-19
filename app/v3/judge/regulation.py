"""③ 조항 검색, 요건 판정, 허가 품목 대조. 조항은 law 근거의 원문에서만 따온다.

조항 검색은 근거 검색(evidence/search.py)을 법령으로 좁혀 쓴다. 로컬 BM25F(한국어 낱말 나누기·분야 동의어)이고,
Azure AI Search가 연결돼 있으면 그 하이브리드·시맨틱 순위와 RRF로 합친다. 재순위기(evidence/rerank.py)가
연결돼 있으면 그 상위 40개를 모델이 다시 줄 세운다(조항 원문과 인용은 그대로다). 2026-11-19 검색 평가(test-v1,
80질의)에서 nDCG@10이 이전의 낱말 수 세기 0.138, 로컬 BM25F 0.617, 로컬 + Azure 0.621, 거기에 모델 재순위를
더한 것(운영 구성) 0.792였다(docs/v3/eval). Azure나 모델이 실패하면 그 앞 단계의 순위로 답한다.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from app.v3.evidence import lexical, search
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.evidence.validator import MIN_QUOTE_CHARS
from app.v3.judge.common import Recorder, permit_match
from app.v3.judge.permits import permit_level

_SPLIT = re.compile(r"(?<=[.。다])\s+|\n+")
QUOTE_MAX = 300  # 인용은 문장 하나, 길면 앞부분만(원문의 부분 문자열이라 검증기를 그대로 통과한다)


def _tokens(text: str) -> list[str]:
    """이전 판의 낱말 나누기. 검색 평가의 비교 기준(S0)이 쓴다."""
    return [t for t in re.split(r"[\s,·/()]+", str(text)) if len(t) >= 2]


def _snippet(excerpt: str, query: str) -> str:
    """질의 낱말(조사를 뗀 꼴)이 가장 많이 든 문장을 원문 그대로 잘라 낸다. 없으면 첫 문장."""
    wanted = {w for w in lexical.words(query) if len(w) >= 2}
    # 검증기는 10자보다 짧은 인용을 받지 않는다(조문 전체가 짧은 경우는 예외)
    parts = [p.strip() for p in _SPLIT.split(excerpt) if len(p.strip()) >= MIN_QUOTE_CHARS]
    if not parts:
        return excerpt.strip()[:QUOTE_MAX]
    scores = [sum(1 for w in wanted if w in part) for part in parts]
    best = max(range(len(parts)), key=lambda i: (scores[i], -i))
    return parts[best][:QUOTE_MAX]


def _search(rec: Recorder, query: str, limit: int):
    """[(법령 근거, 인용문)]. 시행 중인 조항만, 검색 순위대로."""
    found = []
    hits = search.search_evidence(
        rec.registry, query, kinds=["law"], org_id=rec.org_id, limit=max(limit * 3, 10)
    )
    for hit in hits:
        law = rec.registry.get(hit["evidence"]["id"], rec.org_id)
        if law.expired(rec.today):
            continue
        found.append((law, _snippet(law.excerpt, query)))
        if len(found) >= limit:
            break
    return found


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


REQUIREMENTS_FILE = Path(__file__).with_name("requirements.v1.json")
ROUTE_WORDS = {
    "sell": "재활용 위탁 매각",
    "treat": "처리 위탁 처리업",
    "keep": "소각 처분 위탁",
    "reuse": "자가 재활용 재활용시설",
}


@cache
def requirements() -> tuple[dict[str, Any], ...]:
    """경로별 요건 체크리스트(requirements.v1.json). 요건마다 법령 조항과 원문 인용이 붙어 있다.

    2026-11-19 법령 말뭉치(폐기물관리법·시행령·시행규칙·순환경제사회 전환 촉진법)에서 뽑았다. decidedBy가
    permit_match인 요건은 업체 허가 품목 대조로 판정하고, staff인 요건은 서비스가 확인할 수 없어 담당자 할 일로 낸다.
    """
    return tuple(json.loads(REQUIREMENTS_FILE.read_text(encoding="utf-8")))


def _permit_status(registry, stream, vendors) -> tuple[str, str, list[str]]:
    """허가 요건: 대화를 연 업체 가운데 허가 품목이 맞는 곳이 있는가, 그 허가를 무엇으로 확인했는가.

    허가 근거 수준(permits.permit_level)이 업체 입력뿐이거나 공공 명부와 다른 업체가 있으면 조건부다.
    그 업체로 보내기 전에 허가증 사본이나 공공 명부로 확인해야 하기 때문이다.
    """
    if not vendors:
        return (
            "conditional",
            "지금 맡기는 업체의 허가증에 이 폐기물이 들어 있는지 확인이 필요합니다.",
            [],
        )
    results = [(vendor, *permit_match(stream, vendor)) for vendor in vendors]
    matched = [r for r in results if r[1] is True]
    if matched:
        levels = [(r[0], permit_level(registry, stream, r[0])) for r in matched]
        evidence = list(dict.fromkeys(e for _, level in levels for e in level["evidence"]))
        named = all(vendor.get("name") for vendor, _ in levels)
        how = ", ".join(f"{vendor['name']}: {level['label']}" for vendor, level in levels)
        head = (
            f"허가 품목이 맞는 업체({', '.join(str(v['name']) for v, _ in levels)})와만 대화했습니다."
            if named
            else "허가 품목이 맞는 업체와만 대화했습니다."
        )
        weak = [(vendor, level) for vendor, level in levels if level["status"] != "met"]
        if weak:
            short = {"self": "업체 입력뿐", "mismatch": "공공 명부와 다름"}
            who = ", ".join(
                f"{v.get('name') or '업체'}({short.get(level['level'], level['label'])})"
                for v, level in weak
            )
            note = (
                f"{head} 다만 허가 근거가 약한 업체가 있습니다: {who}. 그 업체로 보내기 전에 "
                "허가증 사본이나 공공 허가 명부로 확인해야 합니다."
            )
            return "conditional", note, evidence
        tail = f" 허가 근거: {how}." if named else ""
        return "met", head + tail, evidence
    if any(r[1] is None for r in results):
        return "needs_information", "업체 허가 품목이 올라와 있지 않아 확인하지 못했습니다.", []
    return "unmet", "허가 품목이 맞는 업체가 없습니다. 이 폐기물은 넘기지 않습니다.", []


def check_requirements(registry, *, org_id, today, stream, route, topics=None, vendors=None):
    """경로에 걸리는 요건을 체크리스트로 판정하고, 폐기물과 경로로 찾은 관련 조항을 더한다.

    허가 요건은 업체 허가 품목 대조로 판정한다(vendors가 없으면 담당자 확인). 나머지는 서비스가 확인할 수 없는
    담당자 할 일이라 판정을 막지 않는다. 결과 상태는 허가 요건이 충족되면 calculated, 확인이 필요하면 conditional,
    허가 정보가 없으면 needs_information이다.
    """
    vendors = list(vendors or [])
    args = {
        "stream": stream,
        "route": route,
        "topics": topics,
        "vendors": sorted(str(v.get("orgId")) for v in vendors),
    }
    rec = Recorder("check_requirements", registry, org_id, today, args)
    output = {"streamId": stream.get("id"), "route": route, "items": [], "related": []}
    if not _has_law(rec):
        rec.check("law_index", "conditional", "법령 근거가 적재되지 않았습니다. 확인 필요.")
        return rec.result("needs_information", output)
    cited: set[str] = set()
    decided: list[str] = []
    for index, req in enumerate(r for r in requirements() if route in r["routes"]):
        claims, laws = [], []
        for number, clause in enumerate(req["clauses"]):
            try:
                law = registry.get(clause["evidenceId"], org_id)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
            if law.expired(today) or clause["quote"] not in law.excerpt:
                continue  # 조문이 바뀌었으면 인용하지 않는다(검증기가 거부할 인용을 내지 않는다)
            claims.append(rec.clause(f"req{index}c{number}", clause["quote"], law.id)["id"])
            laws.append(law.id)
            cited.add(law.id)
        if req["decidedBy"] == "permit_match":
            status, note, permit_evidence = _permit_status(registry, stream, vendors)
            decided.append(status)
        else:
            status, note, permit_evidence = "conditional", "담당자가 확인할 일입니다.", []
        rec.check(
            f"req:{req['key']}",
            status,
            f"{req['title']}: {note}",
            None,
            [*permit_evidence, *laws],
        )
        output["items"].append(
            {
                "key": req["key"],
                "title": req["title"],
                "applies": req.get("applies"),
                "action": req["action"],
                "decidedBy": req["decidedBy"],
                "status": status,
                "note": note,
                "claims": claims,
                "evidence": list(dict.fromkeys([*permit_evidence, *laws])),
            }
        )
    # 폐기물과 경로로 찾은 관련 조항(체크리스트에 없는 것). 담당자가 따로 준 주제가 있으면 그것도 찾는다
    queries = [
        " ".join(
            str(x)
            for x in (stream.get("wasteItem"), stream.get("material"), ROUTE_WORDS.get(route, ""))
            if x
        )
    ]
    queries += [str(t) for t in topics or [] if t]
    for query in queries:
        for law, quote in _search(rec, query, 6):
            if law.id in cited or len(output["related"]) >= 3:
                continue
            cited.add(law.id)
            claim = rec.clause(f"rel{len(output['related'])}", quote, law.id)
            output["related"].append(
                {"query": query, "claim": claim["id"], "evidenceId": law.id, "label": law.label}
            )
    if "needs_information" in decided:
        status = "needs_information"
    elif any(s != "met" for s in decided):
        status = "conditional"
    else:
        status = "calculated"
    return rec.result(status, output)


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
        rec.check(
            "permit", "met", "허가 품목에 이 폐기물의 품목과 재질이 있습니다.", None, evidence
        )
        output["reason"] = "허가 품목이 맞습니다."
    else:
        rec.check(
            "permit", "unmet", "허가 품목에 이 폐기물의 품목·재질이 없습니다.", None, evidence
        )
        output["reason"] = "허가 품목이 맞지 않아 대화를 열지 않습니다."
    return rec.result("calculated", output)
