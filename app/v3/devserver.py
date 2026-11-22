"""표준 라이브러리 HTTP 서버. 계약 8장의 API를 service 함수에 그대로 잇는다(로컬 시연, 개발용).

    python3 -m app.v3.devserver --port 8000 [--store 경로.json] [--reset]
                                [--llm rule|foundry|anthropic]   (없으면 환경 변수 V3_LLM)
                                [--auth header|token --org-tokens 토큰.json]

- 시작할 때 가정값, 근거 시드(법령·기준값), 시연 데이터를 올린다.
- --store를 주면 저장소를 그 JSON 파일에, 근거 레지스트리를 옆 파일(<이름>.evidence.json)에 쓴다.
  두 파일이 이미 있으면 시드로 덮어쓰지 않고 이전 진단을 이어 간다. --reset을 주면 처음 상태로
  되돌린다. 저장소 파일만 있고 근거 파일이 없으면 이어 갈 수 없어 처음 상태로 시작한다.
- 기본은 --llm rule(규칙 정책, 모델 미연결)이다. --llm foundry일 때만 환경 변수
  FOUNDRY_ENDPOINT, FOUNDRY_DEPLOYMENT, FOUNDRY_API_KEY를 읽는다. 업체가 ollama를 골랐으면
  OLLAMA_BASE_URL이 있을 때만 그 모델을 쓰고, 없으면 그 업체는 규칙 정책으로 돈다(화면에 그렇게 나온다).
- --llm anthropic은 실제 모델로 돌려 보는 로컬 전용 선택지다(설계의 기본 모델은 Foundry gpt-5-mini).
  키는 ANTHROPIC_API_KEY, 없으면 ANTHROPIC_API_KEY_FILE이 가리키는 파일에서 읽는다. 모델은
  ANTHROPIC_MODEL(기본 claude-sonnet-5-5). 화면에는 실제로 답한 모델 이름이 그대로 나온다.
  업체가 ollama를 골랐고 OLLAMA_BASE_URL이 없으면 그 업체는 규칙 정책으로 돈다. V3_LLM_ALL_VENDORS=1을
  주면 그 업체도 이 모델이 답한다(설정한 모델과 답한 모델이 다르다는 것은 화면에 따로 나온다).
- 도로 거리는 환경 변수 V3_DISTANCE로 고른다: kakao(KAKAO_REST_API_KEY, 카카오모빌리티 길찾기) |
  azure(AZURE_MAPS_KEY) | none. 비어 있으면 AZURE_MAPS_KEY가 있을 때만 Azure Maps를 쓴다. 제공자가
  없거나 호출이 실패하면 직선 거리 × 우회 계수(가정값)로 계산하고 결과에 가정값이라고 남긴다.
- 사진·영상 업로드(POST /intake/media)는 V3_DETECTOR_ENDPOINT(와 V3_DETECTOR_API_KEY)가 있을 때만
  탐지 모델을 부른다. 없으면 {"detectorUnavailable": true}로 답한다(탐지 결과를 지어내지 않는다).
- 문서 업로드(POST /intake/documents)는 AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT와 _API_KEY가 있으면
  Document Intelligence(prebuilt-layout)로 읽고, 없으면 UTF-8 글 파일만 읽는다. AZURE_CONTENT_SAFETY_ENDPOINT와
  _API_KEY가 있으면 Prompt Shields로 지시문 주입을 검사하고, 없으면 shieldUnavailable로 표시한다.
- 요청하는 조직은 X-Org-Id 헤더로 밝힌다(--auth header, 기본). 이 모드는 시연 전용이며 공개 주소에
  그대로 두면 조직 경계가 지켜지지 않는다. --auth token --org-tokens <파일>을 주면 조직별 서비스 토큰
  (Authorization: Bearer <토큰>)으로 조직을 정하고 X-Org-Id는 무시한다. 파일은
  {"조직 ID": "토큰"} 꼴의 JSON이고, GET /orgs는 자기 조직과 대화 상대만 준다.
  옵션 대신 환경 변수로도 켠다(FastAPI 경로와 같은 이름): V3_AUTH=token과 V3_ORG_TOKENS(JSON 글)
  또는 V3_ORG_TOKENS_FILE(그 JSON을 담은 파일 경로).
- 브라우저가 부를 수 있는 출처는 http://localhost:3000, http://127.0.0.1:3000이다. 더하려면
  --cors-origins나 환경 변수 V3_CORS_ORIGINS에 쉼표로 나눠 적는다.
- 업체 측 에이전트를 따로 띄운 프로세스가 답하게 하려면 환경 변수 V3_VENDOR_AGENTS
  ({"업체 조직 ID": "http://127.0.0.1:8032"})와 VENDOR_SERVICE_KEY를 주고 --store로 저장 파일을 정한다.
  그 업체의 프로세스는 `python3 -m app.v3.vendor_devserver --vendor <조직 ID> --store <같은 파일>`로
  띄운다. 주소가 없는 업체는 이 프로세스 안에서 답한다.
- /admin/* 은 관리자 조직(org_admin)만 부른다. 시연 되돌리기(POST /demo/reset)는 로컬 시연에서는
  누구나 부를 수 있다(App(demo_open=True)). --demo-closed를 주면 관리자만 부른다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TRUST_FILE = Path(__file__).resolve().parents[2] / "docs" / "v3" / "eval" / "latest.json"
from urllib.parse import parse_qs, unquote, urlsplit

from app.v3 import httpio, live, localshare, service
from app.v3.agents.factory import (
    LLM_MODES,
    MODE_LABELS,
    LlmConfigError,
    build_llm_factory,
    normalize_mode,
)
from app.v3.evidence.registry import InMemoryRegistry, JsonFileRegistry
from app.v3.intake import criteria_llm, detector, documents, shields
from app.v3.judge.distance import distance_provider_from_env
from app.v3.onboarding import agent as onboarding
from app.v3.onboarding import keys as mcp_keys
from app.v3.scan import client as scan_client
from app.v3.scan import views as scans
from app.v3.store import InMemoryStore, JsonFileStore

PREFIX = "/api/v3"
ALLOWED_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")
ALLOWED_HEADERS = "X-Org-Id, Authorization, Content-Type, Last-Event-ID"
ALLOWED_METHODS = "GET, POST, PUT, OPTIONS"


class Request:
    def __init__(
        self,
        method: str,
        path: str,
        query: dict,
        headers: Mapping[str, str],
        body: bytes,
        auth: httpio.Auth | None = None,
    ):
        self.method, self.path, self.query, self.headers, self.body = (
            method,
            path,
            query,
            headers,
            body,
        )
        # 요청한 조직을 정하는 방법. 기본은 header(X-Org-Id를 믿는다, 시연 전용)
        self.auth = auth or httpio.Auth()
        self._form: tuple[dict[str, str], dict[str, tuple[str, bytes]]] | None = None

    @property
    def viewer(self) -> str:
        """요청한 조직. token 모드에서는 Authorization: Bearer 토큰으로 정하고 X-Org-Id는 무시한다."""
        return self.auth.viewer(self.headers)

    def json(self) -> dict[str, Any]:
        """JSON이 아니면 400 INVALID_JSON, 객체가 아니면 422 INVALID_BODY(api.py와 같다)."""
        return httpio.parse_object(self.body)

    def form(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        if self._form is None:
            self._form = parse_multipart(self.headers.get("Content-Type") or "", self.body)
        return self._form

    def frames(self) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
        """보관장 3D 스캔: multipart의 frames 파일들(api.read_frames와 같다)."""
        content_type = self.headers.get("Content-Type") or ""
        if not content_type.lower().startswith("multipart/form-data"):
            raise service.ServiceError(
                "INVALID_MULTIPART", "multipart/form-data로 보내 주세요.", 400
            )
        every: list[tuple[str, str, bytes]] = []
        fields, _ = parse_multipart(content_type, self.body, every)
        frames = [(name, data) for field, name, data in every if field == "frames"]
        if not frames:
            raise httpio.file_required()
        return fields, frames

    def upload(self, default_name: str) -> tuple[dict[str, str], str, bytes]:
        """(글 항목, 파일 이름, 파일 내용). multipart가 아니면 본문 전체를 파일로 본다."""
        content_type = (self.headers.get("Content-Type") or "").lower()
        if content_type.startswith("multipart/form-data"):
            fields, files = self.form()
            if not files:
                raise httpio.file_required()
            filename, data = files.get("file") or next(iter(files.values()))
            return fields, filename or default_name, httpio.check_upload(data)
        fields = {k: v[0] for k, v in self.query.items()}
        return fields, fields.get("filename") or default_name, httpio.check_upload(self.body)


def _disposition(value: str) -> dict[str, str]:
    """Content-Disposition의 name, filename. 따옴표 안의 세미콜론과 filename*=UTF-8''를 받는다."""
    out: dict[str, str] = {}
    parts, current, quoted = [], "", False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == ";" and not quoted:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    for part in parts[1:]:
        key, _, raw = part.strip().partition("=")
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1].replace('\\"', '"')
        key = key.strip().lower()
        if key.endswith("*"):
            _, _, encoded = raw.partition("''")
            out[key[:-1]] = unquote(encoded or raw)
        else:
            out.setdefault(key, raw)
    return out


def parse_multipart(
    content_type: str, body: bytes, every: list[tuple[str, str, bytes]] | None = None
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """multipart/form-data를 (글 항목, 파일)로 나눈다. 파일은 {항목 이름: (파일 이름, 내용)}.

    every를 주면 같은 이름으로 여러 번 온 파일까지 (항목 이름, 파일 이름, 내용)으로 차례대로 담는다.
    """
    boundary = ""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "boundary":
            boundary = value.strip().strip('"')
    if not boundary:
        raise service.ServiceError("INVALID_MULTIPART", "multipart 경계(boundary)가 없습니다.", 400)
    marker = b"--" + boundary.encode("latin-1")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    # 경계는 줄 머리에서만 경계다. 파일 내용 안에 같은 글자가 있어도 자르지 않는다
    delimiter = re.compile(rb"\r?\n" + re.escape(marker) + rb"(?=--|[ \t]*\r?\n)")
    for chunk in delimiter.split(b"\r\n" + body)[1:]:
        if chunk.startswith(b"--"):
            break  # 닫는 경계
        chunk = chunk.lstrip(b" \t").removeprefix(b"\r\n").removeprefix(b"\n")
        head, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            head, separator, data = chunk.partition(b"\n\n")
        if not separator:
            continue
        info: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-disposition":
                info = _disposition(value)
        if "name" not in info:
            continue
        if "filename" in info:
            files[info["name"]] = (info["filename"], data)
            if every is not None:
                every.append((info["name"], info["filename"], data))
        else:
            fields[info["name"]] = data.decode("utf-8", "replace")
    return fields, files


class Raw:
    """JSON이 아닌 응답(양식 파일 내려받기)."""

    def __init__(self, body: bytes, content_type: str, filename: str | None = None) -> None:
        self.body, self.content_type, self.filename = body, content_type, filename


class Started:
    """202 응답과 함께 뒤에서 돌릴 실행(POST /sessions/{id}/run?mode=async)."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body


