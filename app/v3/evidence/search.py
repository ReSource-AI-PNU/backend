"""근거 검색과 조회. 로컬 레지스트리에 대한 결정적 어휘 검색이다. Azure AI Search는 search_azure.py."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, EvidenceRegistry

_TOKEN = re.compile(r"[0-9a-z가-힣]+")
_HANGUL_RUN = re.compile(r"[가-힣]+")
# 필드별 가중치. 라벨에 맞으면 원문에 맞는 것보다 높게 친다.
_FIELD_WEIGHTS = (("label", 3.0), ("id", 2.0), ("excerpt", 1.0), ("source", 1.0))


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def bigrams(text: str) -> set[str]:
    """한글 덩어리의 글자 2-gram. 조사가 붙어도 겹치는 부분으로 찾는다."""
    grams: set[str] = set()
    for run in _HANGUL_RUN.findall(text):
        grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return grams


def score(evidence: Evidence, query: str) -> float:
    tokens = list(dict.fromkeys(tokenize(query)))
    grams = bigrams(query)
    if not tokens and not grams:
        return 0.0
    total = 0.0
    for name, weight in _FIELD_WEIGHTS:
        value = str(getattr(evidence, name) or "").lower()
        if not value:
            continue
        hits = sum(1 for token in tokens if token in value)
        if tokens:
            total += weight * hits / len(tokens)
        if grams:
            total += 0.5 * weight * len(grams & bigrams(value)) / len(grams)
    return round(total, 6)


def search_evidence(
    registry: EvidenceRegistry,
    query: str,
    *,
    kinds: Iterable[str] | None = None,
    org_id: str | None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """org_id가 볼 수 있는 근거만 점수 순으로 돌려준다. 점수가 같으면 ID 순서다.

    질의가 비어 있으면 볼 수 있는 근거를 ID 순서로 limit개 돌려준다(점수 0).
    """
    items = registry.list(kinds=kinds, org_id=org_id)
    if limit <= 0:
        return []
    if not query.strip():
        return [{"evidence": e.to_dict(), "score": 0.0} for e in items[:limit]]
    scored = [(score(e, query), e) for e in items]
    ranked = sorted((pair for pair in scored if pair[0] > 0), key=lambda p: (-p[0], p[1].id))
    return [{"evidence": e.to_dict(), "score": s} for s, e in ranked[:limit]]


def get_evidence(
    registry: EvidenceRegistry, evidence_id: str, org_id: str | None
) -> dict[str, Any]:
    """없으면 EvidenceNotFound, 볼 수 없으면 EvidenceForbidden."""
    return registry.get(evidence_id, org_id).to_dict()


def evidence_chain(
    registry: EvidenceRegistry, evidence_id: str, org_id: str | None
) -> list[dict[str, Any]]:
    """계산 사슬. meta.inputEvidence를 따라가며 입력 근거를 깊이 우선 순서로 돌려준다.

    시작 근거 자신은 넣지 않는다. 각 항목에는 depth(직접 입력이 1)가 붙는다.
    볼 수 없는 근거는 {"id", "hidden": True}, 없는 근거는 {"id", "missing": True}로만 낸다.
    """
    root = registry.get(evidence_id, org_id)
    seen = {root.id}
    chain: list[dict[str, Any]] = []

    def walk(parent: Evidence, depth: int) -> None:
        for child_id in _inputs(parent):
            if child_id in seen:
                continue
            seen.add(child_id)
            try:
                child = registry.get(child_id, org_id)
            except EvidenceForbidden:
                chain.append({"id": child_id, "hidden": True, "depth": depth})
                continue
            except EvidenceNotFound:
                chain.append({"id": child_id, "missing": True, "depth": depth})
                continue
            chain.append({**child.to_dict(), "depth": depth})
            walk(child, depth + 1)

    walk(root, 1)
    return chain


def _inputs(evidence: Evidence) -> list[str]:
    raw = evidence.meta.get("inputEvidence") or []
    return [str(item) for item in raw] if isinstance(raw, (list, tuple)) else []
