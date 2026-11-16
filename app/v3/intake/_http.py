"""intake 모듈이 함께 쓰는 HTTP 호출. 테스트에서는 transport를 가짜로 바꾼다."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable

# transport(method, url, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


def default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, str], bytes]:
    if not url.lower().startswith("https://"):
        raise ValueError("https 주소만 호출한다")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def header(headers: dict[str, str], name: str) -> str | None:
    """머리글 이름은 대소문자를 가리지 않는다."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None
