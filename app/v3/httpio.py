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

요청한 조직을 정하는 일(Auth)도 여기에 둔다. 두 가지 모드가 있다.

- header(기본, 시연 전용): X-Org-Id 헤더를 그대로 믿는다. 헤더만 바꾸면 다른 조직이 되므로 공개
  주소에 그대로 두면 조직 경계가 지켜지지 않는다.
- token: 조직별 서비스 토큰. Authorization: Bearer <토큰>으로 조직을 정하고 X-Org-Id는 무시한다.
  토큰과 조직의 짝은 설정 V3_ORG_TOKENS(JSON, {"조직 ID": "토큰"} 또는 {"조직 ID": ["토큰", …]})로
  받는다(Key Vault에서 주입). 비교는 hmac.compare_digest로 한다.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.v3.service import ServiceError

MAX_BODY_BYTES = 20 * 1024 * 1024  # 요청 본문 상한(운영 한도)

INVALID_JSON = "INVALID_JSON"
INVALID_BODY = "INVALID_BODY"
FILE_REQUIRED = "FILE_REQUIRED"
TOO_LARGE = "TOO_LARGE"
REQUEST_ERRORS = {INVALID_JSON: 400, INVALID_BODY: 422, FILE_REQUIRED: 422, TOO_LARGE: 413}

# ── 요청한 조직 ──────────────────────────────────────────────────────
AUTH_HEADER, AUTH_TOKEN = "header", "token"
AUTH_MODES = (AUTH_HEADER, AUTH_TOKEN)
ORG_HEADER = "X-Org-Id"
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
class Auth:
    """요청한 조직을 정하는 방법. header(X-Org-Id를 믿는다, 시연 전용) | token(조직별 서비스 토큰)."""

    mode: str = AUTH_HEADER
    tokens: tuple[tuple[str, str], ...] = ()

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
        return self.mode == AUTH_TOKEN

    def viewer(self, headers: Mapping[str, str] | None, *, required: bool = False) -> str:
        """요청한 조직 ID. 밝히지 않았으면 빈 문자열(required면 401).

        token 모드에서는 X-Org-Id를 보지 않는다. Authorization 헤더가 있는데 Bearer 꼴이 아니거나
        토큰이 틀리면 required가 아니어도 401이다(틀린 토큰으로 익명 경로를 타지 못하게 한다).
        """
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


def make_auth(mode: str | None, raw_tokens: Any = None) -> Auth:
    """설정 값(V3_AUTH, V3_ORG_TOKENS)으로 Auth를 만든다. header 모드에서는 토큰 설정을 읽지 않는다."""
    mode = (mode or AUTH_HEADER).strip().lower()
    if mode not in AUTH_MODES:
        raise ValueError(f"V3_AUTH는 {' | '.join(AUTH_MODES)} 가운데 하나여야 한다: {mode!r}")
    return Auth(mode, org_tokens(raw_tokens) if mode == AUTH_TOKEN else ())


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
