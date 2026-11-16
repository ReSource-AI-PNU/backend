"""표준 라이브러리 HTTP 서버. 계약 8장의 API를 service 함수에 그대로 잇는다(로컬 시연, 개발용).

    python3 -m app.v3.devserver --port 8000 [--store 경로.json] [--llm rule|foundry]

- 시작할 때 가정값, 근거 시드(법령·기준값), 시연 데이터를 올린다. --store를 줘도 시작할 때
  시연 시작 상태로 덮어쓴다(근거 레지스트리가 메모리에만 있어 이전 진단을 이어 갈 수 없다).
- 기본은 --llm rule(규칙 정책, 모델 미연결)이다. --llm foundry일 때만 환경 변수
  FOUNDRY_ENDPOINT, FOUNDRY_DEPLOYMENT, FOUNDRY_API_KEY를 읽는다. 업체가 ollama를 골랐으면
  OLLAMA_BASE_URL이 있을 때만 그 모델을 쓰고, 없으면 그 업체는 규칙 정책으로 돈다(화면에 그렇게 나온다).
- 사진·영상 업로드(POST /intake/media)는 V3_DETECTOR_ENDPOINT(와 V3_DETECTOR_API_KEY)가 있을 때만
  탐지 모델을 부른다. 없으면 {"detectorUnavailable": true}로 답한다(탐지 결과를 지어내지 않는다).
- 요청하는 조직은 X-Org-Id 헤더로 밝힌다(시연용).
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
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from app.v3 import httpio, service
from app.v3.agents.llm import FoundryChat, OllamaChat
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.intake import detector
from app.v3.store import InMemoryStore, JsonFileStore

PREFIX = "/api/v3"
ALLOWED_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")
ALLOWED_HEADERS = "X-Org-Id, Content-Type"
ALLOWED_METHODS = "GET, POST, PUT, OPTIONS"
FOUNDRY_ENV = ("FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "FOUNDRY_API_KEY")


class Request:
    def __init__(
        self, method: str, path: str, query: dict, headers: Mapping[str, str], body: bytes
    ):
        self.method, self.path, self.query, self.headers, self.body = (
            method,
            path,
            query,
            headers,
            body,
        )
        self._form: tuple[dict[str, str], dict[str, tuple[str, bytes]]] | None = None

    @property
    def viewer(self) -> str:
        return (self.headers.get("X-Org-Id") or "").strip()

    def json(self) -> dict[str, Any]:
        """JSON이 아니면 400 INVALID_JSON, 객체가 아니면 422 INVALID_BODY(api.py와 같다)."""
        return httpio.parse_object(self.body)

    def form(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        if self._form is None:
            self._form = parse_multipart(self.headers.get("Content-Type") or "", self.body)
        return self._form

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
    content_type: str, body: bytes
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """multipart/form-data를 (글 항목, 파일)로 나눈다. 파일은 {항목 이름: (파일 이름, 내용)}."""
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
        else:
            fields[info["name"]] = data.decode("utf-8", "replace")
    return fields, files


class Raw:
    """JSON이 아닌 응답(양식 파일 내려받기)."""

    def __init__(self, body: bytes, content_type: str, filename: str | None = None) -> None:
        self.body, self.content_type, self.filename = body, content_type, filename


def route(app: service.App, request: Request) -> Any:
    """경로를 service 함수에 잇는다. 돌려준 값은 JSON으로 나간다."""
    if not request.path.startswith(PREFIX):
        raise service.ServiceError("NOT_FOUND", "그런 주소가 없습니다.", 404)
    parts = [unquote(p) for p in request.path[len(PREFIX) :].strip("/").split("/") if p]
    method, viewer = request.method, request.viewer
    match (method, parts):
        case ("GET", ["health"]):
            return {"ok": True}
        case ("POST", ["demo", "reset"]):
            return service.reset_demo(app, viewer or None)
        case ("GET", ["orgs"]):
            return service.list_orgs(app)
        case ("GET", ["orgs", org_id, "streams"]):
            return service.list_streams(app, org_id, viewer)
        case ("PUT", ["orgs", org_id, "streams", stream_id]):
            return service.update_stream(app, org_id, stream_id, request.json(), viewer)
        case ("GET", ["vendors", org_id]):
            return service.get_vendor(app, org_id, viewer)
        case ("PUT", ["vendors", org_id]):
            return service.update_vendor(app, org_id, request.json(), viewer)
        case ("POST", ["sessions"]):
            body = request.json()
            return service.create_session(app, str(body.get("emitterOrgId") or viewer), viewer)
        case ("GET", ["sessions", session_id]):
            return service.get_view(app, session_id, viewer)
        case ("POST", ["sessions", session_id, "run"]):
            return service.run_session(app, session_id, viewer)
        case ("POST", ["sessions", session_id, "answers"]):
            question_id, value = httpio.answer_body(request.json())
            return service.answer_question(app, session_id, question_id, value, viewer)
        case ("POST", ["sessions", session_id, "approvals"]):
            thread_id = str(httpio.required(request.json(), "threadId"))
            return service.approve(app, session_id, thread_id, viewer)
        case ("POST", ["sessions", session_id, "corrections"]):
            return service.record_correction(app, session_id, request.json(), viewer)
        case ("POST", ["sessions", session_id, "outcomes"]):
            return service.record_outcome(app, session_id, request.json(), viewer)
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
        case ("GET", ["admin", "status"]):
            return service.admin_status(app, viewer)
        case ("POST", ["admin", "eval"]):
            return service.run_eval(app, viewer)
        case ("GET", ["admin", "retrain", "export"]):
            return service.export_retraining(app, viewer)
        case ("POST", ["admin", "retrain", "runs"]):
            return service.register_model_run(app, request.json(), viewer)
    raise service.ServiceError("NOT_FOUND", "그런 주소가 없습니다.", 404)


def make_handler(app: service.App, lock: threading.Lock | None = None, quiet: bool = True):
    guard = lock or threading.Lock()

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
                request = Request(self.command, url.path, parse_qs(url.query), self.headers, body)
                with guard:  # 저장소가 동시 쓰기를 막지 않으므로 한 번에 한 요청만 처리한다
                    result = route(app, request)
            except service.ServiceError as error:
                self._json(error.status, error.to_dict())
            except Exception:  # noqa: BLE001 - 서버는 죽지 않고 500으로 답한다
                traceback.print_exc(file=sys.stderr)
                self._json(
                    500, {"error": {"code": "INTERNAL", "message": "서버에서 오류가 났습니다."}}
                )
            else:
                if isinstance(result, Raw):
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


def make_server(app: service.App, host: str = "127.0.0.1", port: int = 8000, quiet: bool = True):
    server = ThreadingHTTPServer((host, port), make_handler(app, quiet=quiet))
    server.daemon_threads = True
    return server


def foundry_factory(env: Mapping[str, str]) -> Callable[..., Any]:
    """llm_factory(role, org_id, model). 값이 빠져 있으면 어떤 값이 없는지만 밝히고 멈춘다."""
    missing = [name for name in FOUNDRY_ENV if not env.get(name)]
    if missing:
        raise SystemExit(f"--llm foundry에는 환경 변수가 필요합니다: {', '.join(missing)}")
    endpoint, deployment, key = (env[name] for name in FOUNDRY_ENV)
    ollama = (env.get("OLLAMA_BASE_URL") or "").strip()

    def factory(role: str, _org_id: str, model: dict[str, Any] | None):
        if role == "vendor" and (model or {}).get("provider") == "ollama":
            # 주소가 없으면 None을 돌려줘 규칙 정책으로 돌게 한다(화면에 모델 미연결로 나온다)
            return OllamaChat(ollama, str(model["name"])) if ollama else None
        return FoundryChat(endpoint, deployment, key)

    return factory


def build_app(
    *,
    store_path: str | None = None,
    llm: str = "rule",
    today: str | None = None,
    env: Mapping[str, str] | None = None,
    demo_open: bool = True,
) -> service.App:
    store = JsonFileStore(store_path) if store_path else InMemoryStore()
    settings = os.environ if env is None else env
    factory = foundry_factory(settings) if llm == "foundry" else None
    app = service.App(
        store,
        InMemoryRegistry(),
        llm_factory=factory,
        today=today,
        demo_open=demo_open,
        # V3_DETECTOR_ENDPOINT가 없으면 None이고, 사진·영상 업로드는 "미연결"이라고 답한다
        detector=detector.client_from_env(settings),
    )
    service.seed(app)
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re:Source AI v3 로컬 서버(표준 라이브러리)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None, help="저장소를 쓸 JSON 파일 경로(없으면 메모리)")
    parser.add_argument("--llm", choices=("rule", "foundry"), default="rule")
    parser.add_argument("--today", default=None, help="기준일 YYYY-MM-DD(없으면 오늘)")
    parser.add_argument(
        "--demo-closed", action="store_true", help="시연 되돌리기를 관리자 조직만 부르게 한다"
    )
    args = parser.parse_args(argv)
    app = build_app(
        store_path=args.store, llm=args.llm, today=args.today, demo_open=not args.demo_closed
    )
    server = make_server(app, args.host, args.port, quiet=False)
    mode = "규칙 정책(모델 미연결)" if args.llm == "rule" else "Foundry"
    sys.stderr.write(
        f"Re:Source AI v3 로컬 서버: http://{args.host}:{server.server_address[1]}{PREFIX} · 모델: {mode}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
