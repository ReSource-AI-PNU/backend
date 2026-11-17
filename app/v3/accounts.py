"""계정, 조직, 소속, 초대, 체험하기(V3_AUTH=bff). 프레임워크와 무관하고 api.py만 부른다.

함수는 service.py처럼 fn(app, ...) 꼴이고 권한이 없으면 ServiceError를 낸다. 계약은 CONTRACT 9장이다.

- 비밀번호는 hashlib.scrypt(n=2**14, r=8, p=1, 사용자마다 16바이트 소금)로 저장하고 응답·기록에 싣지
  않는다. 확인은 hmac.compare_digest로 하고, 없는 이메일도 같은 계산을 한 번 해 시간 차를 줄인다.
- 로그인 실패는 이메일마다 15분에 10번까지다(app.login_limiter, 프로세스 메모리). 넘으면 429.
- 조직 레코드는 기존 저장소(app.store)의 orgs에 둔다. role이 계약의 kind다. 새 조직의 ID는
  org_ + 난수 12자라 시연 조직의 ID(org_hanbit 등)와 겹치지 않는다.
- 초대 토큰은 32바이트 난수이고 sha256 값만 저장한다. 7일 뒤 만료되고 한 번만 쓸 수 있다.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from typing import Any

from app.v3.account_store import EmailTaken, iso, parse_iso
from app.v3.service import App, ServiceError
from app.v3.store import NotFound

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SALT_BYTES, KEY_BYTES = 16, 32
MIN_PASSWORD, MAX_PASSWORD = 8, 256
INVITE_DAYS = 7
ORG_KINDS = ("emitter", "vendor")
MEMBER_ROLES = ("owner", "member")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_BIZ_NO = re.compile(r"(\d{3})-?(\d{2})-?(\d{5})")


# ── 비밀번호 ─────────────────────────────────────────────────────────
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=KEY_BYTES
    )


def hash_password(password: str) -> str:
    """scrypt$n$r$p$소금$값 꼴. 소금은 사용자마다 새로 뽑는다."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


@functools.cache
def _dummy_hash() -> str:
    """없는 이메일로 로그인할 때도 같은 계산을 하려고 쓰는 값(어느 비밀번호와도 맞지 않는다)."""
    return hash_password(secrets.token_urlsafe(24))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = _scrypt(password, _unb64(salt), int(n), int(r), int(p))
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


# ── 입력 확인 ────────────────────────────────────────────────────────
def _invalid(message: str) -> ServiceError:
    return ServiceError("INVALID_VALUE", message, 422)


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _email(value: Any) -> str:
    email = normalize_email(value)
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise _invalid("이메일 주소의 꼴이 맞지 않습니다.")
    return email


def _text(body: dict[str, Any], key: str, label: str, limit: int, *, needed: bool) -> str | None:
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if needed:
            raise _invalid(f"{label}을 적어 주세요.")
        return None
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise _invalid(f"{label}은 {limit}자 이하의 글이어야 합니다.")
    return value.strip()


def _password(value: Any) -> str:
    if not isinstance(value, str) or not MIN_PASSWORD <= len(value) <= MAX_PASSWORD:
        raise _invalid(f"비밀번호는 {MIN_PASSWORD}자 이상 {MAX_PASSWORD}자 이하여야 합니다.")
    return value


