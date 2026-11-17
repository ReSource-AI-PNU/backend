"""실행 기록(store events)을 OpenTelemetry span으로 내보낸다.

OpenTelemetry 부분은 2026-11-18 tracing extra를 깐 환경에서 실제 연결 문자열로 span 하나를 내보내
Application Insights의 dependencies 표에서 확인했다. 이벤트를 span 이름과 속성으로 바꾸는 부분
(event_span)과 TracingStore는 표준 라이브러리만 쓰고 tests/v3/test_tracing.py가 로컬에서 확인한다.

- 화면의 실행 기록과 같은 이벤트를 그대로 옮긴다. 일어나지 않은 호출을 만들지 않는다.
- 두 패키지가 없거나 연결 문자열이 없으면 아무 일도 하지 않는다(받은 저장소를 그대로 돌려준다).
- 내보내기가 실패해도 진단은 계속된다. 실행 기록은 이미 저장소에 남아 있다.
- 필요한 패키지: azure-monitor-opentelemetry (pyproject의 tracing extra. opentelemetry-api, sdk를
  함께 가져온다).
  configure_azure_monitor(connection_string=...)는 공식 문서의 사용법 그대로다.
  https://learn.microsoft.com/python/api/overview/azure/monitor-opentelemetry-readme
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

TRACER_NAME = "resource-ai.v3"
MAX_TEXT = 500  # 속성 하나에 싣는 글자 수 상한
MAX_ITEMS = 50  # 목록 속성의 항목 수 상한
_SCALARS = (str, bool, int, float)

Emit = Callable[[str, dict[str, Any]], None]

_configured = False
_configure_lock = threading.Lock()


def _clip(value: str) -> str:
    return value if len(value) <= MAX_TEXT else value[: MAX_TEXT - 1] + "…"


def _attribute(value: Any) -> Any:
    """OpenTelemetry 속성으로 쓸 수 있는 값(글, 참거짓, 숫자, 글 목록)만 돌려준다. 아니면 None."""
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return [_clip(item) for item in value[:MAX_ITEMS]]
    return None


def event_span(session_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """실행 기록 한 건을 {"name", "attributes"}로 바꾼다.

    속성 이름은 rsai.* 로 둔다. detail의 값은 한 단계만 편다: 글·숫자·참거짓·글 목록은 그대로,
    한 단계 안쪽 객체(예: model.provider)는 점으로 이은 이름으로 싣고, 더 깊은 값은 싣지 않는다
    (도구 입력과 검사 목록 전체를 추적에 복사하지 않는다. 원본은 실행 기록 테이블에 있다).
    """
    kind = str(event.get("kind") or "event")
    attributes: dict[str, Any] = {
        "rsai.session.id": str(session_id),
        "rsai.event.kind": kind,
        "rsai.event.actor": str(event.get("actor") or ""),
        "rsai.event.summary": _clip(str(event.get("summary") or "")),
    }
    if isinstance(event.get("seq"), int):
        attributes["rsai.event.seq"] = event["seq"]
    if event.get("at"):
        attributes["rsai.event.at"] = str(event["at"])
    if event.get("threadId"):
        attributes["rsai.thread.id"] = str(event["threadId"])
    detail = event.get("detail")
    for key, value in sorted(detail.items()) if isinstance(detail, dict) else ():
        flat = _attribute(value)
        if flat is not None:
            attributes[f"rsai.detail.{key}"] = flat
        elif isinstance(value, dict):
            for inner_key, inner in sorted(value.items()):
                inner_flat = _attribute(inner)
                if inner_flat is not None:
                    attributes[f"rsai.detail.{key}.{inner_key}"] = inner_flat
        elif isinstance(value, (list, tuple)):
            attributes[f"rsai.detail.{key}.count"] = len(value)
    name = f"rsai.{kind}"
    tool = detail.get("tool") if isinstance(detail, dict) else None
    if kind == "tool_call" and isinstance(tool, str) and tool:
        name = f"rsai.tool_call {tool}"
    message_type = detail.get("type") if isinstance(detail, dict) else None
    if kind == "message" and isinstance(message_type, str) and message_type:
        name = f"rsai.message {message_type}"
    return {"name": name, "attributes": attributes}


class TracingStore:
    """Store를 그대로 위임하고 add_event만 감싸는 얇은 래퍼.

    emit(session_id, event)는 저장이 끝난 뒤에 불린다. emit이 실패해도 저장된 이벤트를 돌려준다.
    """

    def __init__(self, inner: Any, emit: Emit) -> None:
        self._inner = inner
        self._emit = emit

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def add_event(
        self,
        session_id: str,
        actor: str,
        kind: str,
        summary: str,
        *,
        thread_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = self._inner.add_event(
            session_id, actor, kind, summary, thread_id=thread_id, detail=detail
        )
        try:
            self._emit(session_id, event)
        except Exception as error:  # noqa: BLE001 - 추적 실패가 진단을 막지 않는다
            logger.warning("실행 기록을 추적으로 내보내지 못했다: %s", type(error).__name__)
        return event


def otel_emitter(tracer: Any = None) -> Emit:
    """이벤트마다 span 하나를 만드는 emit. 로컬 미실행."""
    if tracer is None:
        from opentelemetry import trace

        tracer = trace.get_tracer(TRACER_NAME)

    def emit(session_id: str, event: dict[str, Any]) -> None:
        span = event_span(session_id, event)
        with tracer.start_as_current_span(span["name"]) as current:
            for key, value in span["attributes"].items():
                current.set_attribute(key, value)

    return emit


def available() -> bool:
    """두 패키지를 불러올 수 있는지."""
    try:
        import azure.monitor.opentelemetry  # noqa: F401
        import opentelemetry.trace  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def enable(store: Any, connection_string: str | None) -> Any:
    """연결 문자열과 두 패키지가 있으면 추적을 켜고 TracingStore를 돌려준다. 아니면 store 그대로.

    로컬 미실행(패키지가 있을 때의 경로). configure_azure_monitor는 프로세스에서 한 번만 부른다.
    """
    global _configured
    if not connection_string:
        return store
    if not available():
        logger.warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING이 있지만 azure-monitor-opentelemetry가 없어 "
            "추적을 켜지 않는다"
        )
        return store
    from azure.monitor.opentelemetry import configure_azure_monitor

    with _configure_lock:
        if not _configured:
            configure_azure_monitor(connection_string=connection_string)
            _configured = True
    return TracingStore(store, otel_emitter())