class EventStream:
    """SSE 응답(GET /sessions/{id}/stream). first는 첫 읽기(권한 확인을 겸한다)의 결과다."""

    def __init__(self, session_id: str, viewer: str, first: dict[str, Any]) -> None:
        self.session_id, self.viewer, self.first = session_id, viewer, first


def route(app: service.App, request: Request) -> Any:
    """경로를 service 함수에 잇는다. 돌려준 값은 JSON으로 나간다."""
    if not request.path.startswith(PREFIX):
        raise service.ServiceError("NOT_FOUND", "그런 주소가 없습니다.", 404)
    parts = [unquote(p) for p in request.path[len(PREFIX) :].strip("/").split("/") if p]
    method = request.method
    # 상태 확인은 조직을 밝히지 않고 부른다(토큰이 없거나 틀려도 답한다). token 모드에서는 그 밖의
    # 모든 경로에 토큰이 있어야 한다(시연 되돌리기만 예외: 누가 부를 수 있는지는 서비스가 정한다)
    if parts in (["health"], ["trust"]):
        viewer = ""
    else:
        needed = request.auth.scoped and parts != ["demo", "reset"]
        viewer = request.auth.viewer(request.headers, required=needed)
    match (method, parts):
        case ("GET", ["health"]):
            return {"ok": True}
        case ("GET", ["trust"]):
            # 공개 신뢰 페이지가 읽는 최신 평가 요약 (FastAPI 경로와 같은 파일)
            return json.loads(TRUST_FILE.read_text(encoding="utf-8"))
        case ("POST", ["demo", "reset"]):
            return service.reset_demo(app, viewer or None)
        case ("GET", ["orgs"]):
            # token 모드에서는 자기 조직과 대화 상대만 준다
            return service.list_orgs(app, viewer or None, request.auth.scoped)
        case ("GET", ["orgs", org_id, "streams"]):
            return service.list_streams(app, org_id, viewer)
        case ("GET", ["orgs", org_id, "report"]):
            date_from = (request.query.get("from") or [""])[0] or None
            date_to = (request.query.get("to") or [""])[0] or None
            return service.get_report(app, org_id, viewer, date_from, date_to)
        case ("GET", ["orgs", org_id, "memory"]):
            return service.get_memory(app, org_id, viewer)
        case ("PUT", ["orgs", org_id, "memory"]):
            return service.update_memory(app, org_id, request.json(), viewer)
        case ("PUT", ["orgs", org_id, "streams", stream_id]):
            return service.update_stream(app, org_id, stream_id, request.json(), viewer)
        case ("GET", ["vendors", org_id, "permit-catalog"]):
            return service.vendor_permit_catalog(app, org_id, viewer)
        case ("GET", ["vendors", org_id]):
            return service.get_vendor(app, org_id, viewer)
        case ("PUT", ["vendors", org_id]):
            return service.update_vendor(app, org_id, request.json(), viewer)
        case ("GET", ["vendors", org_id, "onboarding"]):
            return onboarding.get_onboarding(app, org_id, viewer)
        case ("POST", ["vendors", org_id, "onboarding", "messages"]):
            return onboarding.post_message(app, org_id, request.json(), viewer)
        case ("POST", ["vendors", org_id, "onboarding", "confirm"]):
            return onboarding.post_confirm(app, org_id, request.json(), viewer)
        case ("DELETE", ["vendors", org_id, "onboarding"]):
            return onboarding.delete_onboarding(app, org_id, viewer)
        case ("GET", ["vendors", org_id, "mcp-keys"]):
            return mcp_keys.get_keys(app, org_id, viewer)
        case ("POST", ["vendors", org_id, "mcp-keys"]):
            return mcp_keys.post_key(app, org_id, request.json(), viewer)
        case ("DELETE", ["vendors", org_id, "mcp-keys", key_id]):
            return mcp_keys.delete_key(app, org_id, key_id, viewer)
        case ("POST", ["sessions"]):
            body = request.json()
            return service.create_session(app, str(body.get("emitterOrgId") or viewer), viewer)
        case ("GET", ["sessions"]):
            return service.list_sessions(app, viewer)
        case ("GET", ["sessions", session_id]):
            return service.get_view(app, session_id, viewer)
        case ("GET", ["sessions", session_id, "quote-request"]):
            stream_id = (request.query.get("streamId") or [""])[0]
            vendor_id = (request.query.get("vendorId") or [""])[0]
            return service.quote_request(app, session_id, viewer, stream_id, vendor_id)
        case ("POST", ["sessions", session_id, "run"]):
            mode = (request.query.get("mode") or [""])[0]
            if mode == "async":
                return Started(service.start_run(app, session_id, viewer))
            if mode not in ("", "sync"):
                raise service.ServiceError("INVALID_VALUE", "mode는 async나 sync여야 합니다.", 422)
            return service.run_session(app, session_id, viewer)
        case ("GET", ["sessions", session_id, "stream"]):
            try:
                after = live.resume_point(
                    (request.query.get("after") or [""])[0], request.headers.get("Last-Event-ID")
                )
            except ValueError:
                raise service.ServiceError(
                    "INVALID_VALUE", "after는 0 이상의 정수여야 합니다.", 422
                ) from None
            first = service.stream_events(app, session_id, viewer, after)
            return EventStream(session_id, viewer, first)
        case ("POST", ["sessions", session_id, "answers"]):
            question_id, value = httpio.answer_body(request.json())
            return service.answer_question(app, session_id, question_id, value, viewer)
        case ("POST", ["sessions", session_id, "approvals"]):
            thread_id = str(httpio.required(request.json(), "threadId"))
            return service.approve(app, session_id, thread_id, viewer)
        case ("POST", ["sessions", session_id, "permit-checks"]):
            return service.record_permit_check(app, session_id, request.json(), viewer)
        case ("POST", ["sessions", session_id, "corrections"]):
            return service.record_correction(app, session_id, request.json(), viewer)
        case ("POST", ["sessions", session_id, "outcomes"]):
            return service.record_outcome(app, session_id, request.json(), viewer)
        case ("PUT", ["sessions", session_id, "outcomes", inspection_id]):
            return service.correct_outcome(app, session_id, inspection_id, request.json(), viewer)
        case ("GET", ["evidence"]):
            kinds = [k for raw in request.query.get("kind", []) for k in raw.split(",") if k]
            return service.search_evidence(
                app, (request.query.get("q") or [""])[0], kinds or None, viewer
            )
        case ("GET", ["evidence", *rest]) if rest:
            return service.get_evidence(app, "/".join(rest), viewer)
        case ("GET", ["intake", "csv", "template"]):
            return Raw(service.csv_template(app), "text/csv; charset=utf-8", "measure-template.csv")
        case ("POST", ["intake", "csv"]):
            fields, filename, data = request.upload("측정값.csv")
            return service.upload_csv(app, fields.get("orgId") or viewer, filename, data, viewer)
        case ("POST", ["intake", "documents"]):
            fields, filename, data = request.upload("문서")
            return service.upload_document(
                app, fields.get("orgId") or viewer, fields.get("kind") or "", filename, data, viewer
            )
        case ("POST", ["intake", "media"]):
            fields, filename, data = request.upload("사진")
            return service.upload_media(
                app,
                fields.get("orgId") or viewer,
                fields.get("streamId") or "",
                filename,
                data,
                viewer,
            )
        case ("POST", ["intake", "scan"]):
            fields, frames = request.frames()
            try:
                duration = float(fields.get("durationSec") or 0) or None
            except ValueError:
                duration = None
            return scans.create_scan(
                app,
                fields.get("orgId") or viewer,
                frames,
                fields.get("source") or "",
                duration,
                viewer,
            )
        case ("GET", ["orgs", org_id, "scans"]):
            return scans.list_scans(app, org_id, viewer)
        case ("GET", ["orgs", org_id, "scans", scan_id]):
            return scans.get_scan(app, org_id, scan_id, viewer)
        case ("GET", ["orgs", org_id, "scans", scan_id, "points"]):
            return Raw(scans.scan_points(app, org_id, scan_id, viewer), "application/octet-stream")
        case ("PUT", ["orgs", org_id, "scans", scan_id, "piles", pile_id]):
            return scans.update_pile(app, org_id, scan_id, pile_id, request.json(), viewer)
        case ("PUT", ["orgs", org_id, "scans", scan_id, "layout"]):
            return scans.update_layout(app, org_id, scan_id, request.json(), viewer)
        case ("POST", ["intake", "documents", "confirm"]):
            body = request.json()
            fields = body.get("fields") or []
            if not isinstance(fields, list):
                raise httpio.invalid_body("fields는 배열이어야 합니다.")
            return service.confirm_document(
                app,
                str(body.get("orgId") or viewer),
                str(body.get("documentName") or ""),
                fields,
                viewer,
            )
        case ("POST", ["intake", "criteria", "confirm"]):
            body = request.json()
            return service.confirm_criteria(
                app,
                str(body.get("orgId") or viewer),
                str(body.get("documentName") or ""),
                body.get("rows"),
                viewer,
                str(body.get("draftId") or "") or None,
            )
        case ("GET", ["admin", "status"]):
            return service.admin_status(app, viewer)
        case ("POST", ["admin", "eval"]):
            return service.run_eval(app, viewer)
        case ("GET", ["admin", "retrain", "export"]):
            return service.export_retraining(app, viewer)
        case ("POST", ["admin", "retrain", "runs"]):
            return service.register_model_run(app, request.json(), viewer)
    raise service.ServiceError("NOT_FOUND", "그런 주소가 없습니다.", 404)


