"""승인 막힘은 바뀐 근거·흐름을 인용한 모든 합의 대화에, 승인을 시도하기 전에 걸린다. 수치는 가상이다."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.v3 import service
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    make_app,
    thread_of,
    to_approval,
)

HEADER = "흐름 이름,공정,재질,오염도(%),수분(%),측정일,출처,형태,로트,월 발생량"
# 시연 데이터와 같은 측정일(2026-11-10)로 값만 바꾼 행
RESAMPLED = "스프루·런너,사출,PP,1.6,0.4,2026-11-10,PIR,가공 전,L-001,600"
SPRUE_MEASURE = "measure:org_hanbit:st_sprue:오염도@2026-11-10"


def approvals(view):
    return {a["threadId"]: a for a in view["approvals"]}


def stored(app, session_id):
    return {a["threadId"]: a for a in app.store.list_approvals(session_id)}


def citing(view, evidence_id):
    """그 근거를 메시지에 인용한 대화의 ID."""
    return {
        t["id"]
        for t in view["threads"]
        if any(evidence_id in c["evidence"] for m in t["messages"] for c in m["claims"])
    }


def test_csv_reupload_blocks_every_agreed_thread_that_cites_the_measurement():
    app = make_app()
    view = to_approval(app)
    session_id = view["session"]["id"]
    agreed = {t["id"]: t for t in view["threads"] if t["status"] == "agreed"}
    cited = citing(view, SPRUE_MEASURE) & set(agreed)
    assert len(cited) == 3  # 세 업체 모두 문의에서 스프루의 측정값을 받았다
    assigned = thread_of(view, SAEGIL)["id"]  # 스프루가 실제로 배정된 대화
    service.approve(app, session_id, assigned, HANBIT)
    service.approve(app, session_id, thread_of(view, MISO)["id"], MISO)

    service.upload_csv(app, HANBIT, "재측정.csv", f"{HEADER}\n{RESAMPLED}\n".encode(), HANBIT)

    # 올린 때에 이미 막혀 있다(뷰를 읽거나 승인을 누르기 전에)
    now = stored(app, session_id)
    assert all(now[t]["blocked"] for t in cited)
    assert all(
        not now[t]["emitter"] and not now[t]["vendor"] for t in cited
    )  # 앞선 승인도 지워진다
    for viewer in (HANBIT, SAEGIL, NAMHAE, MISO):
        shown = approvals(service.get_view(app, session_id, viewer))
        assert shown and all(a["blocked"] for a in shown.values()), viewer
        for thread_id, item in shown.items():
            assert "EVIDENCE_CHANGED" in item["blockedCodes"]
            assert ("STREAM_CHANGED" in item["blockedCodes"]) == (thread_id == assigned)
            assert all(code in item["blockedReason"] for code in item["blockedCodes"])
    for thread_id in cited:
        for org in (HANBIT, agreed[thread_id]["vendorOrgId"]):
            with pytest.raises(service.ServiceError) as caught:
                service.approve(app, session_id, thread_id, org)
            assert caught.value.code == "APPROVAL_BLOCKED" and caught.value.status == 409
    view = service.get_view(app, session_id, HANBIT)
    assert view["session"]["status"] == "awaiting_approval" and view["documents"] == []
    assert all(t["status"] != "confirmed" for t in view["threads"])


def test_a_change_made_behind_the_service_is_caught_when_the_view_is_built():
    app = make_app()
    view = to_approval(app)
    session_id = view["session"]["id"]
    assert not any(a["blocked"] for a in view["approvals"])
    found = app.registry.get(SPRUE_MEASURE, None)
    app.registry.put(replace(found, value=found.value + 0.5))  # 서비스 함수를 거치지 않은 변경
    assert not any(a["blocked"] for a in stored(app, session_id).values())
    shown = approvals(service.get_view(app, session_id, SAEGIL))  # 업체가 먼저 봐도 같다
    assert shown and all(a["blocked"] for a in shown.values())
    cited = citing(service.get_view(app, session_id, HANBIT), SPRUE_MEASURE)
    assert {t for t, a in stored(app, session_id).items() if a["blocked"]} == cited


def test_only_the_threads_that_use_the_changed_stream_are_blocked():
    app = make_app()
    view = to_approval(app)
    session_id = view["session"]["id"]
    film_thread = thread_of(view, SAEGIL)["id"]
    original = app.store.get_stream("st_film")
    service.update_stream(app, HANBIT, "st_film", {"monthlyKg": 310}, HANBIT)
    shown = approvals(service.get_view(app, session_id, HANBIT))
    assert shown[film_thread]["blocked"] and shown[film_thread]["blockedCodes"] == [
        "STREAM_CHANGED"
    ]
    others = [a for t, a in shown.items() if t != film_thread]
    assert others and all(not a["blocked"] and a["blockedCodes"] == [] for a in others)
    # 흐름 자료가 합의한 때와 같아지면 풀린다
    app.store.put_stream(original)
    shown = approvals(service.get_view(app, session_id, HANBIT))
    assert not shown[film_thread]["blocked"] and shown[film_thread]["blockedReason"] is None
    assert shown[film_thread]["blockedCodes"] == []


def test_rechecking_writes_nothing_when_nothing_changed():
    app = make_app()
    view = to_approval(app)
    session_id = view["session"]["id"]
    service.upload_csv(app, HANBIT, "재측정.csv", f"{HEADER}\n{RESAMPLED}\n".encode(), HANBIT)
    first = service.get_view(app, session_id, HANBIT)
    blocks = [e for e in first["events"] if e["summary"] == "근거 확인 실패로 승인을 막았습니다."]
    assert len(blocks) == 3 and all(e["detail"]["codes"] for e in blocks)
    version = app.store.get_session(session_id)["version"]
    for viewer in (HANBIT, SAEGIL, MISO, HANBIT):
        again = service.get_view(app, session_id, viewer)
    assert again["events"] == first["events"] and again["approvals"] == first["approvals"]
    assert app.store.get_session(session_id)["version"] == version


def test_views_before_agreement_and_after_confirmation_are_not_rechecked():
    app = make_app()
    view = service.create_session(app, HANBIT, HANBIT)
    assert view["approvals"] == []
    view = to_approval(app)
    assert all(a["blockedCodes"] == [] for a in view["approvals"])
