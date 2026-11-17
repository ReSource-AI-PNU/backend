"""설계안 v3 HTTP API (FastAPI). 계약 8장의 경로를 app.v3.service 함수에 그대로 연결한다.

주의: 이 파일은 작성한 환경에서 실행해 보지 못했다(fastapi를 불러올 수 없었다).
tests/v3/test_api.py가 CI에서 확인한다.

- 요청하는 조직을 정하는 방법은 설정 V3_AUTH로 고른다(httpio.Auth).
  - header(기본): X-Org-Id 헤더를 그대로 믿는다. 시연 전용이며 공개 주소에 그대로 두면 조직 경계가
    지켜지지 않는다.
  - token: 조직별 서비스 토큰. Authorization: Bearer <토큰>으로 조직을 정하고 X-Org-Id는 무시한다.
    토큰과 조직의 짝은 설정 V3_ORG_TOKENS(JSON, Key Vault에서 주입)로 받고 hmac.compare_digest로
    견준다. GET /orgs는 자기 조직과 대화 상대만 준다. Entra 인증은 아직 연결하지 않았다.
- 오류는 {"error": {"code", "message"}}와 상태 코드로 낸다. 본문은 FastAPI 검증에 맡기지 않고
  직접 읽는다(검증 실패 응답의 모양을 계약과 같게 하려는 것).
- 서비스 함수는 작업 스레드에서 부른다. 메모리 저장소일 때는 한 번에 하나씩만 부른다.
- 판단과 숫자는 여기서 만들지 않는다. 받은 값을 넘기고 돌려받은 값을 그대로 내보낸다.
"""

