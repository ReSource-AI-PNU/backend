"""SQLAlchemy 저장: 근거 레지스트리와 저장소. SQLite와 PostgreSQL에서 같은 코드로 돈다.

주의: 이 파일은 작성한 환경에서 실행해 보지 못했다(sqlalchemy를 불러올 수 없었다).
tests/v3/test_sql.py가 CI에서 SQLite로 확인한다.

- 테이블은 app.platform.db의 Base에 더한다. 만들기는 migrations/versions/0004_v3.py가 한다.
  계정 테이블(v3_users, v3_memberships, v3_invites)은 0005_v3_accounts.py가 만든다(V3_AUTH=bff).
- JSON 타입만 쓴다. 조직 필터와 정렬은 파이썬에서 한다(DB마다 JSON 질의와 정렬 규칙이 달라서다).
  시연 규모에 맞춘 선택이고, 근거가 많아지면 access를 별도 테이블로 빼야 한다.
- 호출 하나가 트랜잭션 하나다. 읽은 값은 항상 깊은 복사이고, 바꾸려면 put_*으로 다시 저장한다.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from sqlalchemy import JSON, Integer, String, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.platform.db import Base
from app.v3.account_store import EmailTaken, iso, parse_iso, utc_now
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.store import SHARED_THREAD_STATUSES, NotFound, VersionConflict, system_clock


class EvidenceRow(Base):
    __tablename__ = "v3_evidence"
    id: Mapped[str] = mapped_column(String(300), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    as_of: Mapped[str] = mapped_column(String(10))
    valid_until: Mapped[str | None] = mapped_column(String(10), nullable=True)
    access: Mapped[list] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)


class RecordRow(Base):
    """종류(kind)와 id를 키로 한 JSON 레코드."""

    __tablename__ = "v3_records"
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class EventRow(Base):
    __tablename__ = "v3_events"
    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    payload: Mapped[dict] = mapped_column(JSON)


class ActivityRow(Base):
    """실시간 진행 기록(live.py). 기록 하나가 트랜잭션 하나라 쓰자마자 다른 요청이 읽는다."""

    __tablename__ = "v3_activity"
    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    payload: Mapped[dict] = mapped_column(JSON)


class UserRow(Base):
    __tablename__ = "v3_users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[str] = mapped_column(String(40))


class MembershipRow(Base):
    __tablename__ = "v3_memberships"
    user_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(100), primary_key=True, index=True)
    member_role: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[str] = mapped_column(String(40))


class InviteRow(Base):
    __tablename__ = "v3_invites"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(100), index=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40))
    used_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    used_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


V3_TABLES = (
    EvidenceRow.__table__,
    RecordRow.__table__,
    EventRow.__table__,
    ActivityRow.__table__,
)
ACCOUNT_TABLES = (UserRow.__table__, MembershipRow.__table__, InviteRow.__table__)


def create_tables(engine: Engine) -> None:
    """테스트와 로컬 SQLite용. 배포 환경에서는 alembic이 만든다."""
    Base.metadata.create_all(engine, tables=[*V3_TABLES, *ACCOUNT_TABLES])


@contextmanager
def _transaction(engine: Engine) -> Iterator[Session]:
    with Session(engine) as db:
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _plain(value: Any) -> Any:
    """JSON으로 한 번 돌린 값. DB에서 읽은 것과 같은 모양(튜플은 리스트)이 된다."""
    return json.loads(json.dumps(value, ensure_ascii=False))


# ── 근거 레지스트리 ──────────────────────────────────────────────────
def _to_evidence(row: EvidenceRow) -> Evidence:
    data = row.payload
    # Evidence.from_dict는 빈 access를 공개("*")로 바꾸므로 쓰지 않는다.
    # access는 열 값을 그대로 쓴다.
    return Evidence(
        id=row.id,
        kind=row.kind,
        excerpt=data.get("excerpt", ""),
        source=data.get("source", ""),
        as_of=row.as_of,
        value=data.get("value"),
        unit=data.get("unit"),
        label=data.get("label", ""),
        valid_until=row.valid_until,
        access=tuple(row.access or ()),
        version=int(row.version),
        meta=copy.deepcopy(data.get("meta") or {}),
    )


class SqlRegistry:
    """EvidenceRegistry 구현. InMemoryRegistry와 같은 규칙으로 버전을 올리고 조직을 거른다.

    meta는 JSON으로 저장되므로 돌려주는 레코드의 meta 안 튜플은 리스트가 된다.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, evidence: Evidence) -> Evidence:
        evidence = replace(evidence, meta=_plain(evidence.meta))
        with _transaction(self._engine) as db:
            row = db.get(EvidenceRow, evidence.id, with_for_update=True)
            if row is not None:
                old = _to_evidence(row)
                if replace(old, version=evidence.version) == evidence:
                    return old
                evidence = replace(evidence, version=old.version + 1)
            values = {
                "kind": evidence.kind,
                "as_of": evidence.as_of,
                "valid_until": evidence.valid_until,
                "access": [str(org) for org in evidence.access],
                "version": evidence.version,
                "payload": _plain(evidence.to_dict()),
            }
            if row is None:
                db.add(EvidenceRow(id=evidence.id, **values))
            else:
                for name, value in values.items():
                    setattr(row, name, value)
        return evidence

    def get(self, evidence_id: str, org_id: str | None = None) -> Evidence:
        with _transaction(self._engine) as db:
            row = db.get(EvidenceRow, evidence_id)
            item = _to_evidence(row) if row is not None else None
        if item is None:
            raise EvidenceNotFound(evidence_id)
        if not item.visible_to(org_id):
            raise EvidenceForbidden(evidence_id)
        return item

    def exists(self, evidence_id: str) -> bool:
        with _transaction(self._engine) as db:
            found = db.scalar(select(EvidenceRow.id).where(EvidenceRow.id == evidence_id))
        return found is not None

    def clear(self) -> None:
        """근거를 모두 지운다. 시연을 처음 상태로 되돌릴 때(service.reset_demo)만 부른다."""
        with _transaction(self._engine) as db:
            db.execute(delete(EvidenceRow))

    def delete(self, evidence_ids: Iterable[str]) -> int:
        """주어진 근거를 지운다. 지운 수를 준다. bff 모드의 시연 되돌리기가 시연 조직의 근거만 지울 때 쓴다."""
        wanted = sorted(set(evidence_ids))
        removed = 0
        with _transaction(self._engine) as db:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                result = db.execute(delete(EvidenceRow).where(EvidenceRow.id.in_(chunk)))
                removed += int(result.rowcount or 0)
        return removed

    def list(
        self, *, kinds: Iterable[str] | None = None, org_id: str | None = None
    ) -> list[Evidence]:
        query = select(EvidenceRow)
        if kinds is not None:
            query = query.where(EvidenceRow.kind.in_(sorted(set(kinds))))
        with _transaction(self._engine) as db:
            items = [_to_evidence(row) for row in db.scalars(query).all()]
        return [item for item in sorted(items, key=lambda e: e.id) if item.visible_to(org_id)]


