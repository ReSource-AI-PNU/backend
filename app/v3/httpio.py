"""가장자리 두 곳(api.py, devserver.py)이 같이 쓰는 요청 읽기와 요청 오류(계약 8장).

두 서버가 같은 요청에 같은 오류 코드와 상태 코드로 답하게 하려고 한곳에 둔다.

| 코드 | 상태 | 조건 |
|---|---|---|
| INVALID_JSON | 400 | 본문이 JSON이 아니다 |
| INVALID_BODY | 422 | 본문이 JSON 객체가 아니거나 필수 항목이 없다 |
| FILE_REQUIRED | 422 | 올릴 파일이 없거나 비어 있다 |
| TOO_LARGE | 413 | 본문이 상한보다 크다 |
| TOKEN_REQUIRED | 401 | token 모드인데 Authorization: Bearer 헤더가 없거나 모양이 틀렸다 |
| INVALID_TOKEN | 401 | token 모드인데 토큰이 어느 조직의 것도 아니다 |
| BFF_KEY_NOT_SET | 503 | bff 모드인데 V3_BFF_KEY가 비어 있다(모든 요청을 거절한다) |
| INVALID_BFF_KEY | 401 | bff 모드인데 Authorization: Bearer 키가 없거나 맞지 않다 |

업체 측 에이전트 서비스(vendor_service.py, vendor_devserver.py)의 서비스 키와 본문 확인도 여기에 둔다:
SERVICE_KEY_NOT_SET 503, UNAUTHORIZED 401, VENDOR_NOT_SET 503, WRONG_RECIPIENT 403.

요청한 조직을 정하는 일(Auth)도 여기에 둔다. 세 가지 모드가 있다.

- header(기본, 시연 전용): X-Org-Id 헤더를 그대로 믿는다. 헤더만 바꾸면 다른 조직이 되므로 공개
  주소에 그대로 두면 조직 경계가 지켜지지 않는다.
- token: 조직별 서비스 토큰. Authorization: Bearer <토큰>으로 조직을 정하고 X-Org-Id는 무시한다.
  토큰과 조직의 짝은 설정 V3_ORG_TOKENS(JSON, {"조직 ID": "토큰"} 또는 {"조직 ID": ["토큰", …]})로
  받는다(Key Vault에서 주입). 비교는 hmac.compare_digest로 한다.
- bff: 프런트 서버(Next.js BFF)만 부른다. Authorization: Bearer <V3_BFF_KEY>로 BFF임을 확인하고,
  사용자는 X-User-Id, 조직은 X-Org-Id로 받는다. 사용자가 그 조직의 구성원인지는 api.py가 계정
  저장소로 확인한다(여기서는 키와 헤더만 읽는다). 로컬 서버(devserver)는 이 모드를 쓰지 않는다.
  X-Demo-View: 1이면 체험하기(시연 조직 읽기 전용)다.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.v3.service import ServiceError

MAX_BODY_BYTES = 20 * 1024 * 1024  # 요청 본문 상한(운영 한도)

INVALID_JSON = "INVALID_JSON"
INVALID_BODY = "INVALID_BODY"
FILE_REQUIRED = "FILE_REQUIRED"
TOO_LARGE = "TOO_LARGE"
REQUEST_ERRORS = {INVALID_JSON: 400, INVALID_BODY: 422, FILE_REQUIRED: 422, TOO_LARGE: 413}

# ── 요청한 조직 ──────────────────────────────────────────────────────
AUTH_HEADER, AUTH_TOKEN, AUTH_BFF = "header", "token", "bff"
AUTH_MODES = (AUTH_HEADER, AUTH_TOKEN, AUTH_BFF)
ORG_HEADER = "X-Org-Id"
USER_HEADER = "X-User-Id"
DEMO_HEADER = "X-Demo-View"
TOKEN_REQUIRED = "TOKEN_REQUIRED"
INVALID_TOKEN = "INVALID_TOKEN"
MIN_TOKEN_CHARS = 16  # 이보다 짧은 토큰은 설정 오류로 본다
HEADER_MODE_WARNING = (
    "header 모드는 시연 전용이며 공개 주소에 그대로 두면 조직 경계가 지켜지지 않는다"
)


def header(headers: Mapping[str, str] | None, name: str) -> str:
    """헤더 값(앞뒤 공백 제거). 대소문자를 가리지 않고 찾는다. 없으면 빈 문자열."""
    if headers is None:
        return ""
    found = headers.get(name)
    if found is None:
        found = headers.get(name.lower())
    if found is None:
        wanted = name.lower()
        found = next((v for k, v in headers.items() if str(k).lower() == wanted), None)
    return str(found or "").strip()


def org_tokens(raw: Any) -> tuple[tuple[str, str], ...]:
    """V3_ORG_TOKENS를 (조직 ID, 토큰) 목록으로 읽는다. 모양이 틀리면 ValueError.

    JSON 글이나 이미 읽은 객체를 받는다: {"조직 ID": "토큰"} 또는 {"조직 ID": ["토큰", …]}(교체 기간에
    두 개를 함께 둘 때). 토큰은 16자 이상이어야 하고 두 조직이 같은 토큰을 쓸 수 없다.
    오류 문장에는 토큰 값을 싣지 않는다.
    """
    if raw is None or raw == "":
        return ()
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("V3_ORG_TOKENS가 JSON이 아니다") from None
    if not isinstance(raw, Mapping):
        raise ValueError('V3_ORG_TOKENS는 {"조직 ID": "토큰"} 꼴의 객체여야 한다')  # noqa: TRY004
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for org_id, value in raw.items():
        org = str(org_id).strip()
        tokens = value if isinstance(value, (list, tuple)) else [value]
        if not org or not tokens:
            raise ValueError("V3_ORG_TOKENS에 조직 ID나 토큰이 빈 항목이 있다")
        for token in tokens:
            if not isinstance(token, str) or len(token.strip()) < MIN_TOKEN_CHARS:
                raise ValueError(f"{org}의 토큰은 {MIN_TOKEN_CHARS}자 이상의 글이어야 한다")
            token = token.strip()
            if token in seen:
                raise ValueError(f"{org}의 토큰이 다른 항목과 같다. 조직마다 다른 토큰을 쓴다")
            seen.add(token)
            pairs.append((org, token))
    return tuple(pairs)


def match_token(presented: str, tokens: tuple[tuple[str, str], ...]) -> str | None:
    """토큰의 조직 ID. 맞는 것이 없으면 None. 모든 항목을 hmac.compare_digest로 끝까지 견준다."""
    given = presented.encode("utf-8")
    found: str | None = None
    for org_id, token in tokens:
        if hmac.compare_digest(token.encode("utf-8"), given) and found is None:
            found = org_id
    return found


@dataclass(frozen=True)
class BffIdentity:
    """bff 모드에서 BFF가 밝힌 사용자, 조직, 체험하기 여부. 소속 확인 전의 값이다."""

    user_id: str
    org_id: str
    demo: bool


@dataclass(frozen=True)
class Auth:
    """요청한 조직을 정하는 방법. header(X-Org-Id를 믿는다, 시연 전용) | token(조직별 서비스 토큰)
    | bff(프런트 서버가 사용자와 조직을 밝힌다. 소속은 api.py가 확인한다)."""

    mode: str = AUTH_HEADER
    tokens: tuple[tuple[str, str], ...] = ()
    bff_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.mode not in AUTH_MODES:
            raise ValueError(
                f"V3_AUTH는 {' | '.join(AUTH_MODES)} 가운데 하나여야 한다: {self.mode!r}"
            )
        if self.mode == AUTH_TOKEN and not self.tokens:
            raise ValueError("V3_AUTH=token이면 V3_ORG_TOKENS에 조직별 토큰이 있어야 한다")

    @property
    def scoped(self) -> bool:
        """조직 목록(GET /orgs)을 자기 조직과 대화 상대로 좁히는가."""
        return self.mode in (AUTH_TOKEN, AUTH_BFF)

    def bff(self, headers: Mapping[str, str] | None) -> BffIdentity:
        """bff 모드: BFF 키를 확인하고 X-User-Id, X-Org-Id, X-Demo-View를 읽는다.

        키가 설정되지 않았으면 모든 요청을 503으로 거절한다. 키 비교는 hmac.compare_digest로 한다.
        """
        if self.mode != AUTH_BFF:
            raise ValueError("bff 모드가 아니다")
        if not self.bff_key:
            raise ServiceError("BFF_KEY_NOT_SET", "V3_BFF_KEY가 설정되지 않았습니다.", 503)
        scheme, _, given = header(headers, "Authorization").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode("utf-8"), self.bff_key.encode("utf-8")
        ):
            raise ServiceError("INVALID_BFF_KEY", "프런트 서버 키가 맞지 않습니다.", 401)
        return BffIdentity(
            user_id=header(headers, USER_HEADER),
            org_id=header(headers, ORG_HEADER),
            demo=header(headers, DEMO_HEADER) == "1",
        )

    def viewer(self, headers: Mapping[str, str] | None, *, required: bool = False) -> str:
        """요청한 조직 ID. 밝히지 않았으면 빈 문자열(required면 401).

        token 모드에서는 X-Org-Id를 보지 않는다. Authorization 헤더가 있는데 Bearer 꼴이 아니거나
        토큰이 틀리면 required가 아니어도 401이다(틀린 토큰으로 익명 경로를 타지 못하게 한다).
        bff 모드에서는 쓰지 않는다(소속 확인 없이 X-Org-Id를 믿게 되므로 RuntimeError).
        """
        if self.mode == AUTH_BFF:
            raise RuntimeError("bff 모드의 조직은 api.py가 소속을 확인한 뒤 정한다")
        if self.mode == AUTH_HEADER:
            org_id = header(headers, ORG_HEADER)
            if not org_id and required:
                raise ServiceError("ORG_REQUIRED", "X-Org-Id 헤더로 조직을 밝혀야 합니다.", 401)
            return org_id
        raw = header(headers, "Authorization")
        if not raw:
            if required:
                raise ServiceError(
                    TOKEN_REQUIRED, "Authorization: Bearer 토큰으로 조직을 밝혀야 합니다.", 401
                )
            return ""
        scheme, _, presented = raw.partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented:
            raise ServiceError(
                TOKEN_REQUIRED, "Authorization 헤더는 Bearer 토큰이어야 합니다.", 401
            )
        org_id = match_token(presented, self.tokens)
        if org_id is None:
            raise ServiceError(INVALID_TOKEN, "토큰이 맞지 않습니다.", 401)
        return org_id


def make_auth(mode: str | None, raw_tokens: Any = None, bff_key: str | None = None) -> Auth:
    """설정 값(V3_AUTH, V3_ORG_TOKENS, V3_BFF_KEY)으로 Auth를 만든다.

    header 모드에서는 토큰 설정을 읽지 않는다. bff 모드는 키가 비어 있어도 만들어진다(요청마다 503).
    """
    mode = (mode or AUTH_HEADER).strip().lower()
    if mode not in AUTH_MODES:
        raise ValueError(f"V3_AUTH는 {' | '.join(AUTH_MODES)} 가운데 하나여야 한다: {mode!r}")
    key = (bff_key or "").strip() if mode == AUTH_BFF else ""
    return Auth(mode, org_tokens(raw_tokens) if mode == AUTH_TOKEN else (), key)


def tokens_text(raw: Any = None, path: str | None = None) -> Any:
    """token 모드의 토큰 설정을 어디서 읽을지 정한다: V3_ORG_TOKENS_FILE(파일 경로)이 있으면 그 파일의
    내용, 없으면 V3_ORG_TOKENS 값 그대로. 로컬 서버와 FastAPI 경로가 같은 순서로 읽는다.

    파일을 읽지 못하면 ValueError다. 문구에는 변수 이름만 싣고 경로와 내용은 싣지 않는다.
    """
    path = (path or "").strip()
    if not path:
        return raw
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        raise ValueError("V3_ORG_TOKENS_FILE이 가리키는 파일을 읽을 수 없습니다.") from None


def extra_origins(raw: str | None) -> list[str]:
    """V3_CORS_ORIGINS(쉼표로 나눈 출처)를 목록으로. http(s)://호스트[:포트] 꼴만 받고 아니면 ValueError.

    끝의 빗금은 떼고 같은 출처는 한 번만 남긴다. 비어 있으면 빈 목록이다.
    """
    found: list[str] = []
    for origin in (raw or "").split(","):
        origin = origin.strip().rstrip("/")
        if not origin:
            continue
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", origin):
            raise ValueError(f"허용 출처는 http(s)://호스트[:포트] 꼴이어야 합니다: {origin}")
        if origin not in found:
            found.append(origin)
    return found


# ── 업체 측 에이전트 서비스(POST /a2a/messages) ───────────────────────
# 오류 코드는 글자 그대로 적는다(계약 8장의 오류 코드 표를 확인하는 테스트가 코드에서 글자로 찾는다)
def check_service_key(headers: Mapping[str, str] | None, expected: str | None) -> None:
    """Authorization: Bearer <서비스 키>를 확인한다. 키가 설정되지 않았으면 모든 요청을 거절한다(503)."""
    if not expected:
        raise ServiceError("SERVICE_KEY_NOT_SET", "서비스 키가 설정되지 않았습니다.", 503)
    scheme, _, token = header(headers, "Authorization").partition(" ")
    given = token.strip()
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        given.encode("utf-8"), str(expected).encode("utf-8")
    ):
        raise ServiceError("UNAUTHORIZED", "서비스 키가 맞지 않습니다.", 401)


def vendor_org_required(vendor_org_id: str | None) -> str:
    """이 서비스가 대변하는 업체. 설정되지 않았으면 503."""
    if not vendor_org_id:
        raise ServiceError("VENDOR_NOT_SET", "VENDOR_ORG_ID가 설정되지 않았습니다.", 503)
    return str(vendor_org_id)


def vendor_request(
    body: dict[str, Any], vendor_org_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(message, context). 모양이 틀리면 422, 다른 업체에게 온 메시지면 403."""
    message = body.get("message")
    context = body.get("context")
    if context is None:
        context = {}
    if not isinstance(message, dict) or not isinstance(context, dict):
        raise invalid_body("message와 context는 JSON 객체여야 합니다.")
    if message.get("to") != vendor_org_id:
        raise ServiceError(
            "WRONG_RECIPIENT", "이 서비스가 대변하는 업체에게 온 메시지가 아닙니다.", 403
        )
    return message, context