def make_handler(
    app: service.App,
    lock: threading.Lock | None = None,
    quiet: bool = True,
    auth: httpio.Auth | None = None,
    router: Callable[[service.App, Request], Any] | None = None,
):
    """router는 (앱, 요청) -> 답. 주지 않으면 이 파일의 route다(업체 측 에이전트 서버가 자기 것을 준다).

    요청은 guard를 잡고 처리한다. 뒤에서 도는 실행(mode=async)은 저장소를 부를 때마다 같은 잠금을 잡는다.
    """
    guard = lock or threading.RLock()
    auth = auth or httpio.Auth()
    handle = router or route

    class Handler(BaseHTTPRequestHandler):
        server_version = "ReSourceDev/3"
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            if not quiet:
                sys.stderr.write(f"{self.address_string()} {format % args}\n")

        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", ALLOWED_HEADERS)
                self.send_header("Access-Control-Allow-Methods", ALLOWED_METHODS)
                self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")

        def _send(self, status: int, body: bytes, content_type: str, extra=()) -> None:
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _started(self, started: Started) -> None:
            run_id = started.body["runId"]
            worker = threading.Thread(
                target=service.execute_run,  # 오류는 실행 기록에 남기고 진단을 멈춘 상태로 돌린다
                args=(app.with_lock(guard), started.body["sessionId"], run_id),
                name=f"v3-run-{started.body['sessionId']}",
                daemon=True,
            )
            worker.start()
            body = json.dumps(started.body, ensure_ascii=False).encode("utf-8")
            self._send(202, body, "application/json; charset=utf-8")

        def _stream(self, stream: EventStream) -> None:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            for name, value in live.STREAM_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Connection", "close")  # 길이를 모르므로 다 보내면 연결을 닫는다
            self.end_headers()
            self.close_connection = True

            def read(after: int) -> dict[str, Any]:
                with guard:
                    return service.stream_events(app, stream.session_id, stream.viewer, after)

            try:
                for chunk in live.stream_chunks(stream.first, read, stream.session_id):
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # 브라우저가 떠났다
            except service.ServiceError as error:
                chunk = live.sse("error", {"code": error.code, "message": error.message})
                self.wfile.write(chunk.encode("utf-8"))

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _handle(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > httpio.MAX_BODY_BYTES:
                    raise httpio.too_large()
                body = self.rfile.read(length) if length > 0 else b""
                url = urlsplit(self.path)
                request = Request(
                    self.command, url.path, parse_qs(url.query), self.headers, body, auth
                )
                with guard:  # 저장소가 동시 쓰기를 막지 않으므로 한 번에 한 요청만 처리한다
                    result = handle(app, request)
            except service.ServiceError as error:
                self._json(error.status, error.to_dict())
            except Exception:  # noqa: BLE001 - 서버는 죽지 않고 500으로 답한다
                traceback.print_exc(file=sys.stderr)
                self._json(
                    500, {"error": {"code": "INTERNAL", "message": "서버에서 오류가 났습니다."}}
                )
            else:
                if isinstance(result, Started):
                    self._started(result)
                elif isinstance(result, EventStream):
                    self._stream(result)
                elif isinstance(result, Raw):
                    extra = (
                        [("Content-Disposition", f'attachment; filename="{result.filename}"')]
                        if result.filename
                        else []
                    )
                    self._send(200, result.body, result.content_type, extra)
                else:
                    self._json(200, result)

        do_GET = do_POST = do_PUT = _handle

    return Handler


def make_server(
    app: service.App,
    host: str = "127.0.0.1",
    port: int = 8000,
    quiet: bool = True,
    auth: httpio.Auth | None = None,
    router: Callable[[service.App, Request], Any] | None = None,
):
    auth = auth or httpio.Auth()
    app.auth_mode = auth.mode  # 관리자 화면에 지금 모드를 보여 준다
    server = ThreadingHTTPServer(
        (host, port), make_handler(app, quiet=quiet, auth=auth, router=router)
    )
    server.daemon_threads = True
    return server


def llm_factory_from_env(mode: str | None, env: Mapping[str, str]) -> Callable[..., Any] | None:
    """llm_factory(role, org_id, model) 또는 None(rule). api.py와 같은 함수로 만든다.

    로컬 서버는 설정이 빠져 있으면 어떤 값이 없는지만 밝히고 멈춘다(키는 출력하지 않는다).
    """
    try:
        return build_llm_factory(mode, env, strict=True)
    except LlmConfigError as error:
        raise SystemExit(str(error)) from None


def foundry_factory(env: Mapping[str, str]) -> Callable[..., Any]:
    return llm_factory_from_env("foundry", env)  # type: ignore[return-value]


def anthropic_factory(env: Mapping[str, str]) -> Callable[..., Any]:
    return llm_factory_from_env("anthropic", env)  # type: ignore[return-value]


def build_app(
    *,
    store_path: str | None = None,
    llm: str = "rule",
    today: str | None = None,
    env: Mapping[str, str] | None = None,
    demo_open: bool = True,
    reset: bool = False,
) -> service.App:
    """앱을 만든다. store_path의 두 파일이 있고 reset이 아니면 시드로 덮어쓰지 않고 이어 간다."""
    resumed = False
    settings = os.environ if env is None else env
    try:
        remote_vendors = localshare.vendor_agents(settings.get("V3_VENDOR_AGENTS"))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if store_path:
        evidence_path = evidence_file(store_path)
        # 저장소와 근거가 함께 있어야 이전 진단의 메시지가 인용한 근거를 다시 확인할 수 있다
        both = Path(store_path).exists() and evidence_path.exists()
        # 따로 띄운 업체 측 에이전트가 있으면 그 프로세스가 쓴 근거를 다시 읽을 수 있는 저장을 쓴다
        store: Any = (localshare.ReloadingStore if remote_vendors else JsonFileStore)(store_path)
        registry: Any = (localshare.ReloadingRegistry if remote_vendors else JsonFileRegistry)(
            evidence_path
        )
        resumed = both and not reset and bool(store.list_orgs())
    else:
        store, registry = InMemoryStore(), InMemoryRegistry()
    factory = llm_factory_from_env(llm, settings)
    try:
        # V3_DISTANCE=kakao|azure|none과 키. 고른 제공자의 키가 없으면 로컬 서버는 멈춘다
        distance_provider = distance_provider_from_env(settings, strict=True)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    try:
        # V3_RECON_URL(보관장 3D 스캔 재구성 서버). 비면 None이고 스캔 올리기는 503이다
        recon = scan_client.client_from_env(settings)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    app = service.App(
        store,
        registry,
        llm_factory=factory,
        today=today,
        recon=recon,
        demo_open=demo_open,
        distance_provider=distance_provider,
        # V3_DETECTOR_ENDPOINT가 없으면 None이고, 사진·영상 업로드는 "미연결"이라고 답한다
        detector=detector.client_from_env(settings),
        documents=documents.client_from_env(settings),
        shields=shields.client_from_env(settings),
        # --llm foundry와 Foundry 주소·배포·키가 있으면 매입 기준표를 모델이 읽는다(V3_CRITERIA_READER=0이면 끔)
        criteria_reader=criteria_llm.reader_from_env({**settings, "V3_LLM": llm}),
    )
    app.resumed = resumed  # 이어 간 것인지(서버가 뜰 때 알려 준다)
    if remote_vendors:
        # V3_VENDOR_AGENTS에 주소가 있는 업체는 따로 띄운 프로세스(vendor_devserver.py)가 답한다
        try:
            app.vendor_gateway = localshare.split_gateway(app, settings)
        except ValueError as error:
            raise SystemExit(str(error)) from None
    if not resumed:
        if hasattr(registry, "clear"):
            registry.clear()  # 이전 실행의 근거가 남아 있으면 처음 상태가 아니다
        service.seed(app)
    return app


def load_auth(
    mode: str | None, tokens_path: str | None, env: Mapping[str, str] | None = None
) -> httpio.Auth:
    """--auth와 --org-tokens(없으면 환경 변수)로 Auth를 만든다. 틀리면 무엇이 틀렸는지만 밝히고 멈춘다.

    옵션을 주지 않았을 때 읽는 환경 변수는 FastAPI 경로와 같다: V3_AUTH(header | token),
    V3_ORG_TOKENS(JSON 글 {"조직 ID": "토큰"}). 로컬 서버는 V3_ORG_TOKENS_FILE(그 JSON을 담은 파일
    경로)도 받는다. 순서는 --org-tokens 파일, V3_ORG_TOKENS_FILE, V3_ORG_TOKENS다.
    """
    env = {} if env is None else env
    mode = (mode or env.get("V3_AUTH") or httpio.AUTH_HEADER).strip().lower()
    if mode == httpio.AUTH_BFF:
        # bff 모드는 계정 저장소로 소속을 확인해야 한다. 그 일은 FastAPI 경로(api.py)만 한다
        raise SystemExit(
            "V3_AUTH=bff는 FastAPI 서버(app.main)에서만 씁니다(로컬 서버는 header, token)."
        )
    raw = None
    if mode == httpio.AUTH_TOKEN:
        option = "--org-tokens"
        if not tokens_path and (env.get("V3_ORG_TOKENS_FILE") or "").strip():
            tokens_path, option = env["V3_ORG_TOKENS_FILE"].strip(), "V3_ORG_TOKENS_FILE"
        if tokens_path:
            try:
                raw = Path(tokens_path).expanduser().read_text(encoding="utf-8")
            except OSError:
                raise SystemExit(f"{option} 파일을 읽을 수 없습니다: {tokens_path}") from None
        else:
            raw = env.get("V3_ORG_TOKENS") or None
        if not raw:
            raise SystemExit(
                "--auth token에는 --org-tokens 파일이나 환경 변수 V3_ORG_TOKENS, "
                "V3_ORG_TOKENS_FILE이 필요합니다."
            )
    try:
        return httpio.make_auth(mode, raw)
    except ValueError as error:
        raise SystemExit(f"조직 토큰 설정이 틀렸습니다: {error}") from None


def cors_origins(extra: str | None) -> tuple[str, ...]:
    """기본 허용 출처에 쉼표로 나눈 출처를 더한다(V3_CORS_ORIGINS, --cors-origins). http(s) 주소만 받는다."""
    found = list(ALLOWED_ORIGINS)
    for origin in (extra or "").split(","):
        origin = origin.strip().rstrip("/")
        if not origin:
            continue
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", origin):
            raise SystemExit(f"허용 출처는 http(s)://호스트[:포트] 꼴이어야 합니다: {origin}")
        if origin not in found:
            found.append(origin)
    return tuple(found)


def evidence_file(store_path: str) -> Path:
    """근거 레지스트리 파일. 저장소 파일 옆에 둔다: demo.json -> demo.evidence.json"""
    path = Path(store_path)
    return path.with_name(f"{path.stem}.evidence.json")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re:Source AI v3 로컬 서버(표준 라이브러리)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None, help="저장소를 쓸 JSON 파일 경로(없으면 메모리)")
    parser.add_argument(
        "--reset", action="store_true", help="--store 파일이 있어도 시연 시작 상태로 되돌린다"
    )
    parser.add_argument(
        "--llm",
        choices=LLM_MODES,
        default=None,
        help="모델. 주지 않으면 환경 변수 V3_LLM, 그것도 없으면 rule(규칙 정책, 모델 미연결)",
    )
    parser.add_argument("--today", default=None, help="기준일 YYYY-MM-DD(없으면 오늘)")
    parser.add_argument(
        "--demo-closed", action="store_true", help="시연 되돌리기를 관리자 조직만 부르게 한다"
    )
    parser.add_argument(
        "--auth",
        choices=(httpio.AUTH_HEADER, httpio.AUTH_TOKEN),
        default=None,
        help="요청한 조직을 정하는 방법. header(기본, 시연 전용: X-Org-Id를 믿는다) | "
        "token(조직별 서비스 토큰: Authorization: Bearer). 주지 않으면 환경 변수 V3_AUTH",
    )
    parser.add_argument(
        "--org-tokens",
        default=None,
        help='--auth token일 때 조직별 토큰 JSON 파일 경로({"조직 ID": "토큰"}). 주지 않으면 '
        "환경 변수 V3_ORG_TOKENS_FILE(파일 경로)이나 V3_ORG_TOKENS(JSON 글)",
    )
    parser.add_argument(
        "--cors-origins",
        default=None,
        help="브라우저에서 부를 수 있게 더 허용할 출처(쉼표로 나눔). 주지 않으면 환경 변수 "
        "V3_CORS_ORIGINS. 기본 허용은 http://localhost:3000, http://127.0.0.1:3000",
    )
    args = parser.parse_args(argv)
    global ALLOWED_ORIGINS
    ALLOWED_ORIGINS = cors_origins(args.cors_origins or os.environ.get("V3_CORS_ORIGINS"))
    auth = load_auth(args.auth, args.org_tokens, os.environ)
    try:
        llm = normalize_mode(args.llm or os.environ.get("V3_LLM"))
    except LlmConfigError as error:
        raise SystemExit(str(error)) from None
    app = build_app(
        store_path=args.store,
        llm=llm,
        today=args.today,
        demo_open=not args.demo_closed,
        reset=args.reset,
    )
    server = make_server(app, args.host, args.port, quiet=False, auth=auth)
    mode = MODE_LABELS[llm]
    state = "이전 진단을 이어 갑니다" if app.resumed else "시연 시작 상태"
    who = (
        "조직 식별: token(조직별 서비스 토큰)"
        if auth.mode == httpio.AUTH_TOKEN
        else f"조직 식별: header(X-Org-Id). {httpio.HEADER_MODE_WARNING}"
    )
    remote = getattr(app.vendor_gateway, "_urls", None)
    where = f" · 따로 띄운 업체 측 에이전트: {', '.join(sorted(remote))}" if remote else ""
    sys.stderr.write(
        f"Re:Source AI v3 로컬 서버: http://{args.host}:{server.server_address[1]}{PREFIX} · "
        f"모델: {mode} · {state} · {who}{where}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
