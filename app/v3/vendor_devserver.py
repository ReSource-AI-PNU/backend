"""업체 측 에이전트 하나를 따로 띄우는 표준 라이브러리 HTTP 서버(로컬 시연, 개발용).

    VENDOR_SERVICE_KEY=<서비스 키> python3 -m app.v3.vendor_devserver \\
        --vendor org_namhae --port 8032 --store <회사 측 서버와 같은 저장 파일> [--llm rule|foundry|anthropic]

- 같은 일을 하는 FastAPI 앱은 vendor_service.py다(배포용, 로컬에서 띄워 보지 못했다). 두 서버는 같은
  서비스 함수(service.handle_vendor_message)를 부르고, 서비스 키와 본문 확인도 httpio의 같은 함수로 한다.
- POST /a2a/messages  본문 {"message": {...}, "context": {...}}, 답 {"message": {...}}
- GET  /healthz       {"status": "ok", "vendorOrgId": ..., "handled": 지금까지 답한 메시지 수}
- 서비스 키는 환경 변수 VENDOR_SERVICE_KEY로만 받는다(Authorization: Bearer). 없으면 모든 메시지를
  503으로 거절한다. --vendor를 주지 않으면 환경 변수 VENDOR_ORG_ID를 쓴다.
- 저장: 회사 측 서버(devserver.py --store)와 같은 JSON 파일 둘(저장소, 근거)을 본다. 배포에서 두 서비스가
  같은 PostgreSQL을 보는 것을 로컬에서 흉내 낸 것이다. 메시지를 받을 때마다 두 파일을 다시 읽고, 판단
  도구가 남긴 계산 근거는 근거 파일에 쓴다. 이 프로세스는 시드를 올리거나 되돌리지 않는다.
- 답 메시지는 받는 쪽(회사 측)이 다시 검증한다. 여기서 검증했다는 표시를 붙이지 않는다.
- 모델은 회사 측 서버와 같은 방식으로 고른다(--llm, 없으면 V3_LLM). 업체 프로필의 model 선택을 따른다.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from typing import Any

from app.v3 import devserver, httpio, localshare, service
from app.v3.agents.factory import LLM_MODES, MODE_LABELS, LlmConfigError, normalize_mode


class VendorAgent:
    """한 업체의 에이전트 서버 상태: 대변하는 업체, 서비스 키, 답한 메시지 수."""

    def __init__(self, app: service.App, vendor_org_id: str, service_key: str) -> None:
        self.app = app
        self.vendor_org_id = vendor_org_id
        self._service_key = service_key
        self.handled = 0

    def __repr__(self) -> str:
        return f"VendorAgent({self.vendor_org_id!r})"

    def route(self, app: service.App, request: devserver.Request) -> Any:
        """devserver.make_handler에 router로 준다. 한 번에 한 요청만 들어온다(잠금은 그쪽이 건다)."""
        parts = [p for p in request.path.strip("/").split("/") if p]
        match (request.method, parts):
            case ("GET", ["healthz"]):
                return {
                    "status": "ok",
                    "vendorOrgId": self.vendor_org_id or None,
                    "handled": self.handled,
                }
            case ("POST", ["a2a", "messages"]):
                httpio.check_service_key(request.headers, self._service_key)
                vendor_org_id = httpio.vendor_org_required(self.vendor_org_id)
                message, context = httpio.vendor_request(request.json(), vendor_org_id)
                self._refresh(message, context)
                reply = service.handle_vendor_message(app, vendor_org_id, message, context)
                self.handled += 1
                return {"message": reply}
        raise service.ServiceError("NOT_FOUND", "그런 주소가 없습니다.", 404)

    def _refresh(self, message: Mapping[str, Any], context: Mapping[str, Any]) -> None:
        """회사 측 프로세스가 쓴 저장소와 근거를 다시 읽는다. 아직 파일이 없으면 503."""
        app = self.app
        ready = [target.reload() for target in (app.store, app.registry)]
        if not all(ready):
            raise service.ServiceError(
                "STORE_NOT_READY",
                "저장 파일이 아직 없습니다. 회사 측 서버를 같은 --store로 먼저 띄워 주세요.",
                503,
            )
        # 대화의 첫 메시지면 이 대화의 도구 결과 메모를 버린다. 회사 측이 시연 데이터를 되돌리면 대화
        # 번호가 다시 1부터 시작하므로, 지난 대화의 메모(이제 없는 계산 근거를 가리킨다)를 쓰지 않게 한다
        thread = context.get("thread") if isinstance(context.get("thread"), Mapping) else {}
        thread_id = str(thread.get("id") or message.get("threadId") or "")
        earlier = [
            m
            for m in (thread.get("messages") or [])
            if isinstance(m, Mapping) and m.get("from") == self.vendor_org_id
        ]
        if thread_id and not earlier:
            app.vendor_memo.pop(thread_id, None)


def build_vendor_app(
    store_path: str,
    *,
    llm: str = "rule",
    today: str | None = None,
    env: Mapping[str, str] | None = None,
) -> service.App:
    """회사 측 서버와 같은 저장 파일을 보는 앱. 시드를 올리지 않는다."""
    settings = os.environ if env is None else env
    return service.App(
        localshare.ReloadingStore(store_path),
        localshare.ReloadingRegistry(devserver.evidence_file(store_path)),
        llm_factory=devserver.llm_factory_from_env(llm, settings),
        today=today,
    )


def make_server(
    agent: VendorAgent, host: str = "127.0.0.1", port: int = 8032, quiet: bool = True
) -> Any:
    return devserver.make_server(agent.app, host, port, quiet=quiet, router=agent.route)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Re:Source AI v3 업체 측 에이전트 서버(표준 라이브러리)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8032)
    parser.add_argument(
        "--vendor", default=None, help="대변하는 업체의 조직 ID. 없으면 환경 변수 VENDOR_ORG_ID"
    )
    parser.add_argument(
        "--store", required=True, help="회사 측 서버(devserver --store)와 같은 저장 파일 경로"
    )
    parser.add_argument(
        "--llm",
        choices=LLM_MODES,
        default=None,
        help="모델. 없으면 환경 변수 V3_LLM, 그것도 없으면 rule",
    )
    parser.add_argument("--today", default=None, help="기준일 YYYY-MM-DD(없으면 오늘)")
    args = parser.parse_args(argv)
    vendor = (args.vendor or os.environ.get("VENDOR_ORG_ID") or "").strip()
    if not vendor:
        raise SystemExit("--vendor나 환경 변수 VENDOR_ORG_ID로 대변할 업체를 정해야 합니다.")
    key = (os.environ.get("VENDOR_SERVICE_KEY") or "").strip()
    if not key:
        raise SystemExit("환경 변수 VENDOR_SERVICE_KEY가 필요합니다(회사 측 서버와 같은 값).")
    try:
        llm = normalize_mode(args.llm or os.environ.get("V3_LLM"))
    except LlmConfigError as error:
        raise SystemExit(str(error)) from None
    app = build_vendor_app(args.store, llm=llm, today=args.today)
    server = make_server(VendorAgent(app, vendor, key), args.host, args.port, quiet=False)
    sys.stderr.write(
        f"Re:Source AI v3 업체 측 에이전트({vendor}): http://{args.host}:{server.server_address[1]} · "
        f"모델: {MODE_LABELS[llm]} · 저장 파일: {args.store}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
