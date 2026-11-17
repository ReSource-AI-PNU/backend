"""업체 측 에이전트로 가는 길. 회사 측은 이 인터페이스로만 업체 측을 부른다.

받은 답 메시지는 회사 측에서 다시 근거 검증기를 통과시킨다(진행기가 한다).
보내는 쪽이 검증했다는 말을 믿지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from app.v3.agents.llm import Transport, urllib_transport
from app.v3.textutil import josa

Handler = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]


class GatewayError(RuntimeError):
    """업체 측 에이전트에 닿지 못했거나 답이 메시지 모양이 아니다."""


class VendorGateway(Protocol):
    def deliver(self, vendor_org: str, message: dict, context: dict) -> dict: ...


class LocalVendorGateway:
    """같은 프로세스에서 업체 측 에이전트를 부른다(로컬 시연, 테스트).

    handler는 service.handle_vendor_message를 앱에 묶은 함수다.
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler

    def deliver(self, vendor_org: str, message: dict, context: dict) -> dict:
        reply = self._handler(vendor_org, message, context)
        if not isinstance(reply, dict):
            raise GatewayError("업체 측 에이전트의 답이 메시지 모양이 아니다")
        return reply


class HttpVendorGateway:
    """POST {base}/a2a/messages. 본문 {"message", "context"}, 답 {"message"}.

    base_urls는 주소 하나(모든 업체가 한 서비스) 또는 {업체 조직 ID: 주소}다.
    서비스 키는 Authorization: Bearer 헤더로만 보내고, 예외 문구와 기록에 넣지 않는다.
    """

    def __init__(
        self,
        base_urls: str | Mapping[str, str],
        service_key: str,
        transport: Transport | None = None,
        *,
        timeout: float = 120,
    ) -> None:
        self._base_urls = base_urls
        self._service_key = service_key
        self._transport = transport or urllib_transport
        self.timeout = timeout

    def __repr__(self) -> str:
        return "HttpVendorGateway()"

    def _base(self, vendor_org: str) -> str:
        if isinstance(self._base_urls, str):
            base = self._base_urls
        else:
            base = self._base_urls.get(vendor_org) or self._base_urls.get("*") or ""
        if not base:
            raise GatewayError(f"업체 측 에이전트 주소가 없다: {vendor_org}")
        return base.rstrip("/")

    def deliver(self, vendor_org: str, message: dict, context: dict) -> dict:
        body = json.dumps(
            {"vendorOrgId": vendor_org, "message": message, "context": context},
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._service_key}",
        }
        url = f"{self._base(vendor_org)}/a2a/messages"
        try:
            status, _, raw = self._transport("POST", url, headers, body, self.timeout)
        except Exception as error:
            raise GatewayError(
                f"업체 측 에이전트에 연결하지 못했다: {type(error).__name__}"
            ) from error
        if status >= 400:
            raise GatewayError(f"업체 측 에이전트가 {josa(str(status), '을를')} 돌려줬다")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayError("업체 측 에이전트의 답이 JSON이 아니다") from error
        reply = data.get("message") if isinstance(data, dict) else None
        if not isinstance(reply, dict):
            raise GatewayError("업체 측 에이전트의 답에 message가 없다")
        return reply
