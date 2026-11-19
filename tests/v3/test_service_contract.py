"""test_api.py가 HTTP로 하는 호출 순서를 service 함수로 직접 따라 한다(순수 파이썬).

test_api.py, test_vendor_service.py는 fastapi가 없는 곳에서는 건너뛴다. 그 테스트들이 가정한
서비스 동작(시드, "모름" 답, 승인 전 문서, 오류 코드와 상태, 조직 시야)이 실제 service.py와
맞는지 여기서 확인한다. api.build_app처럼 시드하지 않은 앱에서 reset_demo부터 시작한다.
회사와 수치는 모두 가상이다.
"""

import pytest

from app.v3 import service
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore

EMITTER, OTHER_EMITTER = "org_hanbit", "org_daon"
ADMIN = "org_admin"
VENDORS = ("org_saegil", "org_namhae", "org_miso")
HIDDEN = (403, 404)  # test_api.py와 같은 뜻: 볼 수 없는 것은 금지나 없음으로 답한다


def rule_factory(role, org_id, model=None):
    """api.make_llm_factory("rule")와 같다: 모델을 주지 않는다(None이면 규칙 정책으로 돈다)."""


def fresh_app(**options):
    """api.build_app이 메모리 저장으로 만드는 것과 같은 모양. 시드는 reset_demo가 올린다."""
    app = service.App(InMemoryStore(), InMemoryRegistry(), llm_factory=rule_factory, **options)
    service.reset_demo(app)
    return app


@pytest.fixture
def app():
    return fresh_app()


def error_of(call, *args):
    with pytest.raises(service.ServiceError) as caught:
        call(*args)
    error = caught.value
    body = error.to_dict()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] and body["error"]["message"]
    return error


def run_to_approval(app, session_id):
    """test_api.run_to_approval과 같은 순서: 멈출 때마다 모든 질문에 "모름"으로 답한다."""
    view = None
    for _ in range(6):
        view = service.run_session(app, session_id, EMITTER)
        if view["session"]["status"] != "awaiting_answers":
            break
        open_questions = [q for q in view["questions"] if not q["answered"]]
        assert open_questions, "awaiting_answers인데 답할 질문이 없다"
        for question in open_questions:
            service.answer_question(app, session_id, question["id"], "unknown", EMITTER)
    return view


def test_reset_on_an_unseeded_app_loads_laws_references_and_demo():
    app = service.App(InMemoryStore(), InMemoryRegistry(), llm_factory=rule_factory)
    assert service.list_orgs(app) == []  # build_app은 시드하지 않는다
    summary = service.reset_demo(app)
    assert summary["seedEvidence"] > 30
    assert {EMITTER, OTHER_EMITTER, *VENDORS} <= {o["id"] for o in service.list_orgs(app)}
    status = service.admin_status(app, ADMIN)
    assert status["laws"]["count"] > 0 and status["evidence"]["byKind"]["ref"] > 0
    assert status["evidence"]["byKind"]["assumption"] > 0  # 가정값도 함께 올라온다
    streams = service.list_streams(app, EMITTER, EMITTER)
    assert {"st_sprue", "st_defect", "st_purge", "st_film"} <= {s["id"] for s in streams}
    assert all(s["orgId"] == EMITTER for s in streams)


def test_reset_twice_gives_the_same_start(app):
    before = (service.list_orgs(app), service.list_streams(app, EMITTER, EMITTER))
    created = service.create_session(app, EMITTER, EMITTER)
    # 조직이 이미 있는 저장소는 관리자만 되돌린다(빈 저장소의 첫 시드만 누구나 부른다)
    assert error_of(service.reset_demo, app).status == 401
    assert error_of(service.reset_demo, app, EMITTER).status == 403
    assert service.get_view(app, created["session"]["id"], EMITTER)["session"]["status"] == "idle"
    service.reset_demo(app, ADMIN)
    assert (service.list_orgs(app), service.list_streams(app, EMITTER, EMITTER)) == before
    assert error_of(service.get_view, app, created["session"]["id"], EMITTER).status == 404