import functools
import logging
import threading
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.core.config import settings
from app.v3 import httpio, service
from app.v3.agents.factory import build_llm_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v3", tags=["v3"])

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
    anthropic_api_key: str = "",
    anthropic_api_key_file: str = "",
    anthropic_model: str = "",
    all_vendors: bool = False,
) -> Callable[[str, str, dict[str, Any] | None], Any]:
    """llm_factory(role, org_id, model) -> LLM | None. None이면 service가 규칙 정책으로 돈다.

    모델을 고르는 규칙은 로컬 서버와 같은 함수(agents/factory.py의 build_llm_factory)가 정한다.
    - mode는 rule | foundry | anthropic. 그 밖의 값이면 ValueError.
    - rule이면 항상 None.
    - 업체가 ollama를 골랐으면 OLLAMA_BASE_URL이 있을 때만 OllamaChat을 준다. 주소가 없으면
      다른 모델로 바꿔 부르지 않고 None을 준다(쓰지 않은 모델을 쓴 것처럼 보이지 않게).
      V3_LLM=anthropic이고 V3_LLM_ALL_VENDORS=1일 때만 그 업체도 anthropic 모델이 답한다.
    - foundry는 FOUNDRY_* 세 값이 모두 있을 때, anthropic은 키(ANTHROPIC_API_KEY나
      ANTHROPIC_API_KEY_FILE)가 있을 때 그 모델을 준다. 빠져 있으면 경고를 남기고 규칙 정책으로 돈다.
    """
    env = {
        "FOUNDRY_ENDPOINT": endpoint,
        "FOUNDRY_DEPLOYMENT": deployment,
        "FOUNDRY_API_KEY": api_key,
        "OLLAMA_BASE_URL": ollama_base_url,
        "ANTHROPIC_API_KEY": anthropic_api_key,
        "ANTHROPIC_API_KEY_FILE": anthropic_api_key_file,
        "ANTHROPIC_MODEL": anthropic_model,
        "V3_LLM_ALL_VENDORS": "1" if all_vendors else "",
    }
    built = build_llm_factory(mode, env, strict=False)
    if built is not None:
        return built

    def rule(role: str, org_id: str, model: dict[str, Any] | None = None) -> Any:
        return None

    return rule


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
    """설정에 따라 service.App을 만든다. DATABASE_URL이 있으면 SQL 저장, 없으면 메모리.

    Azure 연결부는 설정이 있을 때만 켠다(이 부분은 로컬 미실행):
    - APPLICATIONINSIGHTS_CONNECTION_STRING: 실행 기록을 OpenTelemetry span으로 내보낸다(tracing.py).
    - AZURE_MAPS_KEY: 판단 도구의 도로 거리(judge/distance.py). 없으면 직선 거리 가정값 경로다.
      V3_DISTANCE=kakao와 KAKAO_REST_API_KEY를 주면 카카오모빌리티 길찾기로 도로 거리를 받는다
      (이쪽은 실제 호출로 확인했다).
    - AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_API_KEY: 근거 검색의 순위를 Azure AI Search에서 받는다
      (evidence/search.py의 선택 계층). 없거나 설정이 틀리면 로컬 검색이다.
    - V3_AGENT_FRAMEWORK=chat|agent + FOUNDRY_PROJECT_ENDPOINT: Foundry로 가는 모델 호출을
      Microsoft Agent Framework로 한다(agents/maf.py). 비어 있으면 지금처럼 urllib로 부른다.
    """
    import os

    from app.v3 import tracing
    from app.v3.agents import maf
    from app.v3.evidence import search, search_azure
    from app.v3.intake import detector
    from app.v3.judge.distance import distance_provider_from_env

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
        anthropic_api_key=settings.anthropic_api_key,
        anthropic_api_key_file=settings.anthropic_api_key_file,
        anthropic_model=settings.anthropic_model,
        all_vendors=settings.v3_llm_all_vendors,
    )
    factory = maf.factory_from_env(factory, os.environ)
    store = tracing.enable(store, os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", ""))
    search_env = {
        **os.environ,
        "AZURE_SEARCH_ENDPOINT": settings.azure_search_endpoint,
        "AZURE_SEARCH_API_KEY": settings.azure_search_api_key,
        "OLLAMA_BASE_URL": settings.ollama_base_url,
        "FOUNDRY_ENDPOINT": settings.foundry_endpoint,
        "FOUNDRY_API_KEY": settings.foundry_api_key,
    }
    try:
        search_client = search_azure.client_from_env(search_env)
    except ValueError as error:
        logger.warning("Azure AI Search 설정이 틀려 로컬 검색으로 돈다: %s", error)
        search_client = None
    search.use_backend(search_client.search if search_client is not None else None)
    auth = current_auth()  # 설정이 틀리면(모르는 모드, token인데 토큰 없음) 여기서 멈춘다
    if auth.mode == httpio.AUTH_HEADER:
        logger.warning("V3_AUTH=header: %s", httpio.HEADER_MODE_WARNING)
    return service.App(
        store,
        registry,
        auth_mode=auth.mode,
        llm_factory=factory,
        vendor_gateway=make_vendor_gateway(),
        # V3_DISTANCE=kakao|azure|none. 비어 있으면 AZURE_MAPS_KEY가 있을 때 Azure Maps다.
        # 고른 제공자의 키가 없으면 경고를 남기고 직선 거리 가정값 경로로 돈다
        distance_provider=distance_provider_from_env(
            {
                "V3_DISTANCE": settings.v3_distance,
                "AZURE_MAPS_KEY": settings.azure_maps_key,
                "KAKAO_REST_API_KEY": settings.kakao_rest_api_key,
            }
        ),
        demo_open=settings.v3_demo_open,
        # 탐지 모델(Azure ML 엔드포인트). 주소가 없으면 None이고 업로드는 "미연결"이라고 답한다
        detector=detector.client_from_env(
            {
                detector.ENV_ENDPOINT: settings.v3_detector_endpoint,
                detector.ENV_KEY: settings.v3_detector_api_key,
                detector.ENV_DEPLOYMENT: settings.v3_detector_deployment,
            }
        ),
    )


def current_auth() -> httpio.Auth:
    """설정 V3_AUTH, V3_ORG_TOKENS(또는 V3_ORG_TOKENS_FILE)로 요청한 조직을 정하는 방법을 만든다.

    설정이 틀리면 ValueError. header 모드에서는 토큰 설정을 읽지 않는다(파일도 열지 않는다).
    """
    if (settings.v3_auth or "").strip().lower() != httpio.AUTH_TOKEN:
        return httpio.make_auth(settings.v3_auth)
    return httpio.make_auth(
        settings.v3_auth,
        httpio.tokens_text(settings.v3_org_tokens, settings.v3_org_tokens_file),
    )


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
def viewer_of(request: Request, *, required: bool = True) -> str:
    """요청한 조직. header 모드는 X-Org-Id, token 모드는 Authorization: Bearer 토큰으로 정한다."""
    return current_auth().viewer(request.headers, required=required)


async def json_object(request: Request) -> dict[str, Any]:
    """본문을 JSON 객체로 읽는다. JSON이 아니면 400 INVALID_JSON, 객체가 아니면 422 INVALID_BODY."""
    return httpio.parse_object(await request.body())


def required(body: dict[str, Any], key: str) -> Any:
    return httpio.required(body, key)


async def read_upload(request: Request, default_name: str) -> tuple[dict[str, str], str, bytes]:
    """(필드, 파일 이름, 내용). multipart/form-data의 file 필드나, 본문 그대로(필드는 질의 문자열)."""
    if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        try:
            form = await request.form()
        except HTTPException:
            # starlette는 읽을 수 없는 multipart(경계 없음 등)를 HTTPException(400)으로 알린다.
            # 그대로 두면 guarded가 500 INTERNAL로 바꾸므로 devserver와 같은 코드로 답한다.
            raise service.ServiceError(
                "INVALID_MULTIPART", "multipart/form-data 본문을 읽을 수 없습니다.", 400
            ) from None
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            upload = next((value for value in form.values() if not isinstance(value, str)), None)
        if upload is None:
            raise httpio.file_required()
        fields = {key: value for key, value in form.items() if isinstance(value, str)}
        filename = upload.filename or fields.get("filename", "")
        data = await upload.read()
    else:
        fields = dict(request.query_params)
        filename = fields.get("filename", "")
        data = await request.body()
    return fields, filename or default_name, httpio.check_upload(data)


# ── 시연, 조직, 흐름, 업체 ───────────────────────────────────────────
@router.get("/health")
@guarded
async def health(request: Request):
    return {"ok": True}


@router.post("/demo/reset")
@guarded
async def demo_reset(request: Request):
    # 관리자만(설정 V3_DEMO_OPEN이 참이면 누구나). 조직을 밝히지 않았으면 서비스가 401로 답한다
    viewer = viewer_of(request, required=False) or None
    return await call(service.reset_demo, viewer)


@router.get("/orgs")
@guarded
async def orgs(request: Request):
    # header 모드(시연 전용)는 누구에게나 모든 조직을 준다. token 모드는 자기 조직과 대화 상대만 준다
    auth = current_auth()
    viewer = auth.viewer(request.headers, required=auth.scoped) or None
    return await call(service.list_orgs, viewer, auth.scoped)


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
    return await call(service.create_session, str(body.get("emitterOrgId") or viewer), viewer)


@router.get("/sessions")
@guarded
async def session_list(request: Request):
    return await call(service.list_sessions, viewer_of(request))


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
    question_id, value = httpio.answer_body(body)
    return await call(service.answer_question, session_id, question_id, value, viewer)


@router.post("/sessions/{session_id}/approvals")
@guarded
async def approve(session_id: str, request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    return await call(service.approve, session_id, str(required(body, "threadId")), viewer)


# ── 재학습 데이터 수집 (7단계) ───────────────────────────────────────
@router.post("/sessions/{session_id}/corrections")
@guarded
async def record_correction(session_id: str, request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    return await call(service.record_correction, session_id, body, viewer)


@router.post("/sessions/{session_id}/outcomes")
@guarded
async def record_outcome(session_id: str, request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    return await call(service.record_outcome, session_id, body, viewer)


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
    fields, filename, data = await read_upload(request, "측정값.csv")
    org_id = fields.get("orgId") or viewer
    return await call(service.upload_csv, org_id, filename, data, viewer)


@router.post("/intake/documents")
@guarded
async def upload_document(request: Request):
    viewer = viewer_of(request)
    fields, filename, data = await read_upload(request, "문서")
    org_id = fields.get("orgId") or viewer
    kind = fields.get("kind") or ""  # 종류가 없거나 틀리면 서비스가 INVALID_KIND로 답한다
    return await call(service.upload_document, org_id, kind, filename, data, viewer)


@router.post("/intake/media")
@guarded
async def upload_media(request: Request):
    viewer = viewer_of(request)
    fields, filename, data = await read_upload(request, "사진")
    org_id = fields.get("orgId") or viewer
    stream_id = fields.get("streamId") or ""
    return await call(service.upload_media, org_id, stream_id, filename, data, viewer)


@router.post("/intake/documents/confirm")
@guarded
async def confirm_document(request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    fields = body.get("fields") or []
    if not isinstance(fields, list):
        raise httpio.invalid_body("fields는 배열이어야 합니다.")
    org_id = str(body.get("orgId") or viewer)
    name = str(body.get("documentName") or "")
    return await call(service.confirm_document, org_id, name, fields, viewer)


# ── 관리자 ───────────────────────────────────────────────────────────
@router.get("/admin/status")
@guarded
async def admin_status(request: Request):
    return await call(service.admin_status, viewer_of(request))


@router.post("/admin/eval")
@guarded
async def admin_eval(request: Request):
    return await call(service.run_eval, viewer_of(request))


@router.get("/admin/retrain/export")
@guarded
async def admin_retrain_export(request: Request):
    return await call(service.export_retraining, viewer_of(request))


@router.post("/admin/retrain/runs")
@guarded
async def admin_retrain_runs(request: Request):
    viewer = viewer_of(request)
    body = await json_object(request)
    return await call(service.register_model_run, body, viewer)
