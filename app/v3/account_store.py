"""계정 저장소: 사용자, 소속, 초대. 메모리 구현과 로그인 실패 제한이 여기 있다(SQL 구현은 sql.py).

V3_AUTH=bff에서만 쓴다. 조직 레코드는 여기가 아니라 기존 저장소(store.py의 orgs)에 둔다.

- 사용자: {"id", "email", "name", "passwordHash", "createdAt"}. 이메일은 소문자로 맞춰 하나만 둔다.
  passwordHash는 accounts.py가 만든 scrypt 값이다. 밖으로 내보내는 일은 accounts.py가 막는다.
- 소속: {"userId", "orgId", "memberRole": "owner" | "member", "createdAt"}. 한 사람은 한 조직에 한 번만.
- 초대: {"tokenHash", "orgId", "createdBy", "createdAt", "expiresAt", "usedAt", "usedBy"}.
  토큰 원문은 저장하지 않는다(sha256 16진 값만). 시각은 UTC ISO 글이다.

읽을 때는 항상 복사본을 돌려준다.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol


class EmailTaken(ValueError):
    """같은 이메일의 사용자가 이미 있다."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(str(text))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class AccountStore(Protocol):
    def now(self) -> datetime: ...

    def add_user(self, user: dict[str, Any]) -> dict[str, Any]: ...
    def get_user(self, user_id: str) -> dict[str, Any] | None: ...
    def find_user(self, email: str) -> dict[str, Any] | None: ...

    def add_membership(self, membership: dict[str, Any]) -> dict[str, Any]: ...
    def get_membership(self, user_id: str, org_id: str) -> dict[str, Any] | None: ...
    def list_memberships(
        self, *, user_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def add_invite(self, invite: dict[str, Any]) -> dict[str, Any]: ...
    def use_invite(self, token_hash: str, user_id: str, now: datetime) -> dict[str, Any] | None: ...


class InMemoryAccounts:
    """테스트와 메모리 모드(DATABASE_URL이 없을 때)용."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or utc_now
        self._lock = threading.RLock()
        self._users: dict[str, dict[str, Any]] = {}
        self._emails: dict[str, str] = {}
        self._memberships: dict[tuple[str, str], dict[str, Any]] = {}
        self._invites: dict[str, dict[str, Any]] = {}

    def now(self) -> datetime:
        return self._clock()

    # ── 사용자 ────────────────────────────────────────────────────────
    def add_user(self, user: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if user["email"] in self._emails:
                raise EmailTaken(user["email"])
            self._users[user["id"]] = copy.deepcopy(user)
            self._emails[user["email"]] = user["id"]
        return copy.deepcopy(user)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._users.get(user_id)
            return copy.deepcopy(found) if found else None

    def find_user(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            user_id = self._emails.get(email)
            return copy.deepcopy(self._users[user_id]) if user_id else None

    # ── 소속 ──────────────────────────────────────────────────────────
    def add_membership(self, membership: dict[str, Any]) -> dict[str, Any]:
        """이미 소속이면 있던 것을 그대로 돌려준다(역할을 바꾸지 않는다)."""
        key = (membership["userId"], membership["orgId"])
        with self._lock:
            found = self._memberships.setdefault(key, copy.deepcopy(membership))
            return copy.deepcopy(found)

    def get_membership(self, user_id: str, org_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._memberships.get((user_id, org_id))
            return copy.deepcopy(found) if found else None

    def list_memberships(
        self, *, user_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            found = [
                copy.deepcopy(item)
                for (uid, oid), item in self._memberships.items()
                if (user_id is None or uid == user_id) and (org_id is None or oid == org_id)
            ]
        return sorted(found, key=lambda m: (m.get("createdAt") or "", m["orgId"], m["userId"]))

    # ── 초대 ──────────────────────────────────────────────────────────
    def add_invite(self, invite: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._invites[invite["tokenHash"]] = copy.deepcopy(invite)
        return copy.deepcopy(invite)

    def use_invite(self, token_hash: str, user_id: str, now: datetime) -> dict[str, Any] | None:
        """쓰지 않았고 만료되지 않은 초대면 쓴 것으로 적고 돌려준다. 아니면 None. 한 번만 쓸 수 있다."""
        with self._lock:
            invite = self._invites.get(token_hash)
            if invite is None or invite.get("usedAt") or parse_iso(invite["expiresAt"]) <= now:
                return None
            invite.update(usedAt=iso(now), usedBy=user_id)
            return copy.deepcopy(invite)


class LoginLimiter:
    """이메일마다 로그인 실패를 센다. window초 안에 limit번 실패하면 막는다(프로세스 메모리).

    여러 프로세스로 띄우면 프로세스마다 따로 센다. 성공하면 그 이메일의 기록을 지운다.
    """

    def __init__(
        self,
        limit: int = 10,
        window: float = 15 * 60,
        clock: Callable[[], float] | None = None,
        max_keys: int = 10_000,
    ) -> None:
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        found = self._failures.get(key)
        if found is None:
            return deque()
        while found and found[0] <= now - self.window:
            found.popleft()
        if not found:
            self._failures.pop(key, None)
        return found

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, self._clock())) >= self.limit

    def failed(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._failures) >= self.max_keys:
                for stale in list(self._failures):
                    self._recent(stale, now)
            self._recent(key, now)
            self._failures.setdefault(key, deque()).append(now)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