def test_error_codes_and_statuses(app):
    # 헤더가 없을 때 api.py는 서비스에 가기 전에 401 ORG_REQUIRED를 낸다. 서비스도 같은 답이다.
    missing = error_of(service.list_streams, app, EMITTER, "")
    assert (missing.code, missing.status) == ("ORG_REQUIRED", 401)
    unknown = error_of(service.list_streams, app, EMITTER, "org_없는조직")
    assert (unknown.code, unknown.status) == ("UNKNOWN_ORG", 401)
    no_session = error_of(service.get_view, app, "ses_없는것", EMITTER)
    assert (no_session.code, no_session.status) == ("SESSION_NOT_FOUND", 404)
    no_evidence = error_of(service.get_evidence, app, "measure:없는:것", EMITTER)
    assert (no_evidence.code, no_evidence.status) == ("EVIDENCE_NOT_FOUND", 404)
    bad_kind = error_of(service.search_evidence, app, "폐기물", ["없는종류"], EMITTER)
    assert (bad_kind.code, bad_kind.status) == ("INVALID_KIND", 422)
    wrong_role = error_of(service.create_session, app, VENDORS[0], VENDORS[0])
    assert (wrong_role.code, wrong_role.status) == ("WRONG_ROLE", 403)
    no_vendor = error_of(service.get_vendor, app, "org_없는업체", EMITTER)
    assert (no_vendor.code, no_vendor.status) == ("VENDOR_NOT_FOUND", 404)
    bad_value = error_of(
        service.update_stream, app, EMITTER, "st_sprue", {"monthlyKg": -1}, EMITTER
    )
    assert (bad_value.code, bad_value.status) == ("INVALID_VALUE", 422)
    bad_doc = error_of(service.upload_document, app, EMITTER, "없는종류", "a.txt", b"x", EMITTER)
    assert (bad_doc.code, bad_doc.status) == ("INVALID_KIND", 422)


def test_emitters_cannot_see_each_other(app):
    assert error_of(service.list_streams, app, EMITTER, OTHER_EMITTER).status in HIDDEN
    changed = error_of(
        service.update_stream, app, EMITTER, "st_sprue", {"name": "남의 흐름"}, OTHER_EMITTER
    )
    assert changed.status in HIDDEN
    assert error_of(service.create_session, app, EMITTER, OTHER_EMITTER).status in HIDDEN
    mine = service.list_streams(app, EMITTER, EMITTER)
    assert next(s for s in mine if s["id"] == "st_sprue")["name"] != "남의 흐름"
    private = [
        evidence_id for s in mine for evidence_id in (s.get("measure") or {}).get("evidence") or []
    ]
    assert private, "시연 흐름에 측정값 근거가 있어야 한다"
    assert service.get_evidence(app, private[0], EMITTER)["evidence"]["id"] == private[0]
    assert error_of(service.get_evidence, app, private[0], OTHER_EMITTER).status in HIDDEN


def test_demo_scenario_with_unknown_answers_reaches_confirmation(app):
    created = service.create_session(app, EMITTER, EMITTER)
    session_id = created["session"]["id"]
    assert created["session"]["emitterOrgId"] == EMITTER
    assert created["viewer"]["orgId"] == EMITTER

    view = run_to_approval(app, session_id)
    assert view["session"]["status"] == "awaiting_approval"
    assert [s["stage"] for s in view["stages"]] == [1, 2, 3, 4, 5, 6]
    for thread in view["threads"]:
        assert thread["turns"] <= thread["maxTurns"]
        for message in thread["messages"]:
            assert message["validation"]["ok"], message
    batches = {}
    for question in view["questions"]:
        batches[question["batch"]] = batches.get(question["batch"], 0) + 1
        assert question["answered"] and question["unknown"]  # "모름"은 유효한 답이다
    assert set(batches) <= {1, 2} and all(count <= 3 for count in batches.values())
    assert view["events"], "실행 기록이 있어야 한다"
    assert view["documents"] == []  # 승인 전에는 문서를 만들지 않는다

    # 승인 레코드의 모양(test_api.py가 읽는 키)
    for approval in view["approvals"]:
        assert {"threadId", "emitter", "vendor", "blocked", "blockedReason"} <= set(approval)
    blocked = {a["threadId"] for a in view["approvals"] if a["blocked"]}
    agreed = [t for t in view["threads"] if t["status"] == "agreed" and t["id"] not in blocked]
    assert agreed, "전부 '모름'으로 답해도 합의된 스레드가 하나 이상 나와야 한다"
    for thread in agreed:
        service.approve(app, session_id, thread["id"], EMITTER)
        halfway = service.get_view(app, session_id, EMITTER)
        mine = next(t for t in halfway["threads"] if t["id"] == thread["id"])
        assert mine["status"] == "agreed"  # 한쪽만 승인하면 확정하지 않는다
        assert not [d for d in halfway["documents"] if d["threadId"] == thread["id"]]
        service.approve(app, session_id, thread["id"], thread["vendorOrgId"])

    final = service.get_view(app, session_id, EMITTER)
    statuses = {t["id"]: t["status"] for t in final["threads"]}
    assert all(statuses[t["id"]] == "confirmed" for t in agreed)
    assert final["documents"] and {d["kind"] for d in final["documents"]} <= {
        "request",
        "plan",
        "contract",
    }
    assert all(d["validation"]["ok"] for d in final["documents"])

    # 업체는 자기 스레드와 그 스레드의 흐름만 본다. 다른 배출 기업은 진단을 보지 못한다.
    vendor_id = agreed[0]["vendorOrgId"]
    vendor_view = service.get_view(app, session_id, vendor_id)
    assert vendor_view["viewer"]["orgId"] == vendor_id
    assert vendor_view["threads"]
    assert all(t["vendorOrgId"] == vendor_id for t in vendor_view["threads"])
    shared = {sid for t in vendor_view["threads"] for sid in t["streamIds"]}
    assert {s["id"] for s in vendor_view["streams"]} <= shared
    assert vendor_view["questions"] == []
    assert error_of(service.get_view, app, session_id, OTHER_EMITTER).status in HIDDEN
    assert error_of(service.run_session, app, session_id, OTHER_EMITTER).status in HIDDEN
    # 업체는 진단을 진행하거나 질문에 답하지 못한다
    assert error_of(service.run_session, app, session_id, vendor_id).status == 403
    assert error_of(service.approve, app, session_id, "th_없는것", EMITTER).status == 404