def _location(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _invalid("위치는 [경도, 위도] 두 숫자여야 합니다.")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise _invalid("위치는 [경도, 위도] 두 숫자여야 합니다.")
    lng, lat = float(value[0]), float(value[1])
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise _invalid("위치의 경도는 -180~180, 위도는 -90~90 사이여야 합니다.")
    return [lng, lat]


def _biz_no(value: Any) -> str | None:
    if value is None or value == "":
        return None
    found = _BIZ_NO.fullmatch(str(value).strip())
    if not found:
        raise _invalid("사업자등록번호는 숫자 10자리(000-00-00000)여야 합니다.")
    return "-".join(found.groups())


# ── 보기 ─────────────────────────────────────────────────────────────
def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """밖으로 내는 사용자. 비밀번호 값은 싣지 않는다."""
    return {"id": user["id"], "email": user["email"], "name": user["name"]}


def org_view(org: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": org["id"],
        "name": org.get("name"),
        "kind": org.get("role"),
        "bizNo": org.get("bizNo"),
        "region": org.get("region"),
        "address": org.get("address"),
        "location": org.get("location"),
        "demo": bool(org.get("demo")),
        "createdAt": org.get("createdAt"),
    }


def _membership_view(app: App, membership: dict[str, Any]) -> dict[str, Any] | None:
    try:
        org = app.store.get_org(membership["orgId"])
    except NotFound:
        return None
    return {
        "orgId": org["id"],
        "orgName": org.get("name"),
        "kind": org.get("role"),
        "memberRole": membership["memberRole"],
    }


def _account(app: App, user: dict[str, Any]) -> dict[str, Any]:
    memberships = [
        view
        for item in app.accounts.list_memberships(user_id=user["id"])
        if (view := _membership_view(app, item)) is not None
    ]
    return {"user": public_user(user), "memberships": memberships}


# ── 확인 (api.py가 요청마다 부른다) ──────────────────────────────────
def _user(app: App, user_id: str | None) -> dict[str, Any]:
    if not user_id:
        raise ServiceError("USER_REQUIRED", "X-User-Id로 사용자를 밝혀야 합니다.", 401)
    user = app.accounts.get_user(user_id)
    if user is None:
        raise ServiceError("UNKNOWN_USER", "알 수 없는 사용자입니다.", 401)
    return user


def check_member(app: App, user_id: str, org_id: str) -> dict[str, Any]:
    """user_id가 org_id의 구성원이면 소속을 돌려준다. 아니면 403(조직이 있는지도 알려 주지 않는다)."""
    _user(app, user_id)
    membership = app.accounts.get_membership(user_id, org_id) if org_id else None
    if membership is None:
        raise ServiceError("NOT_A_MEMBER", "그 조직의 구성원이 아닙니다.", 403)
    return membership


def check_user(app: App, user_id: str) -> dict[str, Any]:
    return public_user(_user(app, user_id))


def is_demo_org(app: App, org_id: str) -> bool:
    try:
        return app.store.get_org(org_id).get("demo") is True
    except NotFound:
        return False


# ── 계정 ─────────────────────────────────────────────────────────────
def signup(app: App, body: dict[str, Any]) -> dict[str, Any]:
    email = _email(body.get("email"))
    password = _password(body.get("password"))
    name = _text(body, "name", "이름", 100, needed=True)
    user = {
        "id": f"usr_{secrets.token_hex(8)}",
        "email": email,
        "name": name,
        "passwordHash": hash_password(password),
        "createdAt": iso(app.accounts.now()),
    }
    try:
        app.accounts.add_user(user)
    except EmailTaken:
        raise ServiceError("EMAIL_TAKEN", "이미 가입한 이메일입니다.", 409) from None
    return {"user": public_user(user)}


def login(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """이메일이나 비밀번호가 틀리면 어느 쪽이 틀렸는지 알려 주지 않는다(같은 코드와 문장)."""
    email = normalize_email(body.get("email"))
    password = body.get("password")
    if app.login_limiter.blocked(email):
        raise ServiceError(
            "TOO_MANY_ATTEMPTS", "로그인 시도가 너무 많습니다. 잠시 뒤에 다시 해 주세요.", 429
        )
    user = app.accounts.find_user(email) if email else None
    given = password if isinstance(password, str) and len(password) <= MAX_PASSWORD else ""
    matched = verify_password(given, user["passwordHash"] if user else _dummy_hash())
    if user is None or not given or not matched:
        app.login_limiter.failed(email)
        raise ServiceError("INVALID_CREDENTIALS", "이메일이나 비밀번호가 맞지 않습니다.", 401)
    app.login_limiter.succeeded(email)
    return _account(app, user)


def me(app: App, user_id: str) -> dict[str, Any]:
    return _account(app, _user(app, user_id))


# ── 조직 ─────────────────────────────────────────────────────────────
def _new_org_id(app: App) -> str:
    while True:
        org_id = f"org_{secrets.token_hex(6)}"
        try:
            app.store.get_org(org_id)
        except NotFound:
            return org_id


def create_org(app: App, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """조직을 만들고 만든 사람을 owner로 넣는다. 배출 기업은 흐름 없이, 업체는 빈 프로필로 시작한다."""
    _user(app, user_id)
    name = _text(body, "name", "조직 이름", 100, needed=True)
    kind = body.get("kind")
    if kind not in ORG_KINDS:
        raise _invalid("조직 종류(kind)는 emitter 또는 vendor여야 합니다.")
    org = {
        "id": _new_org_id(app),
        "name": name,
        "role": kind,
        "bizNo": _biz_no(body.get("bizNo")),
        "region": _text(body, "region", "지역", 200, needed=False),
        "address": _text(body, "address", "주소", 200, needed=False),
        "location": _location(body.get("location")),
        "demo": False,
        "createdAt": iso(app.accounts.now()),
    }
    app.store.put_org(org)
    if kind == "vendor":
        # 업체 화면의 PUT /vendors/{orgId}와 문서 올리기가 이 프로필을 채운다. 허가 품목이나 매입 기준이
        # 생기기 전에는 업체 탐색에 나오지 않는다(orchestrator.discoverable_profiles)
        app.store.put_vendor_profile(
            {"orgId": org["id"], "permits": [], "criteria": [], "fleet": {}, "guardrails": {}}
        )
    app.accounts.add_membership(
        {
            "userId": user_id,
            "orgId": org["id"],
            "memberRole": "owner",
            "createdAt": org["createdAt"],
        }
    )
    return {**org_view(org), "memberRole": "owner"}


def list_members(app: App, user_id: str, org_id: str) -> list[dict[str, Any]]:
    check_member(app, user_id, org_id)
    out = []
    for membership in app.accounts.list_memberships(org_id=org_id):
        user = app.accounts.get_user(membership["userId"])
        if user is None:
            continue
        out.append(
            {
                "userId": user["id"],
                "name": user["name"],
                "email": user["email"],
                "memberRole": membership["memberRole"],
            }
        )
    return out


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_invite(app: App, user_id: str, org_id: str) -> dict[str, Any]:
    membership = check_member(app, user_id, org_id)
    if membership["memberRole"] != "owner":
        raise ServiceError("OWNER_ONLY", "조직 소유자만 초대할 수 있습니다.", 403)
    token = secrets.token_urlsafe(32)
    now = app.accounts.now()
    expires = iso(now + timedelta(days=INVITE_DAYS))
    app.accounts.add_invite(
        {
            "tokenHash": _token_hash(token),
            "orgId": org_id,
            "createdBy": user_id,
            "createdAt": iso(now),
            "expiresAt": expires,
            "usedAt": None,
            "usedBy": None,
        }
    )
    return {"token": token, "expiresAt": expires}


def peek_invite(app: App, token: str) -> dict[str, Any]:
    """초대 화면이 보일 초대한 조직. 로그인하지 않은 사람도 본다(BFF 키만 있으면 된다).

    {orgName, kind, expiresAt, valid: true}만 준다. 조직 ID, 만든 사람, 구성원은 싣지 않는다. 없거나 만료됐거나
    이미 쓴 초대는 accept_invite와 같은 410 INVITE_INVALID다(어느 쪽인지 밝히지 않는다).
    """
    invalid = ServiceError("INVITE_INVALID", "초대가 만료되었거나 이미 쓰였습니다.", 410)
    if not token or len(token) > 200:
        raise invalid
    invite = app.accounts.get_invite(_token_hash(token))
    if (
        invite is None
        or invite.get("usedAt")
        or parse_iso(invite["expiresAt"]) <= app.accounts.now()
    ):
        raise invalid
    try:
        org = app.store.get_org(invite["orgId"])
    except NotFound:
        raise invalid from None
    return {
        "orgName": org.get("name"),
        "kind": org.get("role"),
        "expiresAt": invite["expiresAt"],
        "valid": True,
    }


def accept_invite(app: App, user_id: str, token: str) -> dict[str, Any]:
    """초대를 받아 member로 들어간다. 만료되었거나 이미 쓴 초대면 410. 이미 구성원이면 소속을 그대로 준다."""
    _user(app, user_id)
    if not token or len(token) > 200:
        raise ServiceError("INVITE_INVALID", "초대가 만료되었거나 이미 쓰였습니다.", 410)
    invite = app.accounts.use_invite(_token_hash(token), user_id, app.accounts.now())
    if invite is None:
        raise ServiceError("INVITE_INVALID", "초대가 만료되었거나 이미 쓰였습니다.", 410)
    membership = app.accounts.add_membership(
        {
            "userId": user_id,
            "orgId": invite["orgId"],
            "memberRole": "member",
            "createdAt": iso(app.accounts.now()),
        }
    )
    view = _membership_view(app, membership)
    if view is None:
        raise ServiceError("INVITE_INVALID", "초대가 만료되었거나 이미 쓰였습니다.", 410)
    return view


# ── 체험하기 ─────────────────────────────────────────────────────────
def demo_orgs(app: App) -> list[dict[str, Any]]:
    """체험하기로 볼 수 있는 시연 조직 [{id, name, kind}]. 조직 목록과 같은 순서(order)다."""

    def position(org: dict[str, Any]) -> tuple[float, str]:
        order = org.get("order")
        known = isinstance(order, (int, float)) and not isinstance(order, bool)
        return (float(order) if known else float("inf"), str(org["id"]))

    found = [org for org in app.store.list_orgs() if org.get("demo") is True]
    return [
        {"id": org["id"], "name": org.get("name"), "kind": org.get("role")}
        for org in sorted(found, key=position)
    ]


def grant_membership(app: App, email: str, org_id: str, member_role: str = "owner") -> dict:
    """운영자가 스크립트로 사용자를 조직에 넣는다(예: 관리자 조직 org_admin). HTTP로는 열지 않는다."""
    if member_role not in MEMBER_ROLES:
        raise ValueError("memberRole은 owner 또는 member여야 한다")
    user = app.accounts.find_user(normalize_email(email))
    if user is None:
        raise ValueError("그 이메일로 가입한 사용자가 없다")
    try:
        app.store.get_org(org_id)
    except NotFound:
        raise ValueError(f"조직 {org_id}가 없다(시연 데이터를 먼저 올린다)") from None
    membership = app.accounts.add_membership(
        {
            "userId": user["id"],
            "orgId": org_id,
            "memberRole": member_role,
            "createdAt": iso(app.accounts.now()),
        }
    )
    return _membership_view(app, membership) or {}
