"""근거 검색과 조회. 기본은 로컬 레지스트리에 대한 결정적 어휘 검색이다.

use_backend로 Azure AI Search(search_azure.py)를 연결하면 순위만 거기서 받는다. 돌려주는 근거의
내용은 언제나 레지스트리에서 다시 읽은 것이고, 조직이 볼 수 없는 근거는 걸러 낸다. 백엔드가
실패하면 로컬 검색으로 답한다. 판단 도구는 이 검색을 쓰지 않으므로 도구의 결과는 그대로다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from app.v3.evidence.model import PUBLIC, Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, EvidenceRegistry

logger = logging.getLogger(__name__)

# backend(query, org_id=..., kinds=..., top=...) -> [{"evidence": {"id", ...}, "score"}]
# AzureSearchClient.search와 같은 서명이다.
SearchBackend = Callable[..., list[dict[str, Any]]]
LOCAL = "local"
_backend: SearchBackend | None = None
_backend_name = LOCAL

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
    kinds = None if kinds is None else list(kinds)
    if limit > 0 and _backend is not None and query.strip():
        ranked_elsewhere = _from_backend(registry, query, kinds, org_id, limit)
        if ranked_elsewhere is not None:
            return ranked_elsewhere
    items = registry.list(kinds=kinds, org_id=org_id)
    if limit <= 0:
        return []
    if not query.strip():
        return [{"evidence": shown(e, org_id), "score": 0.0} for e in items[:limit]]
    scored = [(score(e, query), e) for e in items]
    ranked = sorted((pair for pair in scored if pair[0] > 0), key=lambda p: (-p[0], p[1].id))
    return [{"evidence": shown(e, org_id), "score": s} for s, e in ranked[:limit]]


def use_backend(backend: SearchBackend | None, name: str = "azure") -> None:
    """검색 순위를 낼 백엔드를 정한다. None이면 로컬 검색으로 돌아간다."""
    global _backend, _backend_name
    _backend, _backend_name = backend, (name if backend is not None else LOCAL)


def backend_name() -> str:
    return _backend_name


def _from_backend(
    registry: EvidenceRegistry,
    query: str,
    kinds: list[str] | None,
    org_id: str | None,
    limit: int,
) -> list[dict[str, Any]] | None:
    """백엔드의 순위대로 레지스트리에서 다시 읽어 돌려준다. 백엔드가 실패하면 None."""
    try:
        hits = _backend(query, org_id=org_id, kinds=kinds, top=limit)  # type: ignore[misc]
    except Exception as error:  # noqa: BLE001 - 어떤 실패든 로컬 검색으로 답한다
        logger.warning("검색 백엔드(%s) 실패, 로컬 검색으로 답한다: %s", _backend_name, error)
        return None
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits or []:
        found = hit.get("evidence") if isinstance(hit, dict) else None
        evidence_id = found.get("id") if isinstance(found, dict) else None
        if not isinstance(evidence_id, str) or evidence_id in seen:
            continue
        seen.add(evidence_id)
        try:
            current = registry.get(evidence_id, org_id)
        except (EvidenceNotFound, EvidenceForbidden):
            continue  # 색인에만 남은 근거나 볼 수 없는 근거는 내보내지 않는다
        if kinds is not None and current.kind not in kinds:
            continue
        results.append(
            {"evidence": shown(current, org_id), "score": float(hit.get("score") or 0.0)}
        )
    return results[:limit]


def shown(evidence: Evidence, org_id: str | None) -> dict[str, Any]:
    """org_id에게 보여 줄 근거. 누구와 나눴는지(access)는 보는 조직과 근거를 가진 조직만 남긴다.

    계산 근거의 입력 원본(meta.inputs)은 계산한 조직(inputs.orgId)이 아니면 {"hidden": True}로 가린다.

    업체의 매입 기준 근거는 그 업체와 대화한 배출 기업 모두에게 열려 있다. 목록을 그대로 내면
    한 배출 기업이 같은 업체와 거래하는 다른 배출 기업을 알게 된다. org_id가 None이면(시스템 내부)
    그대로 준다.
    """
    data = evidence.to_dict()
    if org_id is None:
        return data
    inputs = data["meta"].get("inputs")
    if isinstance(inputs, dict) and "orgId" in inputs and inputs.get("orgId") != org_id:
        # 계산 근거의 입력 원본은 계산한 조직만 본다. 나누는 근거에는 처음부터 상대가 볼 수 있는
        # 입력만 싣지만(judge.common.shareable), 그 전에 저장된 근거도 여기서 가린다
        data["meta"] = {**data["meta"], "inputs": {"hidden": True}}
    if PUBLIC in evidence.access:
        data["access"] = [PUBLIC]
        return data
    owner = str((evidence.meta or {}).get("ownerOrgId") or "")
    data["access"] = [o for o in evidence.access if o in (org_id, owner)]
    return data


def get_evidence(
    registry: EvidenceRegistry, evidence_id: str, org_id: str | None
) -> dict[str, Any]:
    """없으면 EvidenceNotFound, 볼 수 없으면 EvidenceForbidden."""
    return shown(registry.get(evidence_id, org_id), org_id)


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
            chain.append({**shown(child, org_id), "depth": depth})
            walk(child, depth + 1)

    walk(root, 1)
    return chain


def _inputs(evidence: Evidence) -> list[str]:
    raw = evidence.meta.get("inputEvidence") or []
    return [str(item) for item in raw] if isinstance(raw, (list, tuple)) else []
