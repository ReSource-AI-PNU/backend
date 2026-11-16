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
            meta={"key": item["key"], "registeredOn": today or doc["asOf"]},
        )
        out.append(registry.put(evidence).id)
    return out
