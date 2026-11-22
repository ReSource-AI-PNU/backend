"""업체가 자기 AI 에이전트를 MCP로 연결할 때 쓰는 키.

- 키 원문은 만들 때 한 번만 돌려준다. 저장하는 것은 sha256 값, 앞 12자(화면 표시용), 이름, 만든 때, 마지막 사용.
- 키 하나는 업체 조직 하나에 묶인다. MCP 요청의 조직은 키가 정한다(호출자가 org_id를 넣지 않는다).
- 업체 프로필의 mcpKeys 칸에 둔다. GET /vendors가 이 칸을 내보내지 않는다(service.get_vendor).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

PREFIX = "rsai_"
LIMIT = 5
PATH = "/mcp/vendor"
LABEL_MAX = 40


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(key: dict[str, Any]) -> dict[str, Any]:
    return {k: key.get(k) for k in ("id", "label", "prefix", "createdAt", "lastUsedAt")}


def _profile(app: Any, org_id: str) -> dict[str, Any]:
    from app.v3.onboarding.agent import _profile as load

    return load(app, org_id)


def listing(app: Any, org_id: str) -> dict[str, Any]:
    keys = _profile(app, org_id).get("mcpKeys") or []
    return {"path": PATH, "limit": LIMIT, "keys": [_public(k) for k in keys]}


def create(app: Any, org_id: str, label: Any) -> dict[str, Any]:
    from app.v3.service import ServiceError

    name = str(label or "").strip()
    if not name or len(name) > LABEL_MAX:
        raise ServiceError("INVALID_VALUE", f"연결 이름을 1~{LABEL_MAX}자로 적어 주세요.", 422)
    profile = _profile(app, org_id)
    keys = list(profile.get("mcpKeys") or [])
    if len(keys) >= LIMIT:
        raise ServiceError(
            "KEY_LIMIT",
            f"연결 키는 {LIMIT}개까지 만들 수 있습니다. 쓰지 않는 키를 먼저 끊어 주세요.",
            409,
        )
    token = PREFIX + secrets.token_urlsafe(32)
    key = {
        "id": app.store.next_id("mk"),
        "label": name,
        "prefix": token[:12],
        "hash": _hash(token),
        "createdAt": app.store.now(),
        "lastUsedAt": None,
    }
    profile["mcpKeys"] = [*keys, key]
    app.store.put_vendor_profile(profile)
    return {"path": PATH, "key": _public(key), "token": token}


def revoke(app: Any, org_id: str, key_id: str) -> dict[str, Any]:
    from app.v3.service import ServiceError

    profile = _profile(app, org_id)
    keys = list(profile.get("mcpKeys") or [])
    kept = [k for k in keys if k.get("id") != key_id]
    if len(kept) == len(keys):
        raise ServiceError("KEY_NOT_FOUND", "그런 연결 키가 없습니다.", 404)
    profile["mcpKeys"] = kept
    app.store.put_vendor_profile(profile)
    return listing(app, org_id)


def find(app: Any, token: str) -> tuple[str, dict[str, Any]] | None:
    """키 원문 → (업체 조직 ID, 키 기록). 맞는 키가 없으면 None. 찾으면 마지막 사용 때를 적는다."""
    if not token.startswith(PREFIX):
        return None
    wanted = _hash(token)
    for profile in app.store.list_vendor_profiles():
        for key in profile.get("mcpKeys") or []:
            if hmac.compare_digest(str(key.get("hash") or ""), wanted):
                key["lastUsedAt"] = app.store.now()
                app.store.put_vendor_profile(profile)
                return profile["orgId"], key
    return None


# ── 서비스 입구(api.py, devserver.py) ────────────────────────────────
def get_keys(app: Any, org_id: str, viewer: str) -> dict[str, Any]:
    from app.v3 import service

    service._owner(app, org_id, viewer, "vendor")
    return listing(app, org_id)


def post_key(app: Any, org_id: str, body: dict[str, Any], viewer: str) -> dict[str, Any]:
    from app.v3 import service

    service._owner(app, org_id, viewer, "vendor")
    return create(app, org_id, (body or {}).get("label") if isinstance(body, dict) else None)


def delete_key(app: Any, org_id: str, key_id: str, viewer: str) -> dict[str, Any]:
    from app.v3 import service

    service._owner(app, org_id, viewer, "vendor")
    return revoke(app, org_id, key_id)
