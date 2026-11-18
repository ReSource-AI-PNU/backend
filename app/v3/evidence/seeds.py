"""근거 시드 적재. seeds/*.json은 Evidence.to_dict() 모양의 객체 배열이다."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceRegistry

SEED_DIR = Path(__file__).parent / "seeds"
# 법령 말뭉치(폐기물관리법·같은 법 시행령·시행규칙·순환경제사회 전환 촉진법 348개 조, law.go.kr 원문).
# 검색 평가(L2) 시험셋과 같은 파일을 쓴다. 파일을 하나만 두어 ③ 규정 검토와 평가가 같은 조문을 본다.
LAW_CORPUS = Path(__file__).resolve().parent.parent / "eval" / "sets" / "corpus" / "laws_extra.json"
_REQUIRED = ("id", "kind", "asOf")


def load_seed_file(
    registry: EvidenceRegistry, path: str | Path, *, meta: dict | None = None
) -> int:
    """파일 하나를 읽어 레지스트리에 넣고 넣은 개수를 돌려준다.

    형식 오류는 파일 이름과 몇 번째 항목인지 밝혀 ValueError로 낸다. 하나라도 틀리면 아무것도 넣지 않는다.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name}: JSON을 읽을 수 없다 ({error})") from error
    if not isinstance(data, list):
        # 형식 오류는 모두 ValueError로 통일한다
        raise ValueError(f"{path.name}: 최상위는 근거 객체의 배열이어야 한다")  # noqa: TRY004
    items: list[Evidence] = []
    seen: set[str] = set()
    for number, raw in enumerate(data, 1):
        where = f"{path.name}: {number}번째 항목"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}이 객체가 아니다")  # noqa: TRY004
        missing = [key for key in _REQUIRED if not raw.get(key)]
        if missing:
            raise ValueError(f"{where}에 필수 키가 없다: {', '.join(missing)}")
        try:
            evidence = Evidence.from_dict(raw)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{where}의 형식이 틀렸다 ({error})") from error
        if evidence.id in seen:
            raise ValueError(f"{where}의 ID가 파일 안에서 겹친다: {evidence.id}")
        seen.add(evidence.id)
        items.append(evidence)
    for evidence in items:
        if meta:
            evidence = replace(evidence, meta={**(evidence.meta or {}), **meta})
        registry.put(evidence)
    return len(items)


def load_seed_dir(registry: EvidenceRegistry, directory: str | Path | None = None) -> int:
    """폴더의 *.json을 이름 순서로 모두 넣는다. 폴더가 없으면 0.

    기본 폴더(SEED_DIR)를 읽을 때는 법령 말뭉치(LAW_CORPUS)도 함께 넣는다. 같은 ID가 겹치면
    레지스트리가 내용이 같은 것은 그대로 둔다.
    """
    folder = SEED_DIR if directory is None else Path(directory)
    if not folder.is_dir():
        return 0
    count = sum(load_seed_file(registry, path) for path in sorted(folder.glob("*.json")))
    if directory is None and LAW_CORPUS.exists():
        # 파일의 evalOnly 표시는 평가 전용이던 때의 것이다(시험셋 해시를 지키려 파일은 그대로 둔다)
        count += load_seed_file(registry, LAW_CORPUS, meta={"evalOnly": False})
    return count
