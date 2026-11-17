"""가정값 근거의 설명(excerpt)은 화면에 그대로 보인다. 팀 기록("…옮겼다. 부호를 뒤집어 …적었다")이 아니라
합니다체 안내여야 하고, 팀 기록은 meta.internalNote에 둔다."""

from __future__ import annotations

import re

from app.v3.evidence.registry import InMemoryRegistry
from app.v3.intake import grade_model
from app.v3.judge.defaults import register_defaults


def test_assumption_descriptions_are_polite_and_keep_team_notes_apart():
    registry = InMemoryRegistry()
    found = [registry.get(i) for i in register_defaults(registry, "2026-11-18")]
    found.append(grade_model.thresholds_evidence())
    assert len(found) > 5
    for evidence in found:
        text = evidence.excerpt
        if evidence.id.startswith("assumption:등급"):
            text = text.split(" ABS:")[0]  # 뒤는 기준값 표(ABS: A 1%·0.3%, …)다
        for sentence in re.split(r"(?<=\.)\s+", text.strip()):
            bare = re.sub(r"\([^)]*\)", "", sentence).strip()
            assert bare.endswith("니다."), (evidence.id, sentence)
        assert not re.search(r"옮겼다|적었다|아니다|이다\.", evidence.excerpt), evidence.id
    moved = [e for e in found if e.meta.get("origin") and e.id.startswith("assumption:current")]
    assert moved and all(e.meta.get("internalNote") for e in moved)