# ── 저장소 ───────────────────────────────────────────────────────────
ORGS, STREAMS, VENDORS, SESSIONS, THREADS = (
    "orgs",
    "streams",
    "vendor_profiles",
    "sessions",
    "threads",
)
QUESTIONS, APPROVALS, DOCUMENTS, COUNTERS = "questions", "approvals", "documents", "counters"
RUNS = "runs"  # 진단마다 실행 자리 하나(id는 진단 ID)


class SqlStore:
    """Store 프로토콜 구현. InMemoryStore와 같은 동작을 한다.

    질문·승인·문서는 세션마다 레코드 하나에 목록으로 둔다(순서를 메모리 구현과 같게 지키려는 것).
    """

    def __init__(self, engine: Engine, clock: Callable[[], str] | None = None) -> None:
        self._engine = engine
        self._clock = clock or system_clock

    # ── 공통 ──────────────────────────────────────────────────────────
    def now(self) -> str:
        return self._clock()

    def next_id(self, prefix: str) -> str:
        with _transaction(self._engine) as db:
            row = self._row(db, COUNTERS, prefix, lock=True)
            value = int(row.payload.get("value", 0)) + 1 if row is not None else 1
            self._write(db, COUNTERS, prefix, {"value": value}, row)
        return f"{prefix}_{value}"

    def reset(self) -> None:
        """레코드와 실행 기록을 모두 지운다. 근거 레지스트리는 건드리지 않는다."""
        with _transaction(self._engine) as db:
            db.execute(delete(RecordRow))
            db.execute(delete(EventRow))
            db.execute(delete(ActivityRow))

    def purge_orgs(self, org_ids: Iterable[str]) -> list[str]:
        """org_ids의 조직, 흐름, 업체 프로필, 그 조직이 배출 기업인 진단을 지운다. 지운 스레드 ID를 준다.

        InMemoryStore.purge_orgs와 같다. ID 카운터는 그대로 둔다.
        """
        wanted = set(org_ids)
        with _transaction(self._engine) as db:
            sessions = {
                row.id
                for row in self._rows(db, SESSIONS)
                if row.payload.get("emitterOrgId") in wanted
            }
            threads = sorted(
                row.id
                for row in self._rows(db, THREADS)
                if row.payload.get("sessionId") in sessions
                or row.payload.get("emitterOrgId") in wanted
            )
            streams = {
                row.id for row in self._rows(db, STREAMS) if row.payload.get("orgId") in wanted
            }
            doomed = {
                ORGS: wanted,
                STREAMS: streams,
                VENDORS: wanted,
                SESSIONS: sessions,
                THREADS: set(threads),
                QUESTIONS: sessions,
                APPROVALS: sessions,
                DOCUMENTS: sessions,
                RUNS: sessions,
            }
            for kind, keys in doomed.items():
                if keys:
                    db.execute(
                        delete(RecordRow).where(RecordRow.kind == kind, RecordRow.id.in_(keys))
                    )
            if sessions:
                db.execute(delete(EventRow).where(EventRow.session_id.in_(sessions)))
                db.execute(delete(ActivityRow).where(ActivityRow.session_id.in_(sessions)))
        return threads

    @staticmethod
    def _row(db: Session, kind: str, key: str, *, lock: bool = False) -> RecordRow | None:
        return db.get(RecordRow, {"kind": kind, "id": key}, with_for_update=lock)

    @staticmethod
    def _write(db: Session, kind: str, key: str, payload: Any, row: RecordRow | None) -> None:
        if row is None:
            db.add(RecordRow(kind=kind, id=key, payload=_plain(payload)))
        else:
            row.payload = _plain(payload)

    @staticmethod
    def _rows(db: Session, kind: str) -> list[RecordRow]:
        rows = db.scalars(select(RecordRow).where(RecordRow.kind == kind)).all()
        return sorted(rows, key=lambda row: row.id)

    def _get(self, kind: str, key: str) -> dict[str, Any]:
        with _transaction(self._engine) as db:
            row = self._row(db, kind, key)
            if row is None:
                raise NotFound(f"{kind}: {key}")
            return copy.deepcopy(row.payload)

    def _put(self, kind: str, key: str, value: dict[str, Any]) -> dict[str, Any]:
        with _transaction(self._engine) as db:
            self._write(db, kind, key, value, self._row(db, kind, key))
        return copy.deepcopy(value)

    def _all(self, kind: str) -> list[dict[str, Any]]:
        with _transaction(self._engine) as db:
            return [copy.deepcopy(row.payload) for row in self._rows(db, kind)]

    def _per_session(self, kind: str, session_id: str) -> list[dict[str, Any]]:
        with _transaction(self._engine) as db:
            row = self._row(db, kind, session_id)
            return copy.deepcopy(row.payload.get("items", [])) if row is not None else []

    def _upsert(self, kind: str, session_id: str, item: dict[str, Any]) -> dict[str, Any]:
        with _transaction(self._engine) as db:
            row = self._row(db, kind, session_id, lock=True)
            items = copy.deepcopy(row.payload.get("items", [])) if row is not None else []
            for index, old in enumerate(items):
                if old.get("id") == item.get("id"):
                    items[index] = item
                    break
            else:
                items.append(item)
            self._write(db, kind, session_id, {"items": items}, row)
        return copy.deepcopy(item)

    # ── 조직 ──────────────────────────────────────────────────────────
    def put_org(self, org: dict[str, Any]) -> dict[str, Any]:
        return self._put(ORGS, org["id"], org)

    def get_org(self, org_id: str) -> dict[str, Any]:
        return self._get(ORGS, org_id)

    def list_orgs(self, role: str | None = None) -> list[dict[str, Any]]:
        return [org for org in self._all(ORGS) if role is None or org.get("role") == role]

    # ── 흐름 ──────────────────────────────────────────────────────────
    def put_stream(self, stream: dict[str, Any]) -> dict[str, Any]:
        if not stream.get("orgId"):
            raise ValueError("흐름에는 orgId가 있어야 한다")
        return self._put(STREAMS, stream["id"], stream)

    def get_stream(self, stream_id: str, org_id: str | None = None) -> dict[str, Any]:
        """org_id를 주면 그 조직이 볼 수 있는 흐름일 때만 돌려준다. 아니면 없는 것으로 답한다."""
        stream = self._get(STREAMS, stream_id)
        visible = org_id is None or stream["orgId"] == org_id
        if not visible and stream_id not in {s["id"] for s in self.streams_visible_to(org_id)}:
            raise NotFound(f"{STREAMS}: {stream_id}")
        return stream

    def list_streams(self, org_id: str) -> list[dict[str, Any]]:
        return [stream for stream in self._all(STREAMS) if stream.get("orgId") == org_id]

    def streams_visible_to(self, org_id: str) -> list[dict[str, Any]]:
        """자기 흐름과, 자기가 업체로 들어간 열린 스레드의 흐름."""
        streams = self._all(STREAMS)
        visible = {s["id"] for s in streams if s.get("orgId") == org_id}
        for thread in self._all(THREADS):
            if (
                thread.get("vendorOrgId") == org_id
                and thread.get("status") in SHARED_THREAD_STATUSES
            ):
                visible.update(thread.get("streamIds") or [])
        return [stream for stream in streams if stream["id"] in visible]

    # ── 업체 프로필 ───────────────────────────────────────────────────
    def put_vendor_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        return self._put(VENDORS, profile["orgId"], profile)

    def get_vendor_profile(self, org_id: str) -> dict[str, Any]:
        return self._get(VENDORS, org_id)

    def list_vendor_profiles(self) -> list[dict[str, Any]]:
        return self._all(VENDORS)

    # ── 세션 ──────────────────────────────────────────────────────────
    def put_session(
        self, session: dict[str, Any], expected_version: int | None = None
    ) -> dict[str, Any]:
        """저장할 때마다 version을 1 올린다. expected_version이 지금 값과 다르면 거부한다."""
        with _transaction(self._engine) as db:
            row = self._row(db, SESSIONS, session["id"], lock=True)
            old = row.payload if row is not None else None
            current = int(old.get("version", 0)) if old else 0
            if expected_version is not None and expected_version != current:
                raise VersionConflict(
                    f"세션 {session['id']}의 버전이 {current}인데 "
                    f"{expected_version}으로 저장하려 했다"
                )
            saved = {**session, "version": current + 1}
            saved.setdefault("createdAt", old.get("createdAt") if old else self.now())
            saved["updatedAt"] = self.now()
            self._write(db, SESSIONS, saved["id"], saved, row)
        return copy.deepcopy(saved)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get(SESSIONS, session_id)

    def list_sessions(self, org_id: str | None = None) -> list[dict[str, Any]]:
        """org_id를 주면 그 조직이 배출 기업이거나 업체로 스레드에 들어간 세션만."""
        sessions = self._all(SESSIONS)
        if org_id is None:
            return sessions
        joined = {t.get("sessionId") for t in self._all(THREADS) if t.get("vendorOrgId") == org_id}
        return [s for s in sessions if s.get("emitterOrgId") == org_id or s["id"] in joined]

    # ── 스레드 ────────────────────────────────────────────────────────
    def put_thread(self, thread: Any) -> dict[str, Any]:
        data = thread.to_dict() if hasattr(thread, "to_dict") else thread
        return self._put(THREADS, data["id"], data)

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self._get(THREADS, thread_id)

    def list_threads(
        self, session_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]:
        """org_id를 주면 그 조직이 당사자인 스레드만."""
        return [
            thread
            for thread in self._all(THREADS)
            if (session_id is None or thread.get("sessionId") == session_id)
            and (
                org_id is None or org_id in (thread.get("emitterOrgId"), thread.get("vendorOrgId"))
            )
        ]

    # ── 질문, 승인, 문서 ──────────────────────────────────────────────
    def put_question(self, session_id: str, question: dict[str, Any]) -> dict[str, Any]:
        return self._upsert(QUESTIONS, session_id, question)

    def list_questions(self, session_id: str) -> list[dict[str, Any]]:
        return self._per_session(QUESTIONS, session_id)

    def _approval(self, session_id: str, thread_id: str) -> dict[str, Any]:
        for item in self._per_session(APPROVALS, session_id):
            if item["threadId"] == thread_id:
                return item
        return {
            "id": thread_id,
            "threadId": thread_id,
            "emitter": False,
            "vendor": False,
            "blocked": False,
            "blockedReason": None,
        }

    def set_approval(
        self, session_id: str, thread_id: str, side: str, value: bool = True
    ) -> dict[str, Any]:
        if side not in ("emitter", "vendor"):
            raise ValueError(f"승인 쪽은 emitter나 vendor여야 한다: {side}")
        item = self._approval(session_id, thread_id)
        if value and item["blocked"]:
            raise PermissionError(
                item["blockedReason"] or "근거 확인 실패 항목이 있어 승인할 수 없다"
            )
        item[side] = bool(value)
        return self._upsert(APPROVALS, session_id, item)

    def block_approval(self, session_id: str, thread_id: str, reason: str | None) -> dict[str, Any]:
        """근거 확인 실패 항목이 있으면 승인을 막는다. reason이 None이면 막은 것을 푼다."""
        item = self._approval(session_id, thread_id)
        item["blocked"] = reason is not None
        item["blockedReason"] = reason
        if reason is not None:
            # 막으면서 지운 승인(화면이 "앞서 한 승인을 지웠다"고 할지 정한다). 이미 막혀 있던 것을
            # 다시 막을 때는 앞서 지운 것을 그대로 둔다
            cleared = [side for side in ("emitter", "vendor") if item.get(side)]
            item["clearedApprovals"] = cleared or list(item.get("clearedApprovals") or [])
            item["emitter"] = item["vendor"] = False
        else:
            item.pop("clearedApprovals", None)
        return self._upsert(APPROVALS, session_id, item)

    def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in item.items() if k != "id"}
            for item in self._per_session(APPROVALS, session_id)
        ]

    def put_document(self, session_id: str, document: dict[str, Any]) -> dict[str, Any]:
        return self._upsert(DOCUMENTS, session_id, document)

    def list_documents(self, session_id: str) -> list[dict[str, Any]]:
        return self._per_session(DOCUMENTS, session_id)

    # ── 실행 기록 ─────────────────────────────────────────────────────
    def add_event(
        self,
        session_id: str,
        actor: str,
        kind: str,
        summary: str,
        *,
        thread_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """seq는 세션마다 1부터 센다. 동시에 두 요청이 같은 seq를 쓰면 기본 키 충돌로 실패한다."""
        with _transaction(self._engine) as db:
            last = db.scalar(
                select(func.max(EventRow.seq)).where(EventRow.session_id == session_id)
            )
            event = {
                "seq": int(last or 0) + 1,
                "at": self.now(),
                "actor": actor,
                "kind": kind,
                "threadId": thread_id,
                "summary": summary,
                "detail": _plain(detail or {}),
            }
            db.add(EventRow(session_id=session_id, seq=event["seq"], payload=event))
        return copy.deepcopy(event)

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        query = select(EventRow).where(EventRow.session_id == session_id).order_by(EventRow.seq)
        with _transaction(self._engine) as db:
            return [copy.deepcopy(row.payload) for row in db.scalars(query).all()]

    # ── 실시간 진행 기록과 실행 자리 ──────────────────────────────────
    def add_activity(self, session_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        """seq는 세션마다 1부터 센다. 기록 하나를 곧바로 커밋한다(실행 중에도 다른 요청이 읽는다).

        두 요청이 같은 seq를 쓰려 하면 기본 키 충돌이 나므로 몇 번 다시 시도한다.
        """
        tries = 0
        while True:
            try:
                with _transaction(self._engine) as db:
                    last = db.scalar(
                        select(func.max(ActivityRow.seq)).where(
                            ActivityRow.session_id == session_id
                        )
                    )
                    saved = {**_plain(entry), "seq": int(last or 0) + 1}
                    saved.setdefault("at", self.now())
                    db.add(ActivityRow(session_id=session_id, seq=saved["seq"], payload=saved))
                return copy.deepcopy(saved)
            except IntegrityError:
                tries += 1
                if tries >= 5:
                    raise

    def list_activity(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        query = (
            select(ActivityRow)
            .where(ActivityRow.session_id == session_id, ActivityRow.seq > int(after))
            .order_by(ActivityRow.seq)
        )
        with _transaction(self._engine) as db:
            return [copy.deepcopy(row.payload) for row in db.scalars(query).all()]

    def claim_run(
        self, session_id: str, run: dict[str, Any], stale_before: float
    ) -> dict[str, Any] | None:
        """비어 있거나 heartbeat가 stale_before보다 앞선(멈춘) 자리면 run을 적고 None을 준다.

        다른 실행이 살아 있으면 적지 않고 그 실행을 준다. 같은 순간에 둘이 빈 자리를 차지하려 하면
        기본 키 충돌로 하나만 적히고, 진 쪽은 이긴 쪽의 실행을 받는다.
        """
        try:
            with _transaction(self._engine) as db:
                row = self._row(db, RUNS, session_id, lock=True)
                if row is not None and float(row.payload.get("heartbeat") or 0) >= stale_before:
                    return copy.deepcopy(row.payload)
                self._write(db, RUNS, session_id, run, row)
        except IntegrityError:
            current = self.get_run(session_id)
            return current if current is not None else dict(run)
        return None

    def touch_run(self, session_id: str, run_id: str, at: float) -> bool:
        with _transaction(self._engine) as db:
            row = self._row(db, RUNS, session_id, lock=True)
            if row is None or row.payload.get("runId") != run_id:
                return False
            self._write(db, RUNS, session_id, {**row.payload, "heartbeat": float(at)}, row)
        return True

    def release_run(self, session_id: str, run_id: str) -> None:
        with _transaction(self._engine) as db:
            row = self._row(db, RUNS, session_id, lock=True)
            if row is not None and row.payload.get("runId") == run_id:
                db.delete(row)

    def get_run(self, session_id: str) -> dict[str, Any] | None:
        with _transaction(self._engine) as db:
            row = self._row(db, RUNS, session_id)
            return copy.deepcopy(row.payload) if row is not None else None


# ── 계정 (V3_AUTH=bff) ──────────────────────────────────────────────
def _user(row: UserRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "email": row.email,
        "name": row.name,
        "passwordHash": row.password_hash,
        "createdAt": row.created_at,
    }


def _membership(row: MembershipRow) -> dict[str, Any]:
    return {
        "userId": row.user_id,
        "orgId": row.org_id,
        "memberRole": row.member_role,
        "createdAt": row.created_at,
    }


def _invite(row: InviteRow) -> dict[str, Any]:
    return {
        "tokenHash": row.token_hash,
        "orgId": row.org_id,
        "createdBy": row.created_by,
        "createdAt": row.created_at,
        "expiresAt": row.expires_at,
        "usedAt": row.used_at,
        "usedBy": row.used_by,
    }


class SqlAccounts:
    """account_store.AccountStore 구현. InMemoryAccounts와 같은 동작을 한다.

    이메일이 겹치는지는 v3_users.email의 유일 제약이 마지막으로 막는다(동시에 가입해도 하나만 남는다).
    """

    def __init__(self, engine: Engine, clock: Callable[[], Any] | None = None) -> None:
        self._engine = engine
        self._clock = clock or utc_now

    def now(self) -> Any:
        return self._clock()

    # ── 사용자 ────────────────────────────────────────────────────────
    def add_user(self, user: dict[str, Any]) -> dict[str, Any]:
        try:
            with _transaction(self._engine) as db:
                taken = db.scalar(select(UserRow.id).where(UserRow.email == user["email"]))
                if taken is not None:
                    raise EmailTaken(user["email"])
                db.add(
                    UserRow(
                        id=user["id"],
                        email=user["email"],
                        name=user["name"],
                        password_hash=user["passwordHash"],
                        created_at=user["createdAt"],
                    )
                )
        except IntegrityError:
            raise EmailTaken(user["email"]) from None
        return copy.deepcopy(user)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with _transaction(self._engine) as db:
            row = db.get(UserRow, user_id)
            return _user(row) if row is not None else None

    def find_user(self, email: str) -> dict[str, Any] | None:
        with _transaction(self._engine) as db:
            row = db.scalar(select(UserRow).where(UserRow.email == email))
            return _user(row) if row is not None else None

    # ── 소속 ──────────────────────────────────────────────────────────
    def add_membership(self, membership: dict[str, Any]) -> dict[str, Any]:
        """이미 소속이면 있던 것을 그대로 돌려준다(역할을 바꾸지 않는다)."""
        key = {"user_id": membership["userId"], "org_id": membership["orgId"]}
        try:
            with _transaction(self._engine) as db:
                row = db.get(MembershipRow, key)
                if row is not None:
                    return _membership(row)
                db.add(
                    MembershipRow(
                        **key,
                        member_role=membership["memberRole"],
                        created_at=membership["createdAt"],
                    )
                )
        except IntegrityError:  # 같은 사람이 동시에 두 번 들어왔다
            found = self.get_membership(membership["userId"], membership["orgId"])
            if found is None:
                raise
            return found
        return copy.deepcopy(membership)

    def get_membership(self, user_id: str, org_id: str) -> dict[str, Any] | None:
        with _transaction(self._engine) as db:
            row = db.get(MembershipRow, {"user_id": user_id, "org_id": org_id})
            return _membership(row) if row is not None else None

    def list_memberships(
        self, *, user_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = select(MembershipRow)
        if user_id is not None:
            query = query.where(MembershipRow.user_id == user_id)
        if org_id is not None:
            query = query.where(MembershipRow.org_id == org_id)
        with _transaction(self._engine) as db:
            found = [_membership(row) for row in db.scalars(query).all()]
        return sorted(found, key=lambda m: (m.get("createdAt") or "", m["orgId"], m["userId"]))

    # ── 초대 ──────────────────────────────────────────────────────────
    def add_invite(self, invite: dict[str, Any]) -> dict[str, Any]:
        with _transaction(self._engine) as db:
            db.add(
                InviteRow(
                    token_hash=invite["tokenHash"],
                    org_id=invite["orgId"],
                    created_by=invite["createdBy"],
                    created_at=invite["createdAt"],
                    expires_at=invite["expiresAt"],
                    used_at=invite.get("usedAt"),
                    used_by=invite.get("usedBy"),
                )
            )
        return copy.deepcopy(invite)

    def use_invite(self, token_hash: str, user_id: str, now: Any) -> dict[str, Any] | None:
        """쓰지 않았고 만료되지 않은 초대면 쓴 것으로 적고 돌려준다. 아니면 None. 한 번만 쓸 수 있다."""
        with _transaction(self._engine) as db:
            row = db.get(InviteRow, token_hash, with_for_update=True)
            if row is None or row.used_at or parse_iso(row.expires_at) <= now:
                return None
            row.used_at, row.used_by = iso(now), user_id
            return _invite(row)
