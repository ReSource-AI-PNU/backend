"""보관장 3D 스캔의 재구성 서버(H100, infra/h100/recon) 호출.

- 설정 V3_RECON_URL(예: H100 사설 주소의 8090 포트). 비어 있으면 None이고 스캔 올리기는 503 SCAN_UNAVAILABLE이다.
- http는 사설 주소(10.x, 172.16-31.x, 192.168.x)나 V3_INTERNAL_HTTP_HOSTS의 이름일 때만 쓴다. 공개 주소로는
  사진을 평문으로 보내지 않는다(그 밖의 주소는 https만).
- 사진은 multipart의 frames 필드로 보낸다. 결과 모양 검사는 scan/result.py가 한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.v3.intake._http import internal_http

ENV_URL = "V3_RECON_URL"
HEALTH_TIMEOUT = 2.0
TIMEOUT = 60.0

# transport(method, url, headers, body, timeout) -> (status, headers, body)
Transport = Callable[
    [str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]
]


class ReconError(RuntimeError):
    """재구성 서버에 닿지 못했거나 응답이 잘못됐다. code는 서버가 준 오류 코드(없으면 UNREACHABLE·BAD_RESPONSE)."""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code, self.status = code, status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """재구성 서버의 넘겨주기(3xx)를 따라가지 않는다(설정한 주소 밖으로 사진을 보내지 않는다)."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def allowed_url(url: str, env: Mapping[str, str] | None = None) -> bool:
    if urllib.parse.urlsplit(url).scheme == "https":
        return bool(urllib.parse.urlsplit(url).hostname)
    return internal_http(url, dict(env) if env is not None else None)


def multipart(files: Sequence[tuple[str, bytes]], field: str = "frames") -> tuple[str, bytes]:
    """(content-type, 본문). 경계는 내용의 해시로 정한다(실행마다 같다)."""
    seed = hashlib.sha256()
    for name, data in files:
        seed.update(name.encode("utf-8"))
        seed.update(hashlib.sha256(data).digest())
    boundary = "rsai-scan-" + seed.hexdigest()[:32]
    chunks: list[bytes] = []
    for name, data in files:
        kind = "image/png" if name.lower().endswith(".png") else "image/jpeg"
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{name}"\r\nContent-Type: {kind}\r\n\r\n'
            ).encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class ReconClient:
    def __init__(self, base_url: str, transport: Transport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport or _default_transport
        self.last_error: str | None = None  # 운영 화면용(사용자 화면에는 보이지 않는다)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _call(
        self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None,
        timeout: float = TIMEOUT,
    ) -> tuple[int, bytes]:  # fmt: skip
        try:
            status, _, data = self.transport(
                method, self.base_url + path, dict(headers or {}), body, timeout
            )
        except OSError as error:  # 연결 거절, 시간 초과(URLError는 OSError다)
            self.last_error = f"{type(error).__name__}: {error}"[:300]
            raise ReconError("UNREACHABLE", "재구성 서버에 닿지 못했습니다.") from error
        return status, data

    def _json(self, status: int, data: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(data.decode("utf-8")) if data else {}
        except (UnicodeDecodeError, ValueError) as error:
            raise ReconError(
                "BAD_RESPONSE", "재구성 서버 응답을 읽지 못했습니다.", status
            ) from error
        if not isinstance(payload, dict):
            raise ReconError("BAD_RESPONSE", "재구성 서버 응답 형식이 잘못됐습니다.", status)
        if status >= 400:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = str(error.get("code") or "RECON_FAILED")
            self.last_error = f"{status} {code}"
            raise ReconError(
                code, str(error.get("message") or "재구성 서버가 거절했습니다."), status
            )
        return payload

    def health(self) -> dict[str, Any] | None:
        """2초 안에 답하면 그 내용, 아니면 None."""
        try:
            status, data = self._call("GET", "/health", timeout=HEALTH_TIMEOUT)
            payload = self._json(status, data)
        except ReconError:
            return None
        return payload if payload.get("ok") is True else None

    def submit(self, frames: Sequence[tuple[str, bytes]]) -> str:
        content_type, body = multipart(frames)
        status, data = self._call(
            "POST", "/jobs", body=body, headers={"Content-Type": content_type}
        )
        payload = self._json(status, data)
        job = payload.get("jobId")
        if not isinstance(job, str) or not JOB_ID.fullmatch(job):
            raise ReconError("BAD_RESPONSE", "재구성 서버가 작업 번호를 주지 않았습니다.", status)
        return job

    def job(self, job_id: str) -> dict[str, Any]:
        if not JOB_ID.fullmatch(str(job_id)):
            raise ReconError("BAD_RESPONSE", "작업 번호가 잘못됐습니다.")
        status, data = self._call("GET", f"/jobs/{job_id}")
        return self._json(status, data)

    def points(self, job_id: str) -> bytes:
        if not JOB_ID.fullmatch(str(job_id)):
            raise ReconError("BAD_RESPONSE", "작업 번호가 잘못됐습니다.")
        status, data = self._call("GET", f"/jobs/{job_id}/points")
        if status != 200:
            raise ReconError("BAD_RESPONSE", "점군을 받지 못했습니다.", status)
        return data


def client_from_env(
    env: Mapping[str, str], transport: Transport | None = None
) -> ReconClient | None:
    """주소가 없으면 None. 쓸 수 없는 주소(공개 http 등)면 ValueError."""
    url = str(env.get(ENV_URL) or "").strip()
    if not url:
        return None
    if not allowed_url(url, env):
        raise ValueError("V3_RECON_URL은 https이거나 사설 주소·내부 서비스 이름의 http여야 한다")
    return ReconClient(url, transport)