def test_first_document_appears_only_after_both_sides_approve(app):
    """test_api.py는 스레드를 하나씩 확정하며 그 사이에 documents == []를 기대한다.

    두 번째 스레드부터는 앞 스레드의 문서가 이미 있으므로, 그 가정은 첫 스레드에서만 참이다.
    """
    session_id = service.create_session(app, EMITTER, EMITTER)["session"]["id"]
    view = run_to_approval(app, session_id)
    agreed = [t for t in view["threads"] if t["status"] == "agreed"]
    first = agreed[0]
    service.approve(app, session_id, first["id"], EMITTER)
    assert service.get_view(app, session_id, EMITTER)["documents"] == []
    service.approve(app, session_id, first["id"], first["vendorOrgId"])
    after = service.get_view(app, session_id, EMITTER)["documents"]
    assert after and {d["threadId"] for d in after} == {first["id"]}


def test_evidence_search_returns_evidence_with_score(app):
    items = service.search_evidence(app, "폐기물", ["law"], EMITTER)
    assert items and all(set(item) == {"evidence", "score"} for item in items)
    assert all(item["evidence"]["kind"] == "law" for item in items)
    one = service.get_evidence(app, items[0]["evidence"]["id"], EMITTER)
    assert one["evidence"]["id"] == items[0]["evidence"]["id"]
    assert isinstance(one["chain"], list)


def test_csv_template_needs_no_special_role_and_admin_status_needs_admin(app):
    assert service.csv_template(app).strip()
    assert error_of(service.admin_status, app, EMITTER).status == 403
    assert error_of(service.admin_status, app, VENDORS[0]).status == 403
    assert service.admin_status(app, ADMIN)["today"] == app.today
    assert service.admin_status(app, ADMIN)["store"] == "InMemoryStore"


def test_vendor_entry_point_answers_one_message(app):
    """vendor_service.py가 부르는 진입점: 메시지 하나에 답 메시지(dict) 하나."""
    session_id = service.create_session(app, EMITTER, EMITTER)["session"]["id"]
    view = run_to_approval(app, session_id)
    thread = next(t for t in view["threads"] if t["messages"])
    vendor_id = thread["vendorOrgId"]
    raw = app.store.get_thread(thread["id"])
    inquiry = next(m for m in raw["messages"] if m["type"] == "inquiry")
    streams = [
        {k: v for k, v in s.items() if k != "currentTreatment"}
        for s in service.list_streams(app, EMITTER, EMITTER)
        if s["id"] in thread["streamIds"]
    ]
    emitter = next(o for o in service.list_orgs(app) if o["id"] == EMITTER)
    context = {
        "sessionId": session_id,
        "today": app.today,
        "thread": {**raw, "messages": [inquiry]},
        "streams": streams,
        "emitter": {k: emitter.get(k) for k in ("id", "name", "location")},
    }
    reply = service.handle_vendor_message(app, vendor_id, inquiry, context)
    assert (reply["from"], reply["to"]) == (vendor_id, EMITTER)
    assert reply["type"] in ("question", "offer", "conditional", "decline", "handoff")
    assert {"validation", "model"} <= set(reply["meta"])
    missing = error_of(service.handle_vendor_message, app, "org_없는업체", inquiry, context)
    assert (missing.code, missing.status) == ("VENDOR_NOT_FOUND", 404)
