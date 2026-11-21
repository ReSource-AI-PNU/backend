"""기준값 키 → 후보 근거 ID. 앞에 있는 후보가 우선이다.

후보 순서는 `prefer(key, ref_id)`로 끼워 넣은 것, defaults 파일의 `refs`에 적은 기준값(ref 근거),
팀 가정값(assumption)이다. 후보가 하나도 없거나 볼 수 없으면 None을 돌려주고, 도구는
"확인 필요"로 둔다. ref 근거는 근거 시드(evidence/seeds)를 레지스트리에 올려야 잡힌다.
"""

from __future__ import annotations

from app.v3.evidence.acceptance import usable_basis
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.judge import defaults

_PREFERRED: dict[str, list[str]] = {}


def keys() -> list[str]:
    doc = defaults.load()
    return sorted(
        {item["key"] for item in doc["items"]} | set(doc.get("refs") or {}) | set(_PREFERRED)
    )


def prefer(key: str, evidence_id: str) -> None:
    """key의 후보 맨 앞에 근거 ID를 넣는다 (예: 가격조사 ref 근거)."""
    current = _PREFERRED.setdefault(key, [])
    if evidence_id in current:
        current.remove(evidence_id)
    current.insert(0, evidence_id)


def reset() -> None:
    _PREFERRED.clear()


def candidates(key: str) -> list[str]:
    refs = (defaults.load().get("refs") or {}).get(key) or []
    return list(dict.fromkeys([*_PREFERRED.get(key, []), *refs, defaults.default_id(key)]))


def resolve(registry, org_id: str | None, key: str) -> Evidence | None:
    for evidence_id in candidates(key):
        try:
            evidence = registry.get(evidence_id, org_id)
            if usable_basis(evidence):
                return evidence
        except (EvidenceNotFound, EvidenceForbidden):
            continue
    return None


REFERENCE_PRICE_PREFIX = "reference.price."
REFERENCE_PRICE_NOTE = (
    "가격조사 참고 시세입니다. 폐플라스틱은 중간가공 후 판매가격이므로 가공 전 폐기물 매입가와 다릅니다. "
    "재질·형태·지역·시점·품질·물량·운반비 조건을 별도 확인해야 하며 손익 계산에는 쓰지 않았습니다."
)


def reference_prices(registry, org_id: str | None, materials=None) -> list[dict]:
    """참고 시세(`reference.price.<재질>.<형태>`). 화면에 보여 주기만 하고 손익 계산에는 쓰지 않는다.

    materials를 주면 그 재질만 돌려준다. 근거가 레지스트리에 없으면 그 항목은 빠진다.
    """
    wanted = None if materials is None else {str(m) for m in materials}
    out = []
    for key in keys():
        if not key.startswith(REFERENCE_PRICE_PREFIX):
            continue
        material, _, form = key[len(REFERENCE_PRICE_PREFIX) :].partition(".")
        if wanted is not None and material not in wanted:
            continue
        evidence = resolve(registry, org_id, key)
        if evidence is None or evidence.kind != "ref":
            continue
        out.append({"key": key, "material": material, "form": form, "evidence": evidence})
    return out
