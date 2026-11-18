"""계정 저장소: 사용자, 소속, 초대. 메모리 구현과 로그인 실패 제한이 여기 있다(SQL 구현은 sql.py).

V3_AUTH=bff에서만 쓴다. 조직 레코드는 여기가 아니라 기존 저장소(store.py의 orgs)에 둔다.

- 사용자: {"id", "email", "name", "passwordHash", "createdAt"}. 이메일은 소문자로 맞춰 하나만 둔다.
  passwordHash는 accounts.py가 만든 scrypt 값이다. 밖으로 내보내는 일은 accounts.py가 막는다.
- 소속: {"userId", "orgId", "memberRole": "owner" | "member", "createdAt"}. 한 사람은 한 조직에 한 번만.
- 초대: {"tokenHash", "orgId", "createdBy", "createdAt", "expiresAt", "usedAt", "usedBy"}.
  토큰 원문은 저장하지 않는다(sha256 16진 값만). 시각은 UTC ISO 글이다.

읽을 때는 항상 복사본을 돌려준다.

지우기(V3_AUTH=bff의 탈퇴·구성원 빼기·운영자 명령)는 accounts.py가 규칙을 정하고 여기서는 레코드만 지운다.
"""

from __future__ import annotations

import copy
import re
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


def like_pattern(text: str) -> str:
    """운영자가 준 찾기 글을 SQL LIKE 꼴(역빗금으로 escape)로 바꾼다. 대소문자는 가리지 않는다.

    *와 %는 아무 글자열이고 그 밖의 글자(_ 포함)는 글자 그대로다. 와일드카드가 없으면 포함 검색이다.
    """
    raw = str(text or "").strip().lower()
    escaped = raw.replace("\\", "\\\\").replace("_", "\\_").replace("*", "%")
    return escaped if "%" in escaped else f"%{escaped}%"


def like_matches(value: str, pattern: str) -> bool:
    """like_pattern이 만든 꼴이 value(소문자로 맞춘다)에 맞는가. 메모리 저장소가 SQL과 같게 찾으려고 쓴다."""
    parts, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            parts.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        parts.append(".*" if char == "%" else "." if char == "_" else re.escape(char))
        index += 1
    return re.fullmatch("".join(parts), str(value or "").lower(), flags=re.DOTALL) is not None


class AccountStore(Protocol):
    def now(self) -> datetime: ...

    def add_user(self, user: dict[str, Any]) -> dict[str, Any]: ...
    def get_user(self, user_id: str) -> dict[str, Any] | None: ...
    def find_user(self, email: str) -> dict[str, Any] | None: ...
    def list_users(self, like: str | None = None) -> list[dict[str, Any]]: ...
    def set_password(self, user_id: str, password_hash: str) -> bool: ...
    def delete_user(self, user_id: str) -> bool: ...

    def add_membership(self, membership: dict[str, Any]) -> dict[str, Any]: ...
    def get_membership(self, user_id: str, org_id: str) -> dict[str, Any] | None: ...
    def list_memberships(
        self, *, user_id: str | None = None, org_id: str | None = None
    ) -> list[dict[str, Any]]: ...
    def set_member_role(self, user_id: str, org_id: str, role: str) -> dict[str, Any] | None: ...
    def remove_membership(self, user_id: str, org_id: str) -> bool: ...

    def add_invite(self, invite: dict[str, Any]) -> dict[str, Any]: ...
    def get_invite(self, token_hash: str) -> dict[str, Any] | None: ...
    def use_invite(self, token_hash: str, user_id: str, now: datetime) -> dict[str, Any] | None: ...
    def delete_invites(
        self, *, org_id: str | None = None, user_id: str | None = None, unused_only: bool = False
    ) -> int: ...


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

    def list_users(self, like: str | None = None) -> list[dict[str, Any]]:
        """사용자 목록(가입한 때 순). like를 주면 이메일이나 이름이 like_pattern에 맞는 사용자만."""
        pattern = like_pattern(like) if like else None
        with self._lock:
            found = [
                copy.deepcopy(user)
                for user in self._users.values()
                if pattern is None
                or like_matches(user["email"], pattern)
                or like_matches(user["name"], pattern)
            ]
        return sorted(found, key=lambda u: (u.get("createdAt") or "", u["id"]))

    def set_password(self, user_id: str, password_hash: str) -> bool:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user["passwordHash"] = password_hash
            return True

    def delete_user(self, user_id: str) -> bool:
        """사용자 레코드(이메일, 이름, 비밀번호 값)와 그 사람의 소속을 지운다. 없었으면 False."""
        with self._lock:
            user = self._users.pop(user_id, None)
            if user is None:
                return False
            self._emails.pop(user["email"], None)
            for key in [k for k in self._memberships if k[0] == user_id]:
                del self._memberships[key]
            return True

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

    def set_member_role(self, user_id: str, org_id: str, role: str) -> dict[str, Any] | None:
        """소속의 역할을 바꾼다. 소속이 없으면 None."""
        with self._lock:
            found = self._memberships.get((user_id, org_id))
            if found is None:
                return None
            found["memberRole"] = role
            return copy.deepcopy(found)

    def remove_membership(self, user_id: str, org_id: str) -> bool:
        with self._lock:
            return self._memberships.pop((user_id, org_id), None) is not None

    # ── 초대 ──────────────────────────────────────────────────────────
    def add_invite(self, invite: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._invites[invite["tokenHash"]] = copy.deepcopy(invite)
        return copy.deepcopy(invite)

    def get_invite(self, token_hash: str) -> dict[str, Any] | None:
        """초대를 읽기만 한다(쓰지 않는다). 없으면 None."""
        with self._lock:
            invite = self._invites.get(token_hash)
            return copy.deepcopy(invite) if invite else None

    def use_invite(self, token_hash: str, user_id: str, now: datetime) -> dict[str, Any] | None:
        """쓰지 않았고 만료되지 않은 초대면 쓴 것으로 적고 돌려준다. 아니면 None. 한 번만 쓸 수 있다."""
        with self._lock:
            invite = self._invites.get(token_hash)
            if invite is None or invite.get("usedAt") or parse_iso(invite["expiresAt"]) <= now:
                return None
            invite.update(usedAt=iso(now), usedBy=user_id)
            return copy.deepcopy(invite)

    def delete_invites(
        self, *, org_id: str | None = None, user_id: str | None = None, unused_only: bool = False
    ) -> int:
        """초대를 지운다. org_id는 그 조직의 초대, user_id는 그 사람이 만들었거나 쓴 초대로 좁힌다.

        unused_only면 아직 쓰지 않은 초대만 지운다. 지운 수를 준다.
        """
        with self._lock:
            doomed = [
                key
                for key, invite in self._invites.items()
                if invite_matches(invite, org_id=org_id, user_id=user_id, unused_only=unused_only)
            ]
            for key in doomed:
                del self._invites[key]
            return len(doomed)


def invite_matches(
    invite: dict[str, Any], *, org_id: str | None, user_id: str | None, unused_only: bool
) -> bool:
    if org_id is not None and invite.get("orgId") != org_id:
        return False
    if user_id is not None and user_id not in (invite.get("createdBy"), invite.get("usedBy")):
        return False
    return not (unused_only and invite.get("usedAt"))


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
