"""실행 기록을 추적으로 옮기는 부분: 이벤트 → span 이름·속성, TracingStore, 꺼져 있을 때의 동작."""

import pytest

from app.v3 import service, tracing
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore
from tests.v3 import wiring
from tests.v3.runtime_fixtures import (
    CLOCK,
    HANBIT,
    MISO,
    MISO_VISITS,
    TODAY,
    approve_all,
    to_approval,
)

TOOL_EVENT = {
    "seq": 2,
    "at": CLOCK,
    "actor": "tool",
    "kind": "tool_call",
    "threadId": None,
    "summary": "identify_stream · needs_information",
    "detail": {
        "tool": "identify_stream",
        "stage": 1,
        "arguments": {"streamId": "st_defect"},
        "status": "needs_information",
        "checks": [{"key": "moisture", "status": "conditional"}],
        "evidence": ["calc:identify_stream:abc:grade"],
        "orgId": "org_hanbit",
        "calledBy": "orchestrator",
    },
}


def test_tool_call_event_becomes_span_attributes():
    span = tracing.event_span("ses_1", TOOL_EVENT)
    assert span["name"] == "rsai.tool_call identify_stream"
    assert span["attributes"] == {
        "rsai.session.id": "ses_1",
        "rsai.event.kind": "tool_call",
        "rsai.event.actor": "tool",
        "rsai.event.summary": "identify_stream · needs_information",
        "rsai.event.seq": 2,
        "rsai.event.at": CLOCK,
        "rsai.detail.tool": "identify_stream",
        "rsai.detail.stage": 1,
        "rsai.detail.status": "needs_information",
        "rsai.detail.orgId": "org_hanbit",
        "rsai.detail.calledBy": "orchestrator",
        "rsai.detail.arguments.streamId": "st_defect",
        "rsai.detail.checks.count": 1,
        "rsai.detail.evidence": ["calc:identify_stream:abc:grade"],
    }


def test_message_and_validation_events():
    message = {
        "seq": 50,
        "actor": "company_agent",
        "kind": "message",
        "threadId": "th_1",
        "summary": "inquiry · (주)한빛정밀",
        "detail": {
            "type": "inquiry",
            "model": {"provider": "rule", "name": "규칙 정책(모델 미연결)"},
            "attempts": 1,
            "reason": None,
        },
    }
    span = tracing.event_span("ses_1", message)
    assert span["name"] == "rsai.message inquiry"
    assert span["attributes"]["rsai.thread.id"] == "th_1"
    assert span["attributes"]["rsai.detail.model.provider"] == "rule"  # 실제로 쓴 구현 그대로
    assert "rsai.detail.reason" not in span["attributes"]  # 없는 값은 싣지 않는다
    validation = {"kind": "validation", "actor": "validator", "detail": {"ok": False}}
    span = tracing.event_span("ses_1", validation)
    assert span["name"] == "rsai.validation" and span["attributes"]["rsai.detail.ok"] is False


def test_attribute_values_are_bounded_and_typed():
    long_text = "가" * 2000
    event = {
        "kind": "document",
        "summary": long_text,
        "detail": {
            "text": long_text,
            "ids": [f"id_{i}" for i in range(200)],
            "mixed": ["a", 1],
            "deep": {"inner": {"more": 1}, "ok": True},
            "none": None,
        },
    }
    attributes = tracing.event_span("ses_1", event)["attributes"]
    assert len(attributes["rsai.event.summary"]) == tracing.MAX_TEXT
    assert len(attributes["rsai.detail.text"]) == tracing.MAX_TEXT
    assert len(attributes["rsai.detail.ids"]) == tracing.MAX_ITEMS
    assert attributes["rsai.detail.mixed.count"] == 2  # 섞인 목록은 개수만
    assert attributes["rsai.detail.deep.ok"] is True
    assert not [key for key in attributes if key.startswith("rsai.detail.deep.inner")]
    for value in attributes.values():
        assert isinstance(value, (str, bool, int, float)) or all(isinstance(v, str) for v in value)
    assert tracing.event_span("ses_1", {})["name"] == "rsai.event"


def test_tracing_store_delegates_and_emits_every_event():
    emitted = []
    inner = InMemoryStore(lambda: CLOCK)
    store = tracing.TracingStore(
        inner, lambda session_id, event: emitted.append((session_id, event))
    )
    app = service.App(store, InMemoryRegistry(), today=TODAY)
    service.seed(app)
    # 미소환경의 노선 밖 수거 답(가상). 없으면 운반비 미정이라 그 대화는 확정할 수 없다(계약 검토 담당)
    service.update_vendor(app, MISO, {"transport": {"offRoute": dict(MISO_VISITS)}}, MISO)
    view = approve_all(app, to_approval(app, HANBIT))
    assert view["session"]["status"] == "confirmed"
    events = inner.list_events(view["session"]["id"])
    assert events and [event for _, event in emitted] == events  # 화면의 실행 기록과 같은 것
    assert {session_id for session_id, _ in emitted} == {view["session"]["id"]}
    kinds = {
        tracing.event_span(sid, event)["attributes"]["rsai.event.kind"] for sid, event in emitted
    }
    assert {"message", "tool_call", "validation", "approval", "document"} <= kinds
    assert (
        store.inner is inner and service.admin_status(app, "org_admin")["store"] == "TracingStore"
    )


def test_emit_failure_does_not_stop_the_run():
    def broken(session_id, event):
        raise RuntimeError("내보내기 실패")

    inner = InMemoryStore(lambda: CLOCK)
    store = tracing.TracingStore(inner, broken)
    event = store.add_event("ses_1", "system", "stage", "진단을 만들었습니다.", detail={"a": 1})
    assert event["seq"] == 1 and inner.list_events("ses_1") == [event]


def test_tracing_store_add_event_has_the_protocol_signature():
    protocol = wiring.class_methods("app.v3.store", "Store")["add_event"]
    wrapped = wiring.class_methods("app.v3.tracing", "TracingStore")["add_event"]
    assert wiring.shape_of(wrapped) == wiring.shape_of(protocol)


def test_enable_does_nothing_without_a_connection_string_or_packages():
    store = InMemoryStore()
    assert tracing.enable(store, "") is store and tracing.enable(store, None) is store
    if not tracing.available():
        assert tracing.enable(store, "InstrumentationKey=test") is store


def test_otel_emitter_sets_attributes_on_a_span():
    """가짜 tracer로 emit의 모양만 본다(실제 OpenTelemetry 호출은 CI에서)."""

    class Span:
        def __init__(self, name):
            self.name, self.attributes = name, {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def set_attribute(self, key, value):
            self.attributes[key] = value

    class Tracer:
        def __init__(self):
            self.spans = []

        def start_as_current_span(self, name):
            self.spans.append(Span(name))
            return self.spans[-1]

    tracer = Tracer()
    tracing.otel_emitter(tracer)("ses_1", TOOL_EVENT)
    assert tracer.spans[0].name == "rsai.tool_call identify_stream"
    assert tracer.spans[0].attributes == tracing.event_span("ses_1", TOOL_EVENT)["attributes"]


def test_real_opentelemetry_accepts_the_attributes():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.otel_emitter(provider.get_tracer(tracing.TRACER_NAME))("ses_1", TOOL_EVENT)
    (span,) = exporter.get_finished_spans()
    assert span.name == "rsai.tool_call identify_stream"
    assert span.attributes["rsai.detail.tool"] == "identify_stream"
    assert list(span.attributes["rsai.detail.evidence"]) == ["calc:identify_stream:abc:grade"]
