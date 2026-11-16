"""가장자리 두 곳(api.py, devserver.py)이 같이 쓰는 요청 읽기와 요청 오류(계약 8장).

두 서버가 같은 요청에 같은 오류 코드와 상태 코드로 답하게 하려고 한곳에 둔다.

| 코드 | 상태 | 조건 |
|---|---|---|
| INVALID_JSON | 400 | 본문이 JSON이 아니다 |
| INVALID_BODY | 422 | 본문이 JSON 객체가 아니거나 필수 항목이 없다 |
| FILE_REQUIRED | 422 | 올릴 파일이 없거나 비어 있다 |
| TOO_LARGE | 413 | 본문이 상한보다 크다 |
"""

from __future__ import annotations

import json
from typing import Any

from app.v3.service import ServiceError

MAX_BODY_BYTES = 20 * 1024 * 1024  # 요청 본문 상한(운영 한도)

INVALID_JSON = "INVALID_JSON"
INVALID_BODY = "INVALID_BODY"
FILE_REQUIRED = "FILE_REQUIRED"
TOO_LARGE = "TOO_LARGE"
REQUEST_ERRORS = {INVALID_JSON: 400, INVALID_BODY: 422, FILE_REQUIRED: 422, TOO_LARGE: 413}


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
