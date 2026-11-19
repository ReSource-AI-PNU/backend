"""intake 모듈이 함께 쓰는 HTTP 호출. 테스트에서는 transport를 가짜로 바꾼다."""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

# transport(method, url, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]

TIMEOUT = 30
# 같은 Docker 네트워크 안의 서비스(앱 VM의 detector 등). CPU 추론이라 영상은 30초를 넘을 수 있다
INTERNAL_TIMEOUT = 180


def internal_hosts(env: dict[str, str] | None = None) -> frozenset[str]:
    """V3_INTERNAL_HTTP_HOSTS(쉼표로 나눔)에 적힌 내부 서비스 이름. 점이 없는 한 단어 이름만 받는다.

    점이 있는 이름(공개 도메인)은 적어도 무시한다. 바깥 주소로는 키를 평문으로 보내지 않는다.
    """
    raw = (env if env is not None else os.environ).get("V3_INTERNAL_HTTP_HOSTS", "")
    return frozenset(
        name for name in (part.strip().lower() for part in raw.split(",")) if name and "." not in name
    )


def timeout_for(url: str, env: dict[str, str] | None = None) -> int:
    """https 주소는 30초, V3_INTERNAL_HTTP_HOSTS의 http 주소는 180초. 그 밖의 주소는 ValueError."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "https":
        return TIMEOUT
    if scheme == "http" and (parts.hostname or "").lower() in internal_hosts(env):
        return INTERNAL_TIMEOUT
    raise ValueError("https 주소나 V3_INTERNAL_HTTP_HOSTS의 내부 서비스만 호출한다")


def default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, str], bytes]:
    timeout = timeout_for(url)
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def send(
    transport: Transport, method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, str], bytes]:
    """transport를 부르고, 연결이 끊기거나 시간이 넘으면(OSError) 한 번만 다시 부른다.

    2026-11-19 매입 기준표 평가(33회)에서 Document Intelligence 연결 끊김과 Prompt Shields TLS 시간 초과가
    한 번씩 났다. 응답을 받은 실패(4xx·5xx)는 다시 부르지 않는다.
    """
    try:
        return transport(method, url, headers, body)
    except OSError:
        return transport(method, url, headers, body)


def header(headers: dict[str, str], name: str) -> str | None:
    """머리글 이름은 대소문자를 가리지 않는다."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None
