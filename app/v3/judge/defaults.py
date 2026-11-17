"""판단 도구가 쓰는 상수. 모두 assumption 근거로 등록하고, 코드에는 숫자를 두지 않는다."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence

PATH = Path(__file__).with_name("defaults.v1.json")


@lru_cache
def load() -> dict[str, Any]:
    return json.loads(PATH.read_text(encoding="utf-8"))


def default_id(key: str) -> str:
    return ids.assumption_id(key, load()["version"])


def sensitivity_scenarios() -> list[dict[str, str]]:
    return list(load()["sensitivity"])


def _meta(item: dict[str, Any], registered_on: str) -> dict[str, Any]:
    """화면 글(excerpt, source)에는 파일 경로를 두지 않고, 어디서 온 값인지는 여기에 남긴다."""
    meta = {"key": item["key"], "registeredOn": registered_on, "file": f"app/v3/judge/{PATH.name}"}
    if item.get("origin"):
        meta["origin"] = item["origin"]  # 옮겨 온 값이면 그 원래 자리
    if item.get("internalNote"):
        meta["internalNote"] = item["internalNote"]  # 팀 기록(화면 글인 excerpt에는 두지 않는다)
    return meta


def register_defaults(registry, today: str | None = None) -> list[str]:
    """기본 가정값을 레지스트리에 넣고 근거 ID 목록을 돌려준다. 여러 번 불러도 결과가 같다."""
    doc = load()
    out = []
    for item in doc["items"]:
        evidence = Evidence(
            id=default_id(item["key"]),
            kind="assumption",
            value=item["value"],
            unit=item["unit"],
            label=item["label"],
            excerpt=item["excerpt"],
            source=doc["source"],
            as_of=doc["asOf"],
            meta=_meta(item, today or doc["asOf"]),
        )
        out.append(registry.put(evidence).id)
    return out
