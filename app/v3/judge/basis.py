"""기준값 키 → 후보 근거 ID. 앞에 있는 후보가 우선이다.

지금은 팀 가정값(assumption)만 있다. 기준월 스냅숏(ref 근거)이 생기면 `prefer(key, ref_id)`로
앞에 끼워 넣는다. 후보가 하나도 없거나 볼 수 없으면 None을 돌려주고, 도구는 "확인 필요"로 둔다.
"""

from __future__ import annotations

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.judge import defaults

_PREFERRED: dict[str, list[str]] = {}


def keys() -> list[str]:
    return sorted({item["key"] for item in defaults.load()["items"]} | set(_PREFERRED))


def prefer(key: str, evidence_id: str) -> None:
    """key의 후보 맨 앞에 근거 ID를 넣는다 (예: 가격조사 ref 근거)."""
    current = _PREFERRED.setdefault(key, [])
    if evidence_id in current:
        current.remove(evidence_id)
    current.insert(0, evidence_id)


def reset() -> None:
    _PREFERRED.clear()


def candidates(key: str) -> list[str]:
    return [*_PREFERRED.get(key, []), defaults.default_id(key)]


def resolve(registry, org_id: str | None, key: str) -> Evidence | None:
    for evidence_id in candidates(key):
        try:
            return registry.get(evidence_id, org_id)
        except (EvidenceNotFound, EvidenceForbidden):
            continue
    return None
