"""저장소 인터페이스와 메모리·파일 구현. 레코드는 계약 6·7장의 JSON(camelCase) dict다.

읽을 때는 항상 깊은 복사를 돌려준다. 바꾸려면 put_*으로 다시 저장해야 한다.
조직 경계: list_streams(org_id)는 그 조직의 흐름만 준다. 업체가 볼 수 있는 흐름은
streams_visible_to(org_id)로 묻는다(자기와 열린 스레드에 걸린 흐름만).
"""

from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# 대화가 열린 적이 있어 업체가 흐름을 볼 수 있는 스레드 상태
SHARED_THREAD_STATUSES = ("open", "agreed", "declined", "handoff", "confirmed")
_TABLES = (
    "orgs",
    "streams",
    "vendors",
    "sessions",
    "threads",
    "questions",
    "approvals",
    "documents",
    "events",
    "counters",
    "activity",
    "runs",
)


class NotFound(KeyError):
    """그런 레코드가 없다."""


class VersionConflict(RuntimeError):
    """다른 요청이 먼저 세션을 바꿨다."""


def system_clock() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store(Protocol):
    def now(self) -> str: ...
    def next_id(self, prefix: str) -> str: ...
    def reset(self) -> None: ...
    def purge_orgs(self, org_ids: Iterable[str]) -> list[str]: ...

    def put_org(self, org: dict[str, Any]) -> dict[str, Any]: ...
    def get_org(self, org_id: str) -> dict[str, Any]: ...
    def list_orgs(self, role: str | None = None) -> list[dict[str, Any]]: ...

    def put_stream(self, stream: dict[str, Any]) -> dict[str, Any]: ...
    def get_stream(self, stream_id: str, org_id: str | None = None) -> dict[str, Any]: ...
    def list_streams(self, org_id: str) -> list[dict[str, Any]]: ...
    def streams_visible_to(self, org_id: str) -> list[dict[str, Any]]: ...

    def put_vendor_profile(self, profile: dict[str, Any]) -> dict[str, Any]: ...
    def get_vendor_profile(self, org_id: str) -> dict[str, Any]: ...
    def list_vendor_profiles(self) -> list[dict[str, Any]]: ...

    def put_session(
        self, session: dict[str, Any], expected_version: int | None = None
    ) -> dict[str, Any]: ...
    def get_session(self, session_id: str) -> dict[str, Any]: ...
    def list_sessions(self, org_id: str | None = None) -> list[dict[str, Any]]: ...

    def put_thread(self, thread: Any) -> dict[str, Any]: ...
    def get_thread(self, thread_id: str) -> dict[str, Any]: ...
    def list_threads(
        self, session_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def put_question(self, session_id: str, question: dict[str, Any]) -> dict[str, Any]: ...
    def list_questions(self, session_id: str) -> list[dict[str, Any]]: ...

    def set_approval(
        self, session_id: str, thread_id: str, side: str, value: bool = True
    ) -> dict[str, Any]: ...
    def block_approval(
        self, session_id: str, thread_id: str, reason: str | None
    ) -> dict[str, Any]: ...
    def list_approvals(self, session_id: str) -> list[dict[str, Any]]: ...

    def put_document(self, session_id: str, document: dict[str, Any]) -> dict[str, Any]: ...
    def list_documents(self, session_id: str) -> list[dict[str, Any]]: ...

    def add_event(
        self,
        session_id: str,
        actor: str,
        kind: str,
        summary: str,
        *,
        thread_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def list_events(self, session_id: str) -> list[dict[str, Any]]: ...

    # 실시간 진행 기록(live.py)과 실행 자리(한 진단에 실행 하나). 계약 8장 "실시간 진행"
    def add_activity(self, session_id: str, entry: dict[str, Any]) -> dict[str, Any]: ...
    def list_activity(self, session_id: str, after: int = 0) -> list[dict[str, Any]]: ...
    def claim_run(
        self, session_id: str, run: dict[str, Any], stale_before: float
    ) -> dict[str, Any] | None: ...
    def touch_run(self, session_id: str, run_id: str, at: float) -> bool: ...
    def release_run(self, session_id: str, run_id: str) -> None: ...
    def get_run(self, session_id: str) -> dict[str, Any] | None: ...


class InMemoryStore:
    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock = clock or system_clock
        self._data: dict[str, dict[str, Any]] = {table: {} for table in _TABLES}
        self._runs_lock = threading.Lock()  # 실행 자리를 차지하는 일(확인과 적기)을 한 번에 한다

    # ── 공통 ──────────────────────────────────────────────────────────
    def now(self) -> str:
        return self._clock()

    def next_id(self, prefix: str) -> str:
        """ses_1, th_2처럼 접두어별로 1부터 센다. 난수를 쓰지 않아 실행마다 같다."""
        counters = self._data["counters"]
        counters[prefix] = int(counters.get(prefix, 0)) + 1
        self._changed()
        return f"{prefix}_{counters[prefix]}"

    def reset(self) -> None:
        self._data = {table: {} for table in _TABLES}
        self._changed()

    def purge_orgs(self, org_ids: Iterable[str]) -> list[str]:
        """org_ids의 조직, 흐름, 업체 프로필, 그 조직이 배출 기업인 진단을 지운다. 지운 스레드 ID를 준다.

        진단과 함께 그 스레드, 질문, 승인, 문서, 실행 기록도 지운다. 다른 조직이 배출 기업인 진단의
        스레드는 업체가 org_ids에 있어도 남긴다(그 진단의 기록이다). ID 카운터는 그대로 둔다.
        """
        wanted = set(org_ids)
        data = self._data
        sessions = {k for k, s in data["sessions"].items() if s.get("emitterOrgId") in wanted}
        threads = sorted(
            k
            for k, t in data["threads"].items()
            if t.get("sessionId") in sessions or t.get("emitterOrgId") in wanted
        )
        doomed = {
            "orgs": wanted,
            "streams": {k for k, s in data["streams"].items() if s.get("orgId") in wanted},
            "vendors": wanted,
            "sessions": sessions,
            "threads": set(threads),
            "questions": sessions,
            "approvals": sessions,
            "documents": sessions,
            "events": sessions,
            "activity": sessions,
            "runs": sessions,
        }
        for table, keys in doomed.items():
            for key in keys:
                data[table].pop(key, None)
        self._changed()
        return threads

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def _changed(self) -> None:
        """파일 저장소가 덮어쓴다."""

    def _get(self, table: str, key: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._data[table][key])
        except KeyError:
            raise NotFound(f"{table}: {key}") from None

    def _put(self, table: str, key: str, value: Any) -> Any:
        self._data[table][key] = copy.deepcopy(value)
        self._changed()
        return copy.deepcopy(value)

    def _per_session(self, table: str, session_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._data[table].get(session_id, []))

    def _upsert(self, table: str, session_id: str, item: dict[str, Any]) -> dict[str, Any]:
        items = self._data[table].setdefault(session_id, [])
        for index, old in enumerate(items):
            if old.get("id") == item.get("id"):
                items[index] = copy.deepcopy(item)
                break
        else:
            items.append(copy.deepcopy(item))
        self._changed()
        return copy.deepcopy(item)

    # ── 조직 ──────────────────────────────────────────────────────────
    def put_org(self, org: dict[str, Any]) -> dict[str, Any]:
        return self._put("orgs", org["id"], org)

    def get_org(self, org_id: str) -> dict[str, Any]:
        return self._get("orgs", org_id)

    def list_orgs(self, role: str | None = None) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(org)
            for _, org in sorted(self._data["orgs"].items())
            if role is None or org.get("role") == role
        ]

    # ── 흐름 ──────────────────────────────────────────────────────────
    def put_stream(self, stream: dict[str, Any]) -> dict[str, Any]:
        if not stream.get("orgId"):
            raise ValueError("흐름에는 orgId가 있어야 한다")
        return self._put("streams", stream["id"], stream)

    def get_stream(self, stream_id: str, org_id: str | None = None) -> dict[str, Any]:
        """org_id를 주면 그 조직이 볼 수 있는 흐름일 때만 돌려준다. 아니면 없는 것으로 답한다."""
        stream = self._get("streams", stream_id)
        visible = org_id is None or stream["orgId"] == org_id
        if not visible and stream_id not in {s["id"] for s in self.streams_visible_to(org_id)}:
            raise NotFound(f"streams: {stream_id}")
        return stream

    def list_streams(self, org_id: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(stream)
            for _, stream in sorted(self._data["streams"].items())
            if stream.get("orgId") == org_id
        ]

    def streams_visible_to(self, org_id: str) -> list[dict[str, Any]]:
        """자기 흐름과, 자기가 업체로 들어간 열린 스레드의 흐름."""
        visible = {s["id"] for s in self._data["streams"].values() if s.get("orgId") == org_id}
        for thread in self._data["threads"].values():
            if (
                thread.get("vendorOrgId") == org_id
                and thread.get("status") in SHARED_THREAD_STATUSES
            ):
                visible.update(thread.get("streamIds") or [])
        return [
            copy.deepcopy(stream)
            for sid, stream in sorted(self._data["streams"].items())
            if sid in visible
        ]

    # ── 업체 프로필 ───────────────────────────────────────────────────
    def put_vendor_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        return self._put("vendors", profile["orgId"], profile)

    def get_vendor_profile(self, org_id: str) -> dict[str, Any]:
        return self._get("vendors", org_id)

    def list_vendor_profiles(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(p) for _, p in sorted(self._data["vendors"].items())]

    # ── 세션 ──────────────────────────────────────────────────────────
    def put_session(
        self, session: dict[str, Any], expected_version: int | None = None
    ) -> dict[str, Any]:
        """저장할 때마다 version을 1 올린다. expected_version이 지금 값과 다르면 거부한다."""
        old = self._data["sessions"].get(session["id"])
        current = int(old.get("version", 0)) if old else 0
        if expected_version is not None and expected_version != current:
            raise VersionConflict(
                f"세션 {session['id']}의 버전이 {current}인데 {expected_version}으로 저장하려 했다"
            )
        saved = {**session, "version": current + 1}
        saved.setdefault("createdAt", old.get("createdAt") if old else self.now())
        saved["updatedAt"] = self.now()
        return self._put("sessions", saved["id"], saved)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get("sessions", session_id)

    def list_sessions(self, org_id: str | None = None) -> list[dict[str, Any]]:
        """org_id를 주면 그 조직이 배출 기업이거나 업체로 스레드에 들어간 세션만."""
        result = []
        for sid, session in sorted(self._data["sessions"].items()):
            if (
                org_id is None
                or session.get("emitterOrgId") == org_id
                or any(
                    t.get("sessionId") == sid and t.get("vendorOrgId") == org_id
                    for t in self._data["threads"].values()
                )
            ):
                result.append(copy.deepcopy(session))
        return result

    # ── 스레드 ────────────────────────────────────────────────────────
    def put_thread(self, thread: Any) -> dict[str, Any]:
        data = thread.to_dict() if hasattr(thread, "to_dict") else thread
        return self._put("threads", data["id"], data)

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self._get("threads", thread_id)

    def list_threads(
        self, session_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]:
        """org_id를 주면 그 조직이 당사자인 스레드만."""
        return [
            copy.deepcopy(thread)
            for _, thread in sorted(self._data["threads"].items())
            if (session_id is None or thread.get("sessionId") == session_id)
            and (
                org_id is None or org_id in (thread.get("emitterOrgId"), thread.get("vendorOrgId"))
            )
        ]

    # ── 질문, 승인, 문서 ──────────────────────────────────────────────
    def put_question(self, session_id: str, question: dict[str, Any]) -> dict[str, Any]:
        return self._upsert("questions", session_id, question)

    def list_questions(self, session_id: str) -> list[dict[str, Any]]:
        return self._per_session("questions", session_id)

    def _approval(self, session_id: str, thread_id: str) -> dict[str, Any]:
        for item in self._data["approvals"].get(session_id, []):
            if item["threadId"] == thread_id:
                return copy.deepcopy(item)
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
        return self._upsert("approvals", session_id, item)

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
        return self._upsert("approvals", session_id, item)

    def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in item.items() if k != "id"}
            for item in self._per_session("approvals", session_id)
        ]

    def put_document(self, session_id: str, document: dict[str, Any]) -> dict[str, Any]:
        return self._upsert("documents", session_id, document)

    def list_documents(self, session_id: str) -> list[dict[str, Any]]:
        return self._per_session("documents", session_id)

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
        events = self._data["events"].setdefault(session_id, [])
        event = {
            "seq": len(events) + 1,
            "at": self.now(),
            "actor": actor,
            "kind": kind,
            "threadId": thread_id,
            "summary": summary,
            "detail": copy.deepcopy(detail or {}),
        }
        events.append(event)
        self._changed()
        return copy.deepcopy(event)

    def list_events(self, session_id: str) -> list[dict[str, Any]]:
        return self._per_session("events", session_id)

    # ── 실시간 진행 기록과 실행 자리 ──────────────────────────────────
    def add_activity(self, session_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        """seq는 세션마다 1부터 센다. at이 없으면 지금 시각을 적는다."""
        items = self._data["activity"].setdefault(session_id, [])
        saved = {**copy.deepcopy(entry), "seq": len(items) + 1}
        saved.setdefault("at", self.now())
        items.append(saved)
        self._changed()
        return copy.deepcopy(saved)

    def list_activity(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        """seq가 after보다 큰 기록을 seq 순으로."""
        items = list(self._data["activity"].get(session_id) or [])
        return [copy.deepcopy(item) for item in items if int(item["seq"]) > after]

    def claim_run(
        self, session_id: str, run: dict[str, Any], stale_before: float
    ) -> dict[str, Any] | None:
        """비어 있거나 heartbeat가 stale_before보다 앞선(멈춘) 자리면 run을 적고 None을 준다.

        다른 실행이 살아 있으면 적지 않고 그 실행을 준다.
        """
        with self._runs_lock:
            current = self._data["runs"].get(session_id)
            if current is not None and float(current.get("heartbeat") or 0) >= stale_before:
                return copy.deepcopy(current)
            self._data["runs"][session_id] = copy.deepcopy(run)
        self._changed()
        return None

    def touch_run(self, session_id: str, run_id: str, at: float) -> bool:
        """살아 있다고 적는다(heartbeat). 자리를 잃었으면 False. 파일에는 쓰지 않는다."""
        current = self._data["runs"].get(session_id)
        if current is None or current.get("runId") != run_id:
            return False
        # 있는 키의 값만 바꾼다(다른 스레드가 표를 읽고 있어도 표의 크기가 바뀌지 않는다)
        current["heartbeat"] = float(at)
        return True

    def release_run(self, session_id: str, run_id: str) -> None:
        with self._runs_lock:
            current = self._data["runs"].get(session_id)
            if current is None or current.get("runId") != run_id:
                return
            self._data["runs"].pop(session_id, None)
        self._changed()

    def get_run(self, session_id: str) -> dict[str, Any] | None:
        current = self._data["runs"].get(session_id)
        return copy.deepcopy(current) if current is not None else None


class JsonFileStore(InMemoryStore):
    """JSON 파일 하나에 통째로 쓴다. 로컬 시연과 개발용이다(동시 쓰기를 막지 않는다)."""

    def __init__(
        self, path: str | os.PathLike[str], clock: Callable[[], str] | None = None
    ) -> None:
        super().__init__(clock)
        self.path = Path(path)
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = {table: loaded.get(table, {}) for table in _TABLES}

    def _changed(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
        )
        os.replace(temp, self.path)
