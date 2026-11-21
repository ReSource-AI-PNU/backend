"""근거 레지스트리 인터페이스와 메모리 구현. PostgreSQL 구현은 app/v3/sql.py에 둔다."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from app.v3.evidence.model import Evidence


class EvidenceNotFound(KeyError):
    """그런 ID의 근거가 없다."""


class EvidenceForbidden(PermissionError):
    """근거는 있지만 이 조직이 볼 수 없다."""


class EvidenceRegistry(Protocol):
    def put(self, evidence: Evidence) -> Evidence:
        """저장한다. 같은 ID가 있고 내용이 다르면 version을 1 올려 덮어쓴다. 저장된 레코드를 돌려준다."""

    def get(self, evidence_id: str, org_id: str | None = None) -> Evidence:
        """없으면 EvidenceNotFound, 볼 수 없으면 EvidenceForbidden."""

    def exists(self, evidence_id: str) -> bool: ...

    def list(
        self, *, kinds: Iterable[str] | None = None, org_id: str | None = None
    ) -> list[Evidence]:
        """org_id가 볼 수 있는 근거만. ID 순서로 정렬한다."""

    def get_many(
        self, evidence_ids: Iterable[str], org_id: str | None = None
    ) -> dict[str, Evidence | None]:
        """여러 근거를 한 번에. 있는 ID만 키로 넣고, 볼 수 없는 것은 값을 None으로 둔다."""

    def list_feedback(self, feedback_type: str) -> list[Evidence]:
        """meta["feedback"]["type"]이 feedback_type인 근거 전부(시스템 조회). ID 순서로 정렬한다."""


class InMemoryRegistry:
    def __init__(self, items: Iterable[Evidence] = ()) -> None:
        self._items: dict[str, Evidence] = {}
        for item in items:
            self.put(item)

    def put(self, evidence: Evidence) -> Evidence:
        old = self._items.get(evidence.id)
        if old is not None:
            if replace(old, version=evidence.version) == evidence:
                return old
            evidence = replace(evidence, version=old.version + 1)
        self._items[evidence.id] = evidence
        return evidence

    def get(self, evidence_id: str, org_id: str | None = None) -> Evidence:
        item = self._items.get(evidence_id)
        if item is None:
            raise EvidenceNotFound(evidence_id)
        if not item.visible_to(org_id):
            raise EvidenceForbidden(evidence_id)
        return item

    def exists(self, evidence_id: str) -> bool:
        return evidence_id in self._items

    def clear(self) -> None:
        """모두 지운다. 시연 데이터를 처음 상태로 되돌릴 때 쓴다."""
        self._items.clear()

    def delete(self, evidence_ids: Iterable[str]) -> int:
        """주어진 근거를 지운다. 지운 수를 준다. bff 모드의 시연 되돌리기가 시연 조직의 근거만 지울 때 쓴다."""
        removed = 0
        for evidence_id in set(evidence_ids):
            removed += self._items.pop(evidence_id, None) is not None
        return removed

    def list(
        self, *, kinds: Iterable[str] | None = None, org_id: str | None = None
    ) -> list[Evidence]:
        wanted = set(kinds) if kinds is not None else None
        return [
            item
            for _, item in sorted(self._items.items())
            if (wanted is None or item.kind in wanted) and item.visible_to(org_id)
        ]

    def get_many(
        self, evidence_ids: Iterable[str], org_id: str | None = None
    ) -> dict[str, Evidence | None]:
        found: dict[str, Evidence | None] = {}
        for evidence_id in set(evidence_ids):
            item = self._items.get(evidence_id)
            if item is not None:
                found[evidence_id] = item if item.visible_to(org_id) else None
        return found

    def list_feedback(self, feedback_type: str) -> list[Evidence]:
        return [item for _, item in sorted(self._items.items()) if is_feedback(item, feedback_type)]


def is_feedback(evidence: Evidence, feedback_type: str) -> bool:
    """meta["feedback"]["type"]이 feedback_type인 근거인지(교정·실제 결과·모델 실행 기록)."""
    record = evidence.meta.get("feedback")
    return isinstance(record, dict) and record.get("type") == feedback_type


class JsonFileRegistry(InMemoryRegistry):
    """근거를 JSON 파일 하나에 통째로 쓴다. 로컬 시연과 개발용이다(동시 쓰기를 막지 않는다).

    파일이 이미 있으면 읽어서 이어 간다(판 번호도 그대로다). JsonFileStore와 짝으로 써서
    서버를 다시 띄워도 이전 진단의 메시지와 문서가 인용한 근거를 그대로 확인할 수 있게 한다.
    바뀐 것이 있을 때만 쓴다.
    """

    def __init__(self, path: str | os.PathLike[str], items: Iterable[Evidence] = ()) -> None:
        self.path = Path(path)
        self._ready = False
        super().__init__()
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in loaded.get("evidence") or []:
                item = Evidence.from_dict(raw)
                self._items[item.id] = item
        self._ready = True
        for item in items:
            self.put(item)

    @staticmethod
    def _plain(evidence: Evidence) -> Evidence:
        """meta를 JSON으로 한 번 돌린다. 파일에서 다시 읽은 것과 같은 모양이어야 판이 헛돌지 않는다."""
        meta = json.loads(json.dumps(evidence.meta, ensure_ascii=False, default=str))
        return replace(evidence, meta=meta)

    def put(self, evidence: Evidence) -> Evidence:
        old = self._items.get(evidence.id)
        saved = super().put(self._plain(evidence))
        if saved is not old:
            self._write()
        return saved

    def clear(self) -> None:
        super().clear()
        self._write()

    def delete(self, evidence_ids: Iterable[str]) -> int:
        removed = super().delete(evidence_ids)
        if removed:
            self._write()
        return removed

    def _write(self) -> None:
        if not self._ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        payload = {"evidence": [item.to_dict() for _, item in sorted(self._items.items())]}
        temp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(temp, self.path)
