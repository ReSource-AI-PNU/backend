"""회사별 기록(app/v3/memory.py): 지난 답변·검수·교정·거래를 회사 하나 기준으로 묶고 계약 검토에 쓴다.

2026-11-22 사용자 요청("기업마다 특성이 다르니 루프에서 남는 기록으로 개인화 메모리를 만들어 서비스 품질을 높이자").
새로 저장하는 값은 없고(켜기·끄기만 조직에 남긴다), 평균을 내거나 판정 숫자를 바꾸지 않는다.
"""

import uuid

import pytest

from app.v3 import memory, service
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    SAEGIL,
    approve_all,
    make_app,
    thread_of,
    to_approval,
)


def _record(app, view, grade, price=320, stream="st_sprue"):
    body = {
        "requestId": f"req-{uuid.uuid4().hex}",
        "threadId": thread_of(view, SAEGIL, "confirmed")["id"],
        "streamId": stream,
        "actualGrade": grade,
        "actualUnitPriceKrwPerKg": price,
        "actualKg": 500,
        "inspectedOn": "2026-11-16",
    }
    return service.record_outcome(app, view["session"]["id"], body, SAEGIL)["outcome"]


def _history(view, vendor):
    thread = thread_of(view, vendor)
    approval = next(a for a in view["approvals"] if a["threadId"] == thread["id"])
    return next((i for i in approval["review"]["items"] if i["key"] == "history"), None)


@pytest.fixture
def dealt():
    """한빛과 새길이 한 번 확정한 앱과 그 진단 뷰."""
    app = make_app()
    return app, approve_all(app, to_approval(app))


def test_inspections_are_compared_with_the_agreed_grade_and_price(dealt):
    app, view = dealt
    _record(app, view, "A", 320)
    _record(app, view, "B", 300)
    found = service.get_memory(app, HANBIT, HANBIT)
    sprue = next(s for s in found["streams"] if s["streamId"] == "st_sprue")
    rows = sprue["inspectionRows"]
    assert [(r["agreedGrade"], r["actualGrade"], r["grade"]) for r in rows] == [
        ("A", "A", "same"),
        ("A", "B", "lower"),
    ]
    assert [r["price"] for r in rows] == ["same", "lower"]
    assert all(r["vendorName"] for r in rows)
    # 기록이 적으면 참고만 한다(평균을 내지 않고 건수만 센다)
    assert sprue["inspections"]["status"] == "few"
    assert sprue["inspections"]["count"] == 2 and sprue["inspections"]["below"] == 1
    assert found["summary"]["inspections"] == 2 and found["summary"]["deals"] >= 1
    assert found["dataOrigin"] == "demo" and found["enabled"] is True


def test_answers_from_earlier_searches_are_listed_with_the_unknown_ones(dealt):
    app, _ = dealt
    found = service.get_memory(app, HANBIT, HANBIT)
    answers = [a for s in found["streams"] for a in s["answers"]]
    assert answers and all(a["answeredOn"] for a in answers)
    unknown = [a for a in answers if a["unknown"]]
    # 시연 담당자는 몇 항목을 '모름'으로 답한다. 모름은 다시 묻지 않으므로 자료 화면에서 채우도록 보인다
    assert unknown and all(a["answer"] is None and a["stillUnknown"] for a in unknown)
    assert found["summary"]["stillUnknown"] == len(unknown)


def test_review_asks_a_person_when_most_inspections_came_in_below(dealt):
    app, view = dealt
    for grade in ("B", "C", "C"):
        _record(app, view, grade)
    again = to_approval(app)
    item = _history(again, SAEGIL)
    assert item is not None and item["status"] == "check"
    assert "절반 이상" in item["text"] and item["evidence"]
    # 숫자는 문장에 쓰지 않는다(건수와 값은 회사 기록 화면이 근거와 함께 보인다)
    assert not any(ch.isdigit() for ch in item["text"])
    approval = next(
        a for a in again["approvals"] if a["threadId"] == thread_of(again, SAEGIL)["id"]
    )
    assert approval["review"]["status"] == "needs_check"
    # 다른 업체의 검토에는 새길과의 검수 이력을 싣지 않는다(검토는 상대 업체도 본다)
    assert _history(again, MISO) is None


def test_few_records_are_only_shown_for_reference(dealt):
    app, view = dealt
    _record(app, view, "C")
    item = _history(to_approval(app), SAEGIL)
    assert item is not None and item["status"] == "ok" and "참고로만" in item["text"]


def test_records_as_agreed_pass(dealt):
    app, view = dealt
    for _ in range(3):
        _record(app, view, "A", 320)
    item = _history(to_approval(app), SAEGIL)
    assert item is not None and item["status"] == "ok" and "합의한 등급과 단가대로" in item["text"]


def test_turning_memory_off_keeps_records_but_review_does_not_use_them(dealt):
    app, view = dealt
    for grade in ("B", "C", "C"):
        _record(app, view, grade)
    off = service.update_memory(app, HANBIT, {"enabled": False}, HANBIT)
    assert off["enabled"] is False and off["summary"]["inspections"] == 3
    assert _history(to_approval(app), SAEGIL) is None
    on = service.update_memory(app, HANBIT, {"enabled": True}, HANBIT)
    assert on["enabled"] is True and memory.enabled(app.store.get_org(HANBIT))


def test_only_the_company_itself_reads_or_changes_its_memory(dealt):
    app, _ = dealt
    with pytest.raises(service.ServiceError) as caught:
        service.get_memory(app, HANBIT, SAEGIL)
    assert caught.value.status == 403
    with pytest.raises(service.ServiceError) as caught:
        service.update_memory(app, HANBIT, {"enabled": "no"}, HANBIT)
    assert caught.value.status == 422
    # 처리 업체는 자기 쪽 기록(배출 기업별 검수와 거래)을 본다. 판정 반영은 배출 기업만 정한다
    assert service.get_memory(app, SAEGIL, SAEGIL)["role"] == "vendor"
    with pytest.raises(service.ServiceError) as caught:
        service.update_memory(app, SAEGIL, {"enabled": False}, SAEGIL)
    assert caught.value.code == "WRONG_ROLE"


def test_a_real_company_does_not_see_demo_records(dealt):
    app, view = dealt
    _record(app, view, "B")
    org = app.store.get_org(HANBIT)
    app.store.put_org({**org, "synthetic": False, "demo": False})
    found = service.get_memory(app, HANBIT, HANBIT)
    assert found["dataOrigin"] == "real" and found["summary"]["inspections"] == 0
