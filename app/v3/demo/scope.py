"""체험하기(bff 모드의 X-Demo-View: 1): 시연 조직만 보이는 읽기 전용 앱.

api.py는 체험하기 요청을 GET일 때만 받고, 서비스 함수를 demo_app(app)으로 부른다. 그 앱의 저장소와
근거 레지스트리는 시연 조직(demo: true)의 자료만 보여 주고 쓰기를 모두 거절한다. 그래서 실제 조직은
어떤 경로로도 보이지 않는다.

- 조직, 흐름, 업체 프로필: 시연 조직의 것만.
- 진단: 배출 기업이 시연 조직인 것만. 스레드는 두 당사자가 모두 시연 조직인 것만. 실제 배출 기업이
  시연 업체와 나눈 대화는 시연 업체로 보아도 보이지 않는다.
- 근거: 공개 근거, 볼 수 있는 조직이 모두 시연 조직인 근거, 시연 조직이 낸 문서·입력 근거(doc, human)만.
  시연 업체의 허가증·매입 기준은 실제 배출 기업과 대화가 열리면 그 기업에도 열리는데, 그런 근거도
  access에서 실제 조직을 지우고 보여 준다. 실제 조직이 가진 근거나 실제 조직이 섞인 계산 근거는 감춘다.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from app.v3.a2a.policy import evidence_owner
from app.v3.evidence.model import PUBLIC, Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, is_feedback
from app.v3.service import ServiceError
from app.v3.store import NotFound

# 시연 조직이 낸 고정 자료. 실제 조직에 열려도 내용은 시연 데이터 그대로다
SHOWN_OUTWARD_KINDS = ("doc", "human")


def read_only() -> ServiceError:
    """체험하기 앱에 쓰려 했다."""
    return ServiceError("DEMO_READ_ONLY", "체험하기에서는 보기만 할 수 있습니다.", 403)


def demo_ids(store: Any) -> frozenset[str]:
    return frozenset(org["id"] for org in store.list_orgs() if org.get("demo") is True)


class DemoStore:
    """시연 조직의 자료만 보여 주는 읽기 전용 저장소."""

    def __init__(self, inner: Any, demo: frozenset[str]) -> None:
        self._store = inner
        self._demo = demo

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("put_", "add_", "set_", "block_")) or name in (
            "reset",
            "purge_orgs",
            "next_id",
            "claim_run",
            "touch_run",
            "release_run",
        ):
            raise read_only()
        return getattr(self._store, name)

    def _demo_session(self, session_id: str) -> bool:
        try:
            return self._store.get_session(session_id).get("emitterOrgId") in self._demo
        except NotFound:
            return False

    def _demo_thread(self, thread: dict[str, Any]) -> bool:
        return thread.get("emitterOrgId") in self._demo and thread.get("vendorOrgId") in self._demo

    # ── 조직, 흐름, 업체 ───────────────────────────────────────────────
    def get_org(self, org_id: str) -> dict[str, Any]:
        if org_id not in self._demo:
            raise NotFound(f"orgs: {org_id}")
        return self._store.get_org(org_id)

    def list_orgs(self, role: str | None = None) -> list[dict[str, Any]]:
        return [org for org in self._store.list_orgs(role) if org["id"] in self._demo]

    def get_stream(self, stream_id: str, org_id: str | None = None) -> dict[str, Any]:
        stream = self._store.get_stream(stream_id, org_id)
        if stream.get("orgId") not in self._demo:
            raise NotFound(f"streams: {stream_id}")
        return stream

    def list_streams(self, org_id: str) -> list[dict[str, Any]]:
        return self._store.list_streams(org_id) if org_id in self._demo else []

    def streams_visible_to(self, org_id: str) -> list[dict[str, Any]]:
        if org_id not in self._demo:
            return []
        return [s for s in self._store.streams_visible_to(org_id) if s.get("orgId") in self._demo]

    def get_vendor_profile(self, org_id: str) -> dict[str, Any]:
        if org_id not in self._demo:
            raise NotFound(f"vendors: {org_id}")
        return self._store.get_vendor_profile(org_id)

    def list_vendor_profiles(self) -> list[dict[str, Any]]:
        return [p for p in self._store.list_vendor_profiles() if p.get("orgId") in self._demo]

    # ── 보관장 3D 스캔 ────────────────────────────────────────────────
    def get_scan(self, scan_id: str) -> dict[str, Any]:
        scan = self._store.get_scan(scan_id)
        if scan.get("orgId") not in self._demo:
            raise NotFound(f"scans: {scan_id}")
        return scan

    def list_scans(self, org_id: str) -> list[dict[str, Any]]:
        return self._store.list_scans(org_id) if org_id in self._demo else []

    def get_scan_points(self, scan_id: str) -> dict[str, Any]:
        points = self._store.get_scan_points(scan_id)
        if points.get("orgId") not in self._demo:
            raise NotFound(f"scan_points: {scan_id}")
        return points

    # ── 진단 ──────────────────────────────────────────────────────────
    def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._store.get_session(session_id)
        if session.get("emitterOrgId") not in self._demo:
            raise NotFound(f"sessions: {session_id}")
        return session

    def list_sessions(self, org_id: str | None = None) -> list[dict[str, Any]]:
        return [s for s in self._store.list_sessions(org_id) if s.get("emitterOrgId") in self._demo]

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        thread = self._store.get_thread(thread_id)
        if not self._demo_thread(thread):
            raise NotFound(f"threads: {thread_id}")
        return thread

    def list_threads(
        self, session_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [t for t in self._store.list_threads(session_id, org_id) if self._demo_thread(t)]

    def list_questions(self, session_id: str) -> list[dict[str, Any]]:
        return self._store.list_questions(session_id) if self._demo_session(session_id) else []

    def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return self._store.list_approvals(session_id) if self._demo_session(session_id) else []

    def list_documents(self, session_id: str) -> list[dict[str, Any]]:
        return self._store.list_documents(session_id) if self._demo_session(session_id) else []

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        return self._store.list_events(session_id) if self._demo_session(session_id) else []

    def list_activity(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        if not self._demo_session(session_id):
            return []
        return self._store.list_activity(session_id, after)

    def get_run(self, session_id: str) -> dict[str, Any] | None:
        return self._store.get_run(session_id) if self._demo_session(session_id) else None


class DemoRegistry:
    """시연 조직의 근거만 보여 주는 읽기 전용 근거 레지스트리."""

    def __init__(self, inner: Any, demo: frozenset[str]) -> None:
        self._registry = inner
        self._demo = demo

    def __getattr__(self, name: str) -> Any:
        if name in ("put", "clear", "delete"):
            raise read_only()
        return getattr(self._registry, name)

    def _shown(self, evidence: Evidence) -> bool:
        if PUBLIC in evidence.access:
            return True
        if all(org in self._demo for org in evidence.access):
            return bool(evidence.access) or evidence_owner(evidence) in self._demo
        return evidence.kind in SHOWN_OUTWARD_KINDS and evidence_owner(evidence) in self._demo

    def _trim(self, evidence: Evidence) -> Evidence:
        if PUBLIC in evidence.access:
            return evidence
        kept = tuple(org for org in evidence.access if org in self._demo)
        return evidence if kept == evidence.access else replace(evidence, access=kept)

    def get(self, evidence_id: str, org_id: str | None = None) -> Evidence:
        evidence = self._registry.get(evidence_id, org_id)
        if not self._shown(evidence):
            raise EvidenceForbidden(evidence_id)
        return self._trim(evidence)

    def exists(self, evidence_id: str) -> bool:
        if not self._registry.exists(evidence_id):
            return False
        return self._shown(self._registry.get(evidence_id, None))

    def list(
        self, *, kinds: Iterable[str] | None = None, org_id: str | None = None
    ) -> list[Evidence]:
        found = self._registry.list(kinds=kinds, org_id=org_id)
        return [self._trim(e) for e in found if self._shown(e)]

    def get_many(
        self, evidence_ids: Iterable[str], org_id: str | None = None
    ) -> dict[str, Evidence | None]:
        get_many = getattr(self._registry, "get_many", None)
        if get_many is None:  # 한 번에 읽는 기능이 없는 레지스트리: get과 같은 규칙으로 하나씩
            found: dict[str, Evidence | None] = {}
            for evidence_id in set(evidence_ids):
                try:
                    found[evidence_id] = self.get(evidence_id, org_id)
                except EvidenceForbidden:
                    found[evidence_id] = None
                except EvidenceNotFound:
                    pass
            return found
        return {
            evidence_id: self._trim(e) if e is not None and self._shown(e) else None
            for evidence_id, e in get_many(evidence_ids, org_id).items()
        }

    def list_feedback(self, feedback_type: str) -> list[Evidence]:
        listed = getattr(self._registry, "list_feedback", None)
        if listed is None:
            return [e for e in self.list(org_id=None) if is_feedback(e, feedback_type)]
        return [self._trim(e) for e in listed(feedback_type) if self._shown(e)]


def demo_app(app: Any) -> Any:
    """app과 같은 설정으로, 시연 조직만 보이는 읽기 전용 저장소·레지스트리를 쓰는 앱(얕은 복사)."""
    demo = demo_ids(app.store)
    scoped = copy.copy(app)
    scoped.store = DemoStore(app.store, demo)
    scoped.registry = DemoRegistry(app.registry, demo)
    scoped.read_only = True
    return scoped
