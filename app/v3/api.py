"""설계안 v3 HTTP API (FastAPI). 계약 8장의 경로를 app.v3.service 함수에 그대로 연결한다.

주의: 이 파일은 작성한 환경에서 실행해 보지 못했다(fastapi를 불러올 수 없었다).
tests/v3/test_api.py가 CI에서 확인한다.

- 요청하는 조직을 정하는 방법은 설정 V3_AUTH로 고른다(httpio.Auth).
  - header(기본): X-Org-Id 헤더를 그대로 믿는다. 시연 전용이며 공개 주소에 그대로 두면 조직 경계가
    지켜지지 않는다.
  - token: 조직별 서비스 토큰. Authorization: Bearer <토큰>으로 조직을 정하고 X-Org-Id는 무시한다.
    토큰과 조직의 짝은 설정 V3_ORG_TOKENS(JSON, Key Vault에서 주입)로 받고 hmac.compare_digest로
    견준다. GET /orgs는 자기 조직과 대화 상대만 준다. Entra 인증은 아직 연결하지 않았다.
  - bff: 프런트 서버(Next.js BFF)만 부른다. Authorization: Bearer <V3_BFF_KEY>, X-User-Id, X-Org-Id.
    사용자가 X-Org-Id의 구성원인지 계정 저장소로 확인한다(아니면 403 NOT_A_MEMBER). X-Demo-View: 1이면
    체험하기다: X-User-Id 없이 시연 조직만, GET만 받고 서비스 함수를 시연 조직만 보이는 읽기 전용 앱
    (demo/scope.py)으로 부른다. 계정·조직 경로는 accounts_api.py에 있다.
- 오류는 {"error": {"code", "message"}}와 상태 코드로 낸다. 본문은 FastAPI 검증에 맡기지 않고
  직접 읽는다(검증 실패 응답의 모양을 계약과 같게 하려는 것).
- 서비스 함수는 작업 스레드에서 부른다. 메모리 저장소일 때는 한 번에 하나씩만 부른다.
- POST /sessions/{id}/run?mode=async는 실행 자리를 차지하고 202를 준 뒤 뒤에서 도는 스레드로 돌린다.
  메모리 저장소면 그 스레드는 저장소 호출 하나마다 같은 잠금을 잡는다(요청과 번갈아 돈다).
  GET /sessions/{id}/stream은 진행 기록을 SSE로 보낸다(계약 8장 "실시간 진행").
- 판단과 숫자는 여기서 만들지 않는다. 받은 값을 넘기고 돌려받은 값을 그대로 내보낸다.
"""