def invalid_json() -> ServiceError:
    return ServiceError(INVALID_JSON, "본문이 JSON이 아닙니다.", REQUEST_ERRORS[INVALID_JSON])


def invalid_body(message: str) -> ServiceError:
    return ServiceError(INVALID_BODY, message, REQUEST_ERRORS[INVALID_BODY])


def file_required() -> ServiceError:
    return ServiceError(FILE_REQUIRED, "올릴 파일이 없습니다.", REQUEST_ERRORS[FILE_REQUIRED])


def too_large() -> ServiceError:
    return ServiceError(TOO_LARGE, "본문이 너무 큽니다.", REQUEST_ERRORS[TOO_LARGE])


def parse_object(raw: bytes) -> dict[str, Any]:
    """본문을 JSON 객체로 읽는다. 빈 본문은 빈 객체다."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise invalid_json() from None
    if not isinstance(data, dict):
        raise invalid_body("본문은 JSON 객체여야 합니다.")
    return data


def required(body: dict[str, Any], key: str) -> Any:
    if body.get(key) in (None, ""):
        raise invalid_body(f"필수 항목이 없습니다: {key}")
    return body[key]


def answer_body(body: dict[str, Any]) -> tuple[str, Any]:
    """(questionId, value). 모르면 value에 "unknown"을 보낸다."""
    if body.get("questionId") in (None, "") or body.get("value") is None:
        raise invalid_body('questionId와 value를 보내 주세요(모르면 value에 "unknown").')
    return str(body["questionId"]), body["value"]


def check_upload(data: bytes | None) -> bytes:
    if not data:
        raise file_required()
    if len(data) > MAX_BODY_BYTES:
        raise too_large()
    return data
