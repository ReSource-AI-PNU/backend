"""근거 레지스트리 인터페이스와 메모리 구현. PostgreSQL 구현은 app/v3/sql.py에 둔다."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
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

    def list(
        self, *, kinds: Iterable[str] | None = None, org_id: str | None = None
    ) -> list[Evidence]:
        wanted = set(kinds) if kinds is not None else None
        return [
            item
            for _, item in sorted(self._items.items())
            if (wanted is None or item.kind in wanted) and item.visible_to(org_id)
        ]