import asyncio
import contextvars
import functools
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.core.config import settings
from app.v3 import accounts, httpio, live, service
from app.v3.agents.factory import build_llm_factory
from app.v3.demo.scope import demo_app

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
    reasoning_effort: str = "",
    max_completion_tokens: str = "",
    ollama_num_ctx: str = "",
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
        "FOUNDRY_REASONING_EFFORT": reasoning_effort,
        "FOUNDRY_MAX_COMPLETION_TOKENS": max_completion_tokens,
        "OLLAMA_NUM_CTX": ollama_num_ctx,
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

    Azure 연결부는 설정이 있을 때만 켠다. 값은 모두 settings(app/core/config.py)에서 읽는다:
    - APPLICATIONINSIGHTS_CONNECTION_STRING: 실행 기록을 OpenTelemetry span으로 내보낸다(tracing.py).
    - AZURE_MAPS_KEY: 판단 도구의 도로 거리(judge/distance.py). 없으면 직선 거리 가정값 경로다.
      V3_DISTANCE=kakao와 KAKAO_REST_API_KEY를 주면 카카오모빌리티 길찾기로 도로 거리를 받는다
      (이쪽은 실제 호출로 확인했다).
    - AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_API_KEY(+ AZURE_SEARCH_INDEX, V3_EMBEDDINGS와 그 값들):
      근거 검색의 순위를 Azure AI Search에서 받는다(evidence/search.py의 선택 계층). 없거나 설정이
      틀리면 로컬 검색이다. 질의 임베딩은 색인 스크립트(scripts/v3/index_evidence.py)와 같은 함수로 만든다.
    - AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT, _API_KEY: 문서 업로드를 prebuilt-layout으로 읽는다.
      없으면 UTF-8 글 파일만 읽는다(extraction "text_only").
    - AZURE_CONTENT_SAFETY_ENDPOINT, _API_KEY: 업로드 문서를 Prompt Shields로 검사한다.
      없으면 검사하지 않고 shieldUnavailable=true로 표시한다.
    - V3_AGENT_FRAMEWORK=chat|agent + FOUNDRY_PROJECT_ENDPOINT: Foundry로 가는 모델 호출을
      Microsoft Agent Framework로 한다(agents/maf.py). 비어 있으면 지금처럼 urllib로 부른다.
    """
    import os

    from app.v3 import tracing
    from app.v3.agents import maf
    from app.v3.evidence import rerank, search, search_azure
    from app.v3.intake import detector, documents, shields
    from app.v3.judge.distance import distance_provider_from_env

    if settings.database_url:
        from app.platform.db import engine
        from app.v3.sql import SqlAccounts, SqlRegistry, SqlStore

        store: Any = SqlStore(engine())
        registry: Any = SqlRegistry(engine())
        account_store: Any = SqlAccounts(engine())
    else:
        from app.v3.account_store import InMemoryAccounts
        from app.v3.evidence.registry import InMemoryRegistry
        from app.v3.store import InMemoryStore

        store, registry = InMemoryStore(), InMemoryRegistry()
        account_store = InMemoryAccounts()
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
        reasoning_effort=settings.foundry_reasoning_effort,
        max_completion_tokens=settings.foundry_max_completion_tokens,
        ollama_num_ctx=settings.ollama_num_ctx,
    )
    factory = maf.factory_from_env(factory, os.environ)
    store = tracing.enable(store, settings.applicationinsights_connection_string)
    try:
        search_client = search_azure.client_from_env(search_settings())
    except ValueError as error:
        logger.warning("Azure AI Search 설정이 틀려 로컬 검색으로 돈다: %s", error)
        search_client = None
    search.use_backend(search_client.search if search_client is not None else None)
    search.use_reranker(
        rerank.reranker_from_env(
            {
                "V3_LLM": settings.v3_llm,
                "FOUNDRY_ENDPOINT": settings.foundry_endpoint,
                "FOUNDRY_DEPLOYMENT": settings.foundry_deployment,
                "FOUNDRY_API_KEY": settings.foundry_api_key,
                rerank.ENV_SWITCH: os.environ.get(rerank.ENV_SWITCH, ""),
            }
        )
    )
    # 둘 다 주소와 키가 모두 있을 때만 켜진다. 없으면 None이고 upload_document가 그렇다고 표시한다
    document_client = documents.client_from_env(
        {
            documents.ENV_ENDPOINT: settings.azure_document_intelligence_endpoint,
            documents.ENV_KEY: settings.azure_document_intelligence_api_key,
        }
    )
    shield_client = shields.client_from_env(
        {
            shields.ENV_ENDPOINT: settings.azure_content_safety_endpoint,
            shields.ENV_KEY: settings.azure_content_safety_api_key,
        }
    )
    logger.info(
        "근거 검색 %s(재순위 %s), Document Intelligence %s, Prompt Shields %s",
        search.backend_name(),
        search.reranker_name() or "없음",
        "연결" if document_client else "미연결",
        "연결" if shield_client else "미연결",
    )
    auth = current_auth()  # 설정이 틀리면(모르는 모드, token인데 토큰 없음) 여기서 멈춘다
    if auth.mode == httpio.AUTH_HEADER:
        logger.warning("V3_AUTH=header: %s", httpio.HEADER_MODE_WARNING)
    if auth.mode == httpio.AUTH_BFF and not auth.bff_key:
        logger.warning("V3_AUTH=bff인데 V3_BFF_KEY가 비어 있어 모든 요청을 503으로 거절한다")
    return service.App(
        store,
        registry,
        auth_mode=auth.mode,
        accounts=account_store,
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
        documents=document_client,
        shields=shield_client,
    )


def search_settings() -> dict[str, str]:
    """search_azure.client_from_env에 넘길 값. 색인 스크립트가 os.environ에서 읽는 이름과 같다."""
    return {
        "AZURE_SEARCH_ENDPOINT": settings.azure_search_endpoint,
        "AZURE_SEARCH_API_KEY": settings.azure_search_api_key,
        "AZURE_SEARCH_INDEX": settings.azure_search_index,
        "AZURE_SEARCH_VECTOR_DIMENSIONS": settings.azure_search_vector_dimensions,
        "V3_EMBEDDINGS": settings.v3_embeddings,
        "OLLAMA_BASE_URL": settings.ollama_base_url,
        "OLLAMA_EMBED_MODEL": settings.ollama_embed_model,
        "FOUNDRY_ENDPOINT": settings.foundry_endpoint,
        "FOUNDRY_API_KEY": settings.foundry_api_key,
        "FOUNDRY_EMBEDDING_DEPLOYMENT": settings.foundry_embedding_deployment,
    }


def current_auth() -> httpio.Auth:
    """설정 V3_AUTH, V3_ORG_TOKENS(또는 V3_ORG_TOKENS_FILE), V3_BFF_KEY로 요청한 조직을 정하는 방법을 만든다.

    설정이 틀리면 ValueError. header 모드에서는 토큰 설정을 읽지 않는다(파일도 열지 않는다).
    """
    mode = (settings.v3_auth or "").strip().lower()
    if mode == httpio.AUTH_BFF:
        return httpio.make_auth(mode, bff_key=settings.v3_bff_key)
    if mode != httpio.AUTH_TOKEN:
        return httpio.make_auth(settings.v3_auth)
    return httpio.make_auth(
        settings.v3_auth,
        httpio.tokens_text(settings.v3_org_tokens, settings.v3_org_tokens_file),
    )


_state: service.App | None = None
_state_lock = threading.Lock()
_memory_lock = threading.RLock()
# 이 요청이 체험하기(시연 조직 읽기 전용)인가. 요청마다 viewer_of가 정한다(요청은 저마다 다른 문맥에서 돈다)
_demo_view: contextvars.ContextVar[bool] = contextvars.ContextVar("v3_demo_view", default=False)


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


def _invoke(fn: Callable[..., Any], args: tuple[Any, ...], demo: bool = False) -> Any:
    app = get_state()
    if settings.database_url:
        return fn(demo_app(app) if demo else app, *args)
    with _memory_lock:  # 메모리 저장소는 동시에 고치면 깨진다
        return fn(demo_app(app) if demo else app, *args)


async def call(fn: Callable[..., Any], *args: Any) -> Any:
    """service 함수를 fn(app, *args)로 작업 스레드에서 부른다.

    체험하기 요청이면 app 대신 시연 조직만 보이는 읽기 전용 앱(demo/scope.py)을 준다.
    """
    return await run_in_threadpool(_invoke, fn, args, _demo_view.get())


# ── 뒤에서 도는 실행 ─────────────────────────────────────────────────
_runs_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}


def _background_app(app: service.App) -> service.App:
    """뒤에서 도는 실행이 쓸 앱. SQL이면 그대로(호출 하나가 트랜잭션 하나), 메모리면 호출마다 잠근다."""
    return app if settings.database_url else app.with_lock(_memory_lock)


def _background_run(app: service.App, session_id: str, run_id: str) -> None:
    try:
        service.execute_run(app, session_id, run_id)
    except Exception:
        # 실행 기록(kind "error")과 진행 기록에 이미 남겼다. 서버 기록에는 자세한 내용을 남긴다
        logger.exception("뒤에서 돈 진단 실행이 실패했다: %s %s", session_id, run_id)
    finally:
        with _runs_lock:
            _running.pop(run_id, None)


def launch_run(app: service.App, session_id: str, run_id: str) -> None:
    """start_run이 차지한 자리에서 진단을 뒤에서 돌린다. 스레드를 띄우지 못하면 자리를 비운다."""
    worker = threading.Thread(
        target=_background_run,
        args=(_background_app(app), session_id, run_id),
        name=f"v3-run-{session_id}",
        daemon=True,  # 프로세스가 끝나면 함께 멈춘다. 남은 자리는 heartbeat가 끊겨 멈춘 것으로 본다
    )
    with _runs_lock:
        _running[run_id] = worker
    try:
        worker.start()
    except BaseException:
        with _runs_lock:
            _running.pop(run_id, None)
        app.store.release_run(session_id, run_id)
        raise


def wait_runs(timeout: float = 30.0) -> bool:
    """뒤에서 도는 실행이 모두 끝날 때까지 기다린다(테스트와 종료 때). 모두 끝났으면 True."""
    deadline = time.monotonic() + timeout
    while True:
        with _runs_lock:
            workers = list(_running.values())
        if not workers:
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        workers[0].join(min(left, 0.5))


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
async def viewer_of(request: Request, *, required: bool = True) -> str:
    """요청한 조직. header 모드는 X-Org-Id, token 모드는 Authorization: Bearer 토큰으로 정한다.

    bff 모드는 BFF 키를 확인하고, X-User-Id가 X-Org-Id의 구성원인지 계정 저장소로 확인한다.
    X-Demo-View: 1이면 체험하기다: GET만 받고(아니면 403 DEMO_READ_ONLY), X-Org-Id는 시연 조직이어야
    하며(아니면 403 NOT_DEMO_ORG), 이 요청의 서비스 함수는 읽기 전용 시연 앱으로 돈다(call).
    """
    auth = current_auth()
    _demo_view.set(False)
    if auth.mode != httpio.AUTH_BFF:
        return auth.viewer(request.headers, required=required)
    who = auth.bff(request.headers)
    if who.demo:
        if request.method != "GET":
            raise service.ServiceError(
                "DEMO_READ_ONLY", "체험하기에서는 보기만 할 수 있습니다.", 403
            )
        if not who.org_id:
            raise service.ServiceError("ORG_REQUIRED", "X-Org-Id로 시연 조직을 밝혀야 합니다.", 401)
        if not await call(accounts.is_demo_org, who.org_id):
            raise service.ServiceError(
                "NOT_DEMO_ORG", "체험하기로는 시연 조직만 볼 수 있습니다.", 403
            )
        _demo_view.set(True)
        return who.org_id
    if not who.org_id:
        if required:
            raise service.ServiceError("ORG_REQUIRED", "X-Org-Id로 조직을 밝혀야 합니다.", 401)
        if who.user_id:
            await call(accounts.check_user, who.user_id)
        return ""
    await call(accounts.check_member, who.user_id, who.org_id)
    return who.org_id


def bff_key_only(request: Request) -> httpio.BffIdentity | None:
    """bff 모드면 BFF 키만 확인하고 BFF가 밝힌 값을 준다. 다른 모드에서는 아무것도 하지 않는다(None).

    이 함수를 쓰는 경로(계정·조직·초대, CSV 양식)는 체험하기 앱으로 돌지 않는다.
    """
    _demo_view.set(False)
    auth = current_auth()
    return auth.bff(request.headers) if auth.mode == httpio.AUTH_BFF else None


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


# 공개 신뢰 페이지(평가 설계 17장)가 읽는 최신 평가 요약. 평가 실행(scripts/v3/eval/run_all)이 만든다
TRUST_FILE = Path(__file__).resolve().parents[2] / "docs" / "v3" / "eval" / "latest.json"


@router.get("/trust")
@guarded
async def trust(request: Request):
    """최신 평가 요약을 인증 없이 돌려준다. 조직 데이터는 들어 있지 않다."""
    try:
        return json.loads(TRUST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise service.ServiceError(
            "TRUST_UNAVAILABLE", "평가 결과를 불러오지 못했습니다.", 503
        ) from error


@router.post("/demo/reset")
@guarded
async def demo_reset(request: Request):
    # 관리자만(설정 V3_DEMO_OPEN이 참이면 누구나). 조직을 밝히지 않았으면 서비스가 401로 답한다
    viewer = await viewer_of(request, required=False) or None
    return await call(service.reset_demo, viewer)


@router.get("/orgs")
@guarded
async def orgs(request: Request):
    # header 모드(시연 전용)는 누구에게나 모든 조직을 준다. token·bff 모드는 자기 조직과 대화 상대만 준다
    auth = current_auth()
    viewer = await viewer_of(request, required=auth.scoped) or None
    return await call(service.list_orgs, viewer, auth.scoped)


@router.get("/orgs/{org_id}/streams")
@guarded
async def streams(org_id: str, request: Request):
    return await call(service.list_streams, org_id, await viewer_of(request))


@router.get("/orgs/{org_id}/report")
@guarded
async def org_report(org_id: str, request: Request):
    params = request.query_params
    return await call(
        service.get_report,
        org_id,
        await viewer_of(request),
        params.get("from") or None,
        params.get("to") or None,
    )


@router.put("/orgs/{org_id}/streams/{stream_id}")
@guarded
async def put_stream(org_id: str, stream_id: str, request: Request):
    viewer = await viewer_of(request)
    patch = await json_object(request)
    return await call(service.update_stream, org_id, stream_id, patch, viewer)


@router.get("/vendors/{org_id}")
@guarded
async def vendor(org_id: str, request: Request):
    return await call(service.get_vendor, org_id, await viewer_of(request))


@router.put("/vendors/{org_id}")
@guarded
async def put_vendor(org_id: str, request: Request):
    viewer = await viewer_of(request)
    patch = await json_object(request)
    return await call(service.update_vendor, org_id, patch, viewer)


# ── 진단 ─────────────────────────────────────────────────────────────
@router.post("/sessions")
@guarded
async def create_session(request: Request):
    viewer = await viewer_of(request)
    body = await json_object(request)
    return await call(service.create_session, str(body.get("emitterOrgId") or viewer), viewer)


@router.get("/sessions")
@guarded
async def session_list(request: Request):
    return await call(service.list_sessions, await viewer_of(request))


@router.get("/sessions/{session_id}")
@guarded
async def session_view(session_id: str, request: Request):
    return await call(service.get_view, session_id, await viewer_of(request))


@router.get("/sessions/{session_id}/quote-request")
@guarded
async def quote_request(session_id: str, request: Request):
    params = request.query_params
    return await call(
        service.quote_request,
        session_id,
        await viewer_of(request),
        params.get("streamId", ""),
        params.get("vendorId", ""),
    )


@router.post("/sessions/{session_id}/run")
@guarded
async def run_session(session_id: str, request: Request):
    # mode=async: 실행 자리만 차지하고 202 {sessionId, status, runId, after}. 진행은 /stream으로 본다
    viewer = await viewer_of(request)
    mode = request.query_params.get("mode", "")
    if mode == "async":
        started = await call(service.start_run, session_id, viewer)
        launch_run(get_state(), session_id, started["runId"])
        return JSONResponse(started, status_code=202)
    if mode not in ("", "sync"):
        raise ApiError("INVALID_VALUE", "mode는 async나 sync여야 합니다.", 422)
    return await call(service.run_session, session_id, viewer)


def _resume_point(request: Request) -> int:
    """이어 받을 지점: ?after=와 Last-Event-ID 헤더 가운데 큰 값(없으면 0, 처음부터)."""
    try:
        return live.resume_point(
            request.query_params.get("after"), request.headers.get("last-event-id")
        )
    except ValueError:
        raise ApiError("INVALID_VALUE", "after는 0 이상의 정수여야 합니다.", 422) from None


async def _stream_body(
    request: Request, session_id: str, viewer: str, demo: bool, first: dict[str, Any]
) -> AsyncIterator[str]:
    """live.stream_chunks와 같은 순서를 비동기로 한다(이벤트 루프를 막지 않는다)."""
    yield f"retry: {live.STREAM_RETRY_MS}\n\n"  # 첫 조각을 곧바로 보내 프록시가 붙들지 않게 한다
    batch, last = first, int(first["lastSeq"])
    seen = live.HUB.version(session_id)
    clock = time.monotonic
    read_at = sent_at = checked_at = clock()
    while True:
        for item in batch["events"]:
            yield live.sse(item["event"], item["data"], item["id"])
            sent_at = clock()
        last = max(last, int(batch["lastSeq"]))
        if not batch["running"]:
            yield live.sse("done", {"status": batch["status"], "lastSeq": last})
            return
        while True:  # 새 기록을 기다린다
            await asyncio.sleep(live.STREAM_POLL_SECONDS)
            now = clock()
            if now - sent_at >= live.STREAM_KEEPALIVE_SECONDS:
                yield ": keep-alive\n\n"
                sent_at = now
            if now - checked_at >= live.STREAM_READ_SECONDS:
                checked_at = now
                if await request.is_disconnected():
                    return
            version = live.HUB.version(session_id)
            if version != seen or now - read_at >= live.STREAM_READ_SECONDS:
                seen, read_at = version, now
                break
        try:
            batch = await run_in_threadpool(
                _invoke, service.stream_events, (session_id, viewer, last), demo
            )
        except service.ServiceError as error:  # 도중에 볼 수 없게 됐다(시연 되돌리기로 지워짐 등)
            yield live.sse("error", {"code": error.code, "message": error.message})
            return
        except Exception:  # 저장소가 잠시 닿지 않는 등. 브라우저는 Last-Event-ID로 다시 붙는다
            logger.exception("진행 기록을 읽지 못했다: %s", session_id)
            yield live.sse("error", {"code": "INTERNAL", "message": "서버에서 오류가 났습니다."})
            return


@router.get("/sessions/{session_id}/stream")
@guarded
async def session_stream(session_id: str, request: Request):
    viewer = await viewer_of(request)
    after = _resume_point(request)
    demo = _demo_view.get()
    # 첫 읽기에서 권한을 확인한다. 볼 수 없으면 SSE가 아니라 보통의 오류 응답이 나간다
    first = await call(service.stream_events, session_id, viewer, after)
    return StreamingResponse(
        _stream_body(request, session_id, viewer, demo, first),
        media_type="text/event-stream",
        headers=live.STREAM_HEADERS,
    )


@router.post("/sessions/{session_id}/answers")
@guarded
async def answer(session_id: str, request: Request):
    viewer = await viewer_of(request)
    body = await json_object(request)
    question_id, value = httpio.answer_body(body)
    return await call(service.answer_question, session_id, question_id, value, viewer)


@router.post("/sessions/{session_id}/approvals")
@guarded
async def approve(session_id: str, request: Request):
    viewer = await viewer_of(request)
    body = await json_object(request)
    return await call(service.approve, session_id, str(required(body, "threadId")), viewer)


# ── 재학습 데이터 수집 (7단계) ───────────────────────────────────────
@router.post("/sessions/{session_id}/corrections")
@guarded
async def record_correction(session_id: str, request: Request):
    viewer = await viewer_of(request)
    body = await json_object(request)
    return await call(service.record_correction, session_id, body, viewer)


@router.post("/sessions/{session_id}/outcomes")
@guarded
async def record_outcome(session_id: str, request: Request):
    viewer = await viewer_of(request)
    body = await json_object(request)
    return await call(service.record_outcome, session_id, body, viewer)


# ── 근거 ─────────────────────────────────────────────────────────────
@router.get("/evidence")
@guarded
async def search_evidence(request: Request):
    viewer = await viewer_of(request)
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
    return await call(service.get_evidence, evidence_id, await viewer_of(request))


# ── 자료 올리기 ──────────────────────────────────────────────────────
@router.get("/intake/csv/template")
@guarded
async def csv_template(request: Request):
    bff_key_only(request)
    data = await call(service.csv_template)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{CSV_TEMPLATE_NAME}"'},
    )


@router.post("/intake/csv")
@guarded
async def upload_csv(request: Request):
    viewer = await viewer_of(request)
    fields, filename, data = await read_upload(request, "측정값.csv")
    org_id = fields.get("orgId") or viewer
    return await call(service.upload_csv, org_id, filename, data, viewer)


@router.post("/intake/documents")
@guarded
async def upload_document(request: Request):
    viewer = await viewer_of(request)
    fields, filename, data = await read_upload(request, "문서")
    org_id = fields.get("orgId") or viewer
    kind = fields.get("kind") or ""  # 종류가 없거나 틀리면 서비스가 INVALID_KIND로 답한다
    return await call(service.upload_document, org_id, kind, filename, data, viewer)


@router.post("/intake/media")
@guarded
async def upload_media(request: Request):
    viewer = await viewer_of(request)
    fields, filename, data = await read_upload(request, "사진")
    org_id = fields.get("orgId") or viewer
    stream_id = fields.get("streamId") or ""
    return await call(service.upload_media, org_id, stream_id, filename, data, viewer)


@router.post("/intake/documents/confirm")
@guarded
async def confirm_document(request: Request):
    viewer = await viewer_of(request)
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
    return await call(service.admin_status, await viewer_of(request))


@router.post("/admin/eval")
@guarded
async def admin_eval(request: Request):
    return await call(service.run_eval, await viewer_of(request))


@router.get("/admin/retrain/export")
@guarded
async def admin_retrain_export(request: Request):
    return await call(service.export_retraining, await viewer_of(request))


@router.post("/admin/retrain/runs")
@guarded
async def admin_retrain_runs(request: Request):
    viewer = await viewer_of(request)
    body = await json_object(request)
    return await call(service.register_model_run, body, viewer)
