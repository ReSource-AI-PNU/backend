"""설계안 v3 HTTP API (FastAPI). 계약 8장의 경로를 app.v3.service 함수에 그대로 연결한다.

주의: 이 파일은 작성한 환경에서 실행해 보지 못했다(fastapi를 불러올 수 없었다).
tests/v3/test_api.py가 CI에서 확인한다.

- 요청하는 조직은 X-Org-Id 헤더로 받는다(시연용. 실제 인증은 아직 연결하지 않았다).
- 오류는 {"error": {"code", "message"}}와 상태 코드로 낸다. 본문은 FastAPI 검증에 맡기지 않고
  직접 읽는다(검증 실패 응답의 모양을 계약과 같게 하려는 것).
- 서비스 함수는 작업 스레드에서 부른다. 메모리 저장소일 때는 한 번에 하나씩만 부른다.
- 판단과 숫자는 여기서 만들지 않는다. 받은 값을 넘기고 돌려받은 값을 그대로 내보낸다.
"""

import functools
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.v3 import service
from app.v3.agents.llm import FoundryChat, OllamaChat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v3", tags=["v3"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 업로드 크기 상한(운영 한도)
CSV_TEMPLATE_NAME = "resource-ai-measure-template.csv"


class ApiError(Exception):
    """요청 모양이 틀렸을 때. 서비스 함수의 ServiceError와 같은 모양으로 나간다."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# ── 앱 상태 ──────────────────────────────────────────────────────────
def make_llm_factory(
    mode: str,
    *,
    endpoint: str = "",
    deployment: str = "",
    api_key: str = "",
    ollama_base_url: str = "",
) -> Callable[[str, str, dict[str, Any] | None], Any]:
    """llm_factory(role, org_id, model) -> LLM | None. None이면 service가 규칙 정책으로 돈다.

    - mode가 rule이면 항상 None.
    - 업체가 ollama를 골랐으면 OLLAMA_BASE_URL이 있을 때만 OllamaChat을 준다. 주소가 없으면
      다른 모델로 바꿔 부르지 않고 None을 준다(쓰지 않은 모델을 쓴 것처럼 보이지 않게).
    - 그 밖에는 FOUNDRY_* 세 값이 모두 있을 때 FoundryChat을 준다.
    """
    if mode not in ("rule", "foundry"):
        raise ValueError(f"V3_LLM은 rule이나 foundry여야 한다: {mode!r}")
    foundry_ready = bool(endpoint and deployment and api_key)
    if mode == "foundry" and not foundry_ready:
        logger.warning("V3_LLM=foundry인데 FOUNDRY_* 설정이 비어 있어 규칙 정책으로 돈다")

    def factory(role: str, org_id: str, model: dict[str, Any] | None = None) -> Any:
        if mode == "rule":
            return None
        chosen = model or {}
        if role == "vendor" and chosen.get("provider") == "ollama":
            if ollama_base_url and chosen.get("name"):
                return OllamaChat(ollama_base_url, str(chosen["name"]))
            logger.warning("%s: Ollama 주소나 모델 이름이 없어 규칙 정책으로 돈다", org_id)
            return None
        if foundry_ready:
            return FoundryChat(endpoint, deployment, api_key)
        return None

    return factory


def make_vendor_gateway() -> Any:
    """V3_VENDOR_AGENTS가 있으면 HTTP 게이트웨이, 없으면 None(service가 같은 프로세스에서 부른다).

    업체 측 에이전트 서비스 자신(VENDOR_ORG_ID가 있는 프로세스)은 다시 HTTP로 넘기지 않는다.
    """
    if not settings.v3_vendor_agents or settings.vendor_org_id:
        return None
    if not settings.vendor_service_key:
        raise RuntimeError("V3_VENDOR_AGENTS를 쓰려면 VENDOR_SERVICE_KEY가 있어야 한다")
    from app.v3.agents.gateway import HttpVendorGateway

    return HttpVendorGateway(dict(settings.v3_vendor_agents), settings.vendor_service_key)


def build_app() -> service.App:
    """설정에 따라 service.App을 만든다. DATABASE_URL이 있으면 SQL 저장, 없으면 메모리."""
    if settings.database_url:
        from app.platform.db import engine
        from app.v3.sql import SqlRegistry, SqlStore

        store: Any = SqlStore(engine())
        registry: Any = SqlRegistry(engine())
    else:
        from app.v3.evidence.registry import InMemoryRegistry
        from app.v3.store import InMemoryStore

        store, registry = InMemoryStore(), InMemoryRegistry()
    factory = make_llm_factory(
        settings.v3_llm,
        endpoint=settings.foundry_endpoint,
        deployment=settings.foundry_deployment,
        api_key=settings.foundry_api_key,
        ollama_base_url=settings.ollama_base_url,
    )
    return service.App(store, registry, llm_factory=factory, vendor_gateway=make_vendor_gateway())


_state: service.App | None = None
_state_lock = threading.Lock()
_memory_lock = threading.RLock()


def get_state() -> service.App:
    """처음 부를 때 한 번 만든다."""
    global _state
    with _state_lock:
        if _state is None:
            _state = build_app()
        return _state


def set_state(app: service.App | None) -> None:
    """테스트에서 앱 상태를 바꾸거나(None이면 다음 요청 때 다시 만든다) 비운다."""
    global _state
    with _state_lock:
        _state = app


def _invoke(fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
    app = get_state()
    if settings.database_url:
        return fn(app, *args)
    with _memory_lock:  # 메모리 저장소는 동시에 고치면 깨진다
        return fn(app, *args)


async def call(fn: Callable[..., Any], *args: Any) -> Any:
    """service 함수를 fn(app, *args)로 작업 스레드에서 부른다."""
    return await run_in_threadpool(_invoke, fn, args)


def guarded(handler: Callable[..., Any]) -> Callable[..., Any]:
    """오류를 계약의 모양으로 바꾼다. 예상하지 못한 오류의 내용은 응답에 싣지 않는다."""

    @functools.wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except ApiError as error:
            return error_response(error.code, error.message, error.status)
        except service.ServiceError as error:
            return error_response(
                str(getattr(error, "code", "SERVICE_ERROR")),
                str(getattr(error, "message", "") or error),
                int(getattr(error, "status", 400)),
            )
        except Exception:
            logger.exception("v3 API 처리 중 오류")
            return error_response("INTERNAL", "서버에서 오류가 났습니다.", 500)

    return wrapped


# ── 요청 읽기 ────────────────────────────────────────────────────────
def viewer_of(request: Request) -> str:
    viewer = (request.headers.get("x-org-id") or "").strip()
    if not viewer:
        raise ApiError("ORG_REQUIRED", "X-Org-Id 헤더로 조직을 밝혀야 합니다.", 401)
    return viewer


async def json_object(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise ApiError("INVALID_REQUEST", "본문이 JSON이 아닙니다.") from None
    if not isinstance(data, dict):
        raise ApiError("INVALID_REQUEST", "본문은 JSON 객체여야 합니다.")
    return data


def required(body: dict[str, Any], key: str) -> Any:
    if key not in body or body[key] in (None, ""):
        raise ApiError("INVALID_REQUEST", f"필수 항목이 없습니다: {key}")
    return body[key]


async def read_upload(request: Request) -> tuple[dict[str, str], str, bytes]:
    """(필드, 파일 이름, 내용). multipart/form-data의 file 필드나, 본문 그대로(필드는 질의 문자열)."""
    if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ApiError("INVALID_REQUEST", "file 필드에 파일을 넣어야 합니다.")
        fields = {key: value for key, value in form.items() if isinstance(value, str)}
        filename = upload.filename or fields.get("filename", "")
        data = await upload.read()
    else:
        fields = dict(request.query_params)
        filename = fields.get("filename", "")
        data = await request.body()
    if not data:
        raise ApiError("INVALID_REQUEST", "빈 파일입니다.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError("FILE_TOO_LARGE", "파일이 너무 큽니다.", 413)
    return fields, filename, data


# ── 시연, 조직, 흐름, 업체 ───────────────────────────────────────────
@router.post("/demo/reset")
@guarded
async def demo_reset(request: Request):
    return await call(service.reset_demo)


@router.get("/orgs")
@guarded
async def orgs(request: Request):
    return await call(service.list_orgs)


@router.get("/orgs/{org_id}/streams")
@guarded
async def streams(org_id: str, request: Request):
    return await call(service.list_streams, org_id, viewer_of(request))


@router.put("/orgs/{org_id}/streams/{stream_id}")
@guarded
async def put_stream(org_id: str, stream_id: str, request: Request):
    viewer = viewer_of(request)
    patch = await json_object(request)
    return await call(service.update_stream, org_id, stream_id, patch, viewer)


@router.get("/vendors/{org_id}")
@guarded
async def vendor(org_id: str, request: Request):
    return await call(service.get_vendor, org_id, viewer_of(request))


@router.put("/vendors/{org_id}")
@guarded
async def put_vendor(org_id: str, request: Request):
    viewer = viewer_of(request)
    patch = await json_object(request)
    return await call(service.update_vendor, org_id, patch, viewer)


# ── 진단 ─────────────────────────────────────────────────────────────
@router.post("/sessions")
@guarded
async def create_session(request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    return await call(service.create_session, str(required(body, "emitterOrgId")), viewer)


@router.get("/sessions/{session_id}")
@guarded
async def session_view(session_id: str, request: Request):
    return await call(service.get_view, session_id, viewer_of(request))


@router.post("/sessions/{session_id}/run")
@guarded
async def run_session(session_id: str, request: Request):
    return await call(service.run_session, session_id, viewer_of(request))


@router.post("/sessions/{session_id}/answers")
@guarded
async def answer(session_id: str, request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    question_id = str(required(body, "questionId"))
    if "value" not in body or body["value"] is None:
        raise ApiError("INVALID_REQUEST", '필수 항목이 없습니다: value (모르면 "unknown")')
    return await call(service.answer_question, session_id, question_id, body["value"], viewer)


@router.post("/sessions/{session_id}/approvals")
@guarded
async def approve(session_id: str, request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    return await call(service.approve, session_id, str(required(body, "threadId")), viewer)


# ── 근거 ─────────────────────────────────────────────────────────────
@router.get("/evidence")
@guarded
async def search_evidence(request: Request):
    viewer = viewer_of(request)
    query = request.query_params.get("q", "")
    kinds = [
        kind.strip()
        for value in request.query_params.getlist("kind")
        for kind in value.split(",")
        if kind.strip()
    ]
    return await call(service.search_evidence, query, kinds or None, viewer)


@router.get("/evidence/{evidence_id:path}")
@guarded
async def evidence(evidence_id: str, request: Request):
    return await call(service.get_evidence, evidence_id, viewer_of(request))


# ── 자료 올리기 ──────────────────────────────────────────────────────
@router.get("/intake/csv/template")
@guarded
async def csv_template(request: Request):
    data = await call(service.csv_template)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{CSV_TEMPLATE_NAME}"'},
    )


@router.post("/intake/csv")
@guarded
async def upload_csv(request: Request):
    viewer = viewer_of(request)
    fields, filename, data = await read_upload(request)
    org_id = fields.get("orgId") or viewer
    return await call(service.upload_csv, org_id, filename, data, viewer)


@router.post("/intake/documents")
@guarded
async def upload_document(request: Request):
    viewer = viewer_of(request)
    fields, filename, data = await read_upload(request)
    org_id = fields.get("orgId") or viewer
    kind = fields.get("kind") or ""
    if not kind:
        raise ApiError("INVALID_REQUEST", "필수 항목이 없습니다: kind")
    return await call(service.upload_document, org_id, kind, filename, data, viewer)


# ── 관리자 ───────────────────────────────────────────────────────────
@router.get("/admin/status")
@guarded
async def admin_status(request: Request):
    return await call(service.admin_status, viewer_of(request))


@router.post("/admin/eval")
@guarded
async def admin_eval(request: Request):
    return await call(service.run_eval, viewer_of(request))
