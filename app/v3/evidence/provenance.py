"""Evidence classification, scope compatibility and immutable calculation inputs."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import replace

from app.v3 import ids

CATEGORIES = {
    "measured": "① 사용자 실측·증빙",
    "official": "② 공식 통계·공식 계수",
    "literature": "③ 적용 조건이 있는 연구 문헌",
    "assumption": "④ 계산을 위한 가정",
    "policy": "⑤ 서비스 운영 정책",
    "hold": "⑥ 근거 부족으로 산정 보류",
}


def compatible(*factors, metric: str) -> bool:
    """Same units alone do not establish compatibility. Missing scope fails closed."""
    if not factors or any(f is None for f in factors):
        return False
    if any(
        isinstance(f.value, bool)
        or not isinstance(f.value, (int, float))
        or not math.isfinite(f.value)
        for f in factors
    ):
        return False
    keys = (
        "datasetVersion",
        "functionalUnit",
        "systemBoundary",
        "country",
        "material",
        "energyType",
    )
    first = factors[0].meta
    if any(not first.get(k) for k in keys[:-1]) or (
        metric == "energy" and not first.get("energyType")
    ):
        return False
    return all(
        f.meta.get("metric") == metric
        and f.meta.get("verificationStatus") == "original_checked"
        and all(f.meta.get(k) == first.get(k) for k in keys)
        for f in factors
    )


def freeze_inputs(registry, evidence_ids, viewers):
    """Freeze the readable dependency graph, retaining every node's original ACL."""
    memo = {}

    def freeze(eid):
        if eid in memo:
            return memo[eid]
        item = registry.get(eid)
        if not all(item.visible_to(viewer) for viewer in viewers):
            return None
        if item.meta.get("frozen"):
            return eid
        memo[eid] = None
        children = [freeze(child) for child in item.meta.get("inputEvidence", [])]
        meta = {
            **deepcopy(item.meta),
            "frozen": True,
            "originalId": item.id,
            "originalVersion": item.version,
            "inputEvidence": [child for child in children if child],
        }
        frozen = replace(item, id=f"{item.kind}:snapshot:{ids.digest(item.to_dict())}", meta=meta)
        # Content-addressed immutable node: do not touch an earlier calculation.
        if not registry.exists(frozen.id):
            registry.put(frozen)
        memo[eid] = frozen.id
        return frozen.id

    return [fid for eid in evidence_ids if (fid := freeze(eid))]
