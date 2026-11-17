"""진단 뷰 어디에도 보는 조직이 열 수 없는 근거 ID가 실려 나가지 않는다(실행 기록 포함)."""

import json
import re

import pytest

from app.v3 import service
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from tests.v3.runtime_fixtures import (
    DAON,
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    approve_all,
    make_app,
    to_approval,
)

VENDORS = (SAEGIL, NAMHAE, MISO)


def closed_ids(app, viewer):
    """레지스트리에 있지만 viewer가 열 수 없는 근거 ID."""
    closed = []
    for evidence in app.registry.list(org_id=None):
        try:
            app.registry.get(evidence.id, viewer)
        except (EvidenceForbidden, EvidenceNotFound):
            closed.append(evidence.id)
    return closed


def leaks(app, view, viewer):
    """뷰 JSON 전체에서 viewer가 열 수 없는 근거 ID를 찾는다(다른 ID의 앞부분인 경우는 뺀다)."""
    text = json.dumps(view, ensure_ascii=False)
    found = []
    for evidence_id in closed_ids(app, viewer):
        if evidence_id in text and re.search(re.escape(evidence_id) + r'(?![^"\s,\]}])', text):
            found.append(evidence_id)
    return found


def views_of(app, session_id, parties):
    out = {}
    for org in parties:
        try:
            out[org] = service.get_view(app, session_id, org)
        except service.ServiceError as error:
            assert error.status == 404  # 열린 대화가 없는 업체에게는 진단이 없다
    return out


@pytest.fixture(scope="module")
def world():
    """두 배출 기업이 같은 업체들과 진단을 돌린다(한빛은 확정까지, 다온은 승인 대기까지)."""
    app = make_app()
    hanbit = to_approval(app, HANBIT)
    daon = to_approval(app, DAON)
    waiting = views_of(app, hanbit["session"]["id"], (HANBIT, *VENDORS))
    approve_all(app, hanbit)
    return app, hanbit["session"]["id"], daon["session"]["id"], waiting


def test_closed_ids_exist_so_the_check_means_something(world):
    app, *_ = world
    assert len(closed_ids(app, HANBIT)) > 20 and len(closed_ids(app, SAEGIL)) > 20
    # 도우미가 실제로 찾아내는지: 닫힌 ID를 일부러 넣어 본다
    closed = closed_ids(app, HANBIT)[0]
    assert leaks(app, {"events": [{"detail": {"evidence": [closed]}}]}, HANBIT) == [closed]
    assert leaks(app, {"note": closed + "-뒤에붙은글"}, HANBIT) == []


def test_no_view_carries_an_evidence_id_the_viewer_cannot_open(world):
    app, hanbit_id, daon_id, waiting = world
    checked = 0
    for viewer, view in waiting.items():  # 승인 전
        assert leaks(app, view, viewer) == [], viewer
        checked += 1
    for session_id, emitter in ((hanbit_id, HANBIT), (daon_id, DAON)):  # 확정 뒤, 다른 배출 기업
        for viewer, view in views_of(app, session_id, (emitter, *VENDORS)).items():
            assert leaks(app, view, viewer) == [], (session_id, viewer)
            checked += 1
    assert checked >= 8


def test_events_keep_only_a_count_of_what_was_hidden(world):
    app, hanbit_id, _, _ = world
    raw = app.store.list_events(hanbit_id)
    view = service.get_view(app, hanbit_id, HANBIT)
    assert [e["seq"] for e in view["events"]] == [e["seq"] for e in raw]  # 줄 수와 순서는 그대로
    hidden_total = 0
    for before, after in zip(raw, view["events"], strict=True):
        cited = [e for e in (before.get("detail") or {}).get("evidence") or []]
        kept = (after["detail"] or {}).get("evidence") or []
        count = after["detail"].get("hiddenEvidenceCount", 0)
        assert set(kept) <= set(cited) and count >= len(set(cited) - set(kept))
        for evidence_id in kept:
            app.registry.get(evidence_id, HANBIT)  # 남은 것은 모두 열 수 있다
        if set(cited) - set(kept):
            assert before["detail"].get("orgId") != HANBIT  # 가려진 것은 상대 쪽이 한 일이다
        hidden_total += count
        assert {k: v for k, v in after.items() if k != "detail"} == {
            k: v for k, v in before.items() if k != "detail"
        }
    assert hidden_total > 0
    # 저장된 기록은 바뀌지 않는다(뷰를 만들 때만 가린다)
    assert app.store.list_events(hanbit_id) == raw
    assert not any("hiddenEvidenceCount" in (e.get("detail") or {}) for e in raw)


def test_vendor_view_hides_the_emitters_private_calculations(world):
    app, hanbit_id, _, _ = world
    emitter_only = {
        evidence.id
        for evidence in app.registry.list(org_id=None)
        if evidence.kind == "calc" and tuple(evidence.access) == (HANBIT,)
    }
    assert emitter_only  # 현행 처리 단가에서 나온 계산 근거
    for vendor in VENDORS:
        try:
            text = json.dumps(service.get_view(app, hanbit_id, vendor), ensure_ascii=False)
        except service.ServiceError:
            continue
        assert not [e for e in emitter_only if e in text], vendor


def test_scrub_handles_ids_in_any_position():
    app = make_app()
    open_id = next(e.id for e in app.registry.list(org_id=HANBIT) if e.kind == "law")
    closed = closed_ids(app, HANBIT)[:2]
    event = {
        "seq": 1,
        "detail": {
            "evidence": [open_id, closed[0]],
            "reasonEvidence": [closed[0], closed[1]],
            "primary": closed[1],
            "marks": {closed[0]: "abc", open_id: "def"},
            "nested": [{"evidence": [closed[1]]}],
            "text": "근거: 확인 필요",
            "codes": ["VALUE_MISMATCH"],
            "calledBy": "model:rule",  # 근거 ID처럼 생겼지만 근거가 아니다
        },
    }
    after = service._event_view(app, event, HANBIT, {})
    assert after["detail"] == {
        "evidence": [open_id],
        "reasonEvidence": [],
        "primary": None,
        "marks": {open_id: "def"},
        "nested": [{"evidence": []}],
        "text": "근거: 확인 필요",
        "codes": ["VALUE_MISMATCH"],
        "calledBy": "model:rule",
        "hiddenEvidenceCount": 2,
    }
    same = service._event_view(app, {"seq": 2, "detail": {"evidence": [open_id]}}, HANBIT, {})
    assert "hiddenEvidenceCount" not in same["detail"]
