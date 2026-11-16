"""합의 뒤에 업체가 조건을 바꾸면 승인을 막고, 다시 진행하면 새 조건으로 대화를 이어 간다.

회사와 수치는 모두 가상이다. 재현: 승인 전에 새길자원이 스프루 기준(pc_saegil_pp_a)의 단가를
낮췄는데 배출 기업의 승인이 그대로 통과했다.
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request

import pytest

from app.v3 import devserver, service
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    TODAY,
    approve_all,
    make_app,
    recheck,
    thread_of,
    to_approval,
)

CODE = "VENDOR_TERMS_CHANGED"


def set_criterion(app, criterion_id, vendor=SAEGIL, **changes):
    """업체 화면에서 매입 기준표의 한 줄을 고친다(PUT /vendors/{orgId})."""
    criteria = copy.deepcopy(service.get_vendor(app, vendor, vendor)["criteria"])
    for item in criteria:
        if item["id"] == criterion_id:
            item.update(changes)
    return service.update_vendor(app, vendor, {"criteria": criteria}, vendor)


def set_guardrails(app, vendor=SAEGIL, **changes):
    return service.update_vendor(app, vendor, {"guardrails": changes}, vendor)


def approval_of(app, session_id, vendor, viewer=HANBIT):
    view = service.get_view(app, session_id, viewer)
    thread = thread_of(view, vendor)
    return next((a for a in view["approvals"] if a["threadId"] == thread["id"]), None)


def blocked(error_call, *args):
    with pytest.raises(service.ServiceError) as caught:
        error_call(*args)
    assert caught.value.code == "APPROVAL_BLOCKED" and caught.value.status == 409
    return caught.value


def agreed_app():
    app = make_app()
    view = to_approval(app)
    return app, view, view["session"]["id"], thread_of(view, SAEGIL)


def price_of(view, stream_id):
    return next(a for a in view["result"]["assignments"] if a["streamId"] == stream_id)["unitPrice"]


# ── 재현과 막기 ──────────────────────────────────────────────────────


def test_price_cut_after_agreement_blocks_the_emitter_approval():
    app, view, session_id, thread = agreed_app()
    assert price_of(view, "st_sprue")["value"] == 320
    service.approve(app, session_id, thread["id"], SAEGIL)  # 업체가 먼저 승인해 둔 상태
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=220)
    # 고친 때에 이미 막히고 앞선 승인이 지워진다
    stored = next(a for a in app.store.list_approvals(session_id) if a["threadId"] == thread["id"])
    assert stored["blocked"] and not stored["emitter"] and not stored["vendor"]
    error = blocked(service.approve, app, session_id, thread["id"], HANBIT)
    assert "업체의 조건이 바뀌어" in error.message
    blocked(service.approve, app, session_id, thread["id"], SAEGIL)
    for viewer in (HANBIT, SAEGIL):
        item = approval_of(app, session_id, SAEGIL, viewer)
        assert item["blocked"] and item["blockedCodes"] == [CODE] and CODE in item["blockedReason"]
        assert item["emitter"] is False and item["vendor"] is False
    view = service.get_view(app, session_id, HANBIT)
    assert thread_of(view, SAEGIL)["status"] == "agreed" and view["documents"] == []
    # 다른 업체와의 합의는 그대로다
    for vendor in (MISO, NAMHAE):
        assert approval_of(app, session_id, vendor)["blocked"] is False


def test_rerun_continues_the_same_thread_with_the_new_terms():
    app, view, session_id, thread = agreed_app()
    service.approve(app, session_id, thread_of(view, MISO)["id"], HANBIT)
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=220)
    view = service.run_session(app, session_id, HANBIT)
    assert view["session"]["status"] == "awaiting_approval"
    again = thread_of(view, SAEGIL)
    assert again["id"] == thread["id"] and again["status"] == "agreed"
    kinds = [m["type"] for m in again["messages"]]
    assert kinds[:4] == [m["type"] for m in thread["messages"]]  # 앞선 대화는 그대로 남는다
    assert kinds[4] == "counter" and again["messages"][4]["model"]["provider"] == "code"
    assert again["messages"][4]["from"] == HANBIT and again["messages"][4]["claims"] == []
    assert any(k in ("offer", "conditional") for k in kinds[5:])
    assert again["turns"] <= again["maxTurns"] and again["agreedSeq"] > 4
    assert set(again["assignedStreamIds"]) == {"st_film", "st_sprue"}
    price = price_of(view, "st_sprue")
    assert price["value"] == 220 and price["evidence"][0].startswith("calc:judge_route:")
    assert app.registry.get(price["evidence"][0], HANBIT).value == 220
    item = approval_of(app, session_id, SAEGIL)
    assert item["blocked"] is False and item["blockedCodes"] == []
    assert item["emitter"] is False and item["vendor"] is False  # 새 조건은 새로 승인받는다
    # 바뀌지 않은 다른 대화에 해 둔 승인은 남는다
    assert approval_of(app, session_id, MISO)["emitter"] is True
    summaries = [e["summary"] for e in view["events"] if e["threadId"] == thread["id"]]
    assert any("대화를 다시 열었습니다" in s for s in summaries)
    checked, passed = recheck(app, view)
    assert checked == passed
    view = approve_all(app, view)
    assert view["session"]["status"] == "confirmed"
    request = next(
        d for d in view["documents"] if d["threadId"] == thread["id"] and d["kind"] == "request"
    )
    assert "합의 단가 220원/kg" in request["rendered"] and "320" not in request["rendered"]
    checked, passed = recheck(app, view)
    assert checked == passed


def test_rerun_without_a_change_does_nothing():
    app, _view, session_id, _thread = agreed_app()
    before = service.get_view(app, session_id, HANBIT)
    after = service.run_session(app, session_id, HANBIT)
    assert after["threads"] == before["threads"] and after["events"] == before["events"]
    assert after["session"]["status"] == "awaiting_approval"


# ── 무엇이 합의 조건에 영향을 주는가 ─────────────────────────────────


@pytest.mark.parametrize(
    "change",
    [
        lambda app: set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=300),  # 단가
        lambda app: set_criterion(app, "pc_saegil_pp_a", minMonthlyKg=700),  # 최소 물량
        lambda app: set_criterion(app, "pc_saegil_pp_a", maxContaminationPct=0.5),  # 상한
        lambda app: set_criterion(app, "pc_saegil_film_b", maxMoisturePct=1),  # 필름의 상한
        lambda app: set_criterion(app, "pc_saegil_pp_b", unitPriceKrwPerKg=330),  # 더 비싼 기준
        lambda app: set_guardrails(app, days=["화"]),  # 합의한 요일(목)이 빠졌다
        lambda app: set_guardrails(app, days=["월", "수"]),
        lambda app: set_guardrails(app, minPickupsPerMonth=4),  # 횟수
        lambda app: set_guardrails(app, floorPriceKrwPerKg=330),  # 합의 단가가 하한 아래가 됐다
        lambda app: set_guardrails(app, ceilingPriceKrwPerKg=300),
        lambda app: set_guardrails(app, autoDecline=["스프루"]),
        lambda app: service.update_vendor(app, SAEGIL, {"fleet": {"capacityKg": 100}}, SAEGIL),
    ],
)
def test_changes_that_touch_the_agreed_terms_block(change):
    app, _view, session_id, thread = agreed_app()
    service.approve(app, session_id, thread["id"], HANBIT)
    change(app)
    item = approval_of(app, session_id, SAEGIL)
    assert item["blocked"] and CODE in item["blockedCodes"]
    assert item["emitter"] is False
    blocked(service.approve, app, session_id, thread["id"], SAEGIL)


@pytest.mark.parametrize(
    "change",
    [
        lambda app: set_criterion(app, "pc_saegil_pp_b", unitPriceKrwPerKg=170),  # 쓰이지 않은 기준
        lambda app: set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=320),  # 같은 값
        lambda app: set_guardrails(app, days=["화", "목", "금"]),  # 요일이 늘었을 뿐이다
        lambda app: set_guardrails(app, floorPriceKrwPerKg=140),  # 합의 단가는 여전히 하한 위
        lambda app: set_guardrails(app, autoDecline=["PVC", "석면"]),
        lambda app: service.update_vendor(
            app, SAEGIL, {"model": {"provider": "ollama", "name": "qwen3"}}, SAEGIL
        ),
        lambda app: service.update_vendor(app, SAEGIL, {"fleet": {"costPerKm": 1500}}, SAEGIL),
    ],
)
def test_changes_that_do_not_touch_the_agreed_terms_do_not_block(change):
    app, _view, session_id, thread = agreed_app()
    service.approve(app, session_id, thread["id"], HANBIT)
    change(app)
    item = approval_of(app, session_id, SAEGIL)
    assert item["blocked"] is False and item["emitter"] is True
    service.approve(app, session_id, thread["id"], SAEGIL)
    assert thread_of(service.get_view(app, session_id, HANBIT), SAEGIL)["status"] == "confirmed"


def test_another_vendors_change_does_not_block_this_thread():
    app, _view, session_id, _thread = agreed_app()
    set_criterion(app, "pc_miso_pp_b", vendor=MISO, unitPriceKrwPerKg=150)
    assert approval_of(app, session_id, SAEGIL)["blocked"] is False
    assert approval_of(app, session_id, MISO)["blockedCodes"] == [CODE]
    assert approval_of(app, session_id, NAMHAE)["blocked"] is False


# ── 다시 진행한 뒤 ───────────────────────────────────────────────────


def test_new_terms_can_lose_the_stream_to_another_offer():
    """단가가 다른 업체의 제안보다 낮아지면 그 흐름은 옮겨 가고 그 대화도 승인을 다시 받는다."""
    app, view, session_id, _thread = agreed_app()
    miso = thread_of(view, MISO)
    service.approve(app, session_id, miso["id"], HANBIT)
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=150)  # 미소환경의 스프루 제안보다 낮다
    set_criterion(app, "pc_saegil_pp_b", unitPriceKrwPerKg=140)
    view = service.run_session(app, session_id, HANBIT)
    by_stream = {a["streamId"]: a for a in view["result"]["assignments"]}
    assert by_stream["st_sprue"]["vendorOrgId"] == MISO
    assert by_stream["st_film"]["vendorOrgId"] == SAEGIL
    assert thread_of(view, SAEGIL)["assignedStreamIds"] == ["st_film"]
    assert set(thread_of(view, MISO)["assignedStreamIds"]) == {"st_defect", "st_sprue"}
    moved = approval_of(app, session_id, MISO)
    assert moved["emitter"] is False and moved["blocked"] is False  # 배정이 늘어 다시 승인받는다
    view = approve_all(app, view)
    assert view["session"]["status"] == "confirmed"
    checked, passed = recheck(app, view)
    assert checked == passed


def test_confirmed_threads_keep_their_streams_through_a_renegotiation():
    app, view, session_id, _thread = agreed_app()
    miso = thread_of(view, MISO)
    service.approve(app, session_id, miso["id"], HANBIT)
    service.approve(app, session_id, miso["id"], MISO)
    documents = len(service.get_view(app, session_id, HANBIT)["documents"])
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=220)
    view = service.run_session(app, session_id, HANBIT)
    done = thread_of(view, MISO)
    assert done["status"] == "confirmed" and done["assignedStreamIds"] == ["st_defect"]
    assert len(view["documents"]) == documents
    assert thread_of(view, SAEGIL)["status"] == "agreed"
    assert view["session"]["status"] == "awaiting_approval"


def test_editing_the_same_line_twice_can_still_be_renegotiated():
    """두 번째로 고치면 업체가 낸 예전 답의 근거 값도 바뀐다. 그래도 새 조건으로 이어 갈 수 있다."""
    app, view, session_id, thread = agreed_app()
    raw = app.store.get_thread(thread["id"])
    raw["maxTurns"] = 24  # 턴 상한과 섞이지 않게 넉넉히 둔다(상한은 아래 테스트에서 따로 본다)
    app.store.put_thread(raw)
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=220)
    view = service.run_session(app, session_id, HANBIT)
    assert price_of(view, "st_sprue")["value"] == 220
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=240)
    item = approval_of(app, session_id, SAEGIL)
    assert item["blocked"] and CODE in item["blockedCodes"]
    view = service.run_session(app, session_id, HANBIT)
    assert price_of(view, "st_sprue")["value"] == 240
    item = approval_of(app, session_id, SAEGIL)
    assert item["blocked"] is False and item["blockedCodes"] == []
    view = approve_all(app, view)
    assert view["session"]["status"] == "confirmed"


def test_renegotiation_stays_inside_the_turn_limit():
    app, view, session_id, _thread = agreed_app()
    prices = iter((300, 280, 260, 250, 240))
    for _ in range(5):
        set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=next(prices))
        view = service.run_session(app, session_id, HANBIT)
        again = thread_of(view, SAEGIL)
        assert again["turns"] <= again["maxTurns"]
        if again["status"] != "agreed":
            break
    assert again["status"] == "handoff" and "턴 상한" in again["statusReason"]
    assert again["id"] not in {a["threadId"] for a in view["approvals"]}
    assert all(a["vendorOrgId"] != SAEGIL for a in view["result"]["assignments"])
    assert all(t["status"] != "confirmed" for t in view["threads"])


def test_terms_the_vendor_can_no_longer_offer_end_in_handoff_not_agreement():
    app, _view, session_id, thread = agreed_app()
    set_guardrails(app, floorPriceKrwPerKg=330)  # 기준표 단가가 하한 아래가 됐다
    view = service.run_session(app, session_id, HANBIT)
    again = thread_of(view, SAEGIL)
    # 업체 측은 가드레일 밖의 단가를 낼 수 없다. 두 번 거부된 뒤 담당자에게 넘어간다
    assert again["status"] == "handoff" and again["assignedStreamIds"] == []
    assert [m["type"] for m in again["messages"]][4:] == ["counter", "handoff"]
    assert thread["id"] not in {a["threadId"] for a in view["approvals"]}
    by_stream = {a["streamId"]: a for a in view["result"]["assignments"]}
    assert by_stream["st_sprue"]["vendorOrgId"] == MISO  # 다른 제안으로 옮겨 갔다
    # 받을 곳이 없어진 흐름은 "현행 유지 확정"이 아니라 "확인 필요"로 나간다
    film = by_stream["st_film"]
    assert film["vendorOrgId"] is None and film["status"] == "needs_information"
    assert [c["key"] for c in film["checks"]][-1] == "handoff"
    assert view["session"]["status"] == "awaiting_approval"
    assert all(t["status"] != "confirmed" for t in view["threads"])


def test_two_vendors_changing_at_once_are_both_renegotiated_in_one_run():
    app, view, session_id, _thread = agreed_app()
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=300)
    set_criterion(app, "pc_miso_pp_b", vendor=MISO, unitPriceKrwPerKg=190)
    view = service.run_session(app, session_id, HANBIT)
    assert view["session"]["status"] == "awaiting_approval"
    for vendor in (SAEGIL, MISO):
        again = thread_of(view, vendor)
        assert again["status"] == "agreed" and "counter" in [m["type"] for m in again["messages"]]
        assert approval_of(app, session_id, vendor)["blocked"] is False
    assert "counter" not in [m["type"] for m in thread_of(view, NAMHAE)["messages"]]
    assert price_of(view, "st_sprue")["value"] == 300
    assert price_of(view, "st_defect")["value"] == 190
    checked, passed = recheck(app, view)
    assert checked == passed
    assert approve_all(app, view)["session"]["status"] == "confirmed"


def test_a_vendor_that_closes_during_renegotiation_does_not_stop_the_other_thread():
    app, view, session_id, _thread = agreed_app()
    set_guardrails(app, floorPriceKrwPerKg=330)  # 새길자원은 더 낼 수 있는 조건이 없다
    set_criterion(app, "pc_miso_pp_b", vendor=MISO, unitPriceKrwPerKg=190)
    view = service.run_session(app, session_id, HANBIT)
    assert thread_of(view, SAEGIL)["status"] == "handoff"
    miso = thread_of(view, MISO)
    assert miso["status"] == "agreed" and set(miso["assignedStreamIds"]) == {
        "st_defect",
        "st_sprue",
    }
    assert price_of(view, "st_sprue")["value"] == 190
    assert view["session"]["status"] == "awaiting_approval"


def test_stream_change_together_with_vendor_change_is_not_reopened():
    app, view, session_id, thread = agreed_app()
    set_criterion(app, "pc_saegil_pp_a", unitPriceKrwPerKg=220)
    service.update_stream(app, HANBIT, "st_sprue", {"monthlyKg": 640}, HANBIT)
    item = approval_of(app, session_id, SAEGIL)
    assert {"STREAM_CHANGED", CODE} <= set(item["blockedCodes"])
    view = service.run_session(app, session_id, HANBIT)
    same = thread_of(view, SAEGIL)
    assert [m["type"] for m in same["messages"]] == [m["type"] for m in thread["messages"]]
    assert approval_of(app, session_id, SAEGIL)["blocked"] is True


# ── HTTP ─────────────────────────────────────────────────────────────


def call(base, method, path, org, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"X-Org-Id": org, "Content-Type": "application/json"}
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_reported_case_over_http():
    app = devserver.build_app(today=TODAY, env={})
    server = devserver.make_server(app, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"
    try:
        _, view = call(base, "POST", "/sessions", HANBIT, {})
        session = f"/sessions/{view['session']['id']}"
        for _ in range(4):
            _, view = call(base, "POST", f"{session}/run", HANBIT)
            if view["session"]["status"] != "awaiting_answers":
                break
            for question in view["questions"]:
                if not question["answered"]:
                    call(
                        base,
                        "POST",
                        f"{session}/answers",
                        HANBIT,
                        {"questionId": question["id"], "value": "unknown"},
                    )
        assert view["session"]["status"] == "awaiting_approval"
        thread = thread_of(view, SAEGIL)
        _, profile = call(base, "GET", "/vendors/org_saegil", SAEGIL)
        for item in profile["criteria"]:
            if item["id"] == "pc_saegil_pp_a":
                assert item["unitPriceKrwPerKg"] == 320
                item["unitPriceKrwPerKg"] = 220
        status, _ = call(
            base, "PUT", "/vendors/org_saegil", SAEGIL, {"criteria": profile["criteria"]}
        )
        assert status == 200
        status, body = call(
            base, "POST", f"{session}/approvals", HANBIT, {"threadId": thread["id"]}
        )
        assert status == 409 and body["error"]["code"] == "APPROVAL_BLOCKED"
        _, view = call(base, "GET", session, SAEGIL)
        item = next(a for a in view["approvals"] if a["threadId"] == thread["id"])
        assert item["blocked"] and item["blockedCodes"] == [CODE]
        status, view = call(base, "POST", f"{session}/run", HANBIT)
        assert status == 200 and view["session"]["status"] == "awaiting_approval"
        assert price_of(view, "st_sprue")["value"] == 220
        for org in (HANBIT, SAEGIL):
            status, view = call(
                base, "POST", f"{session}/approvals", org, {"threadId": thread["id"]}
            )
            assert status == 200
        assert thread_of(view, SAEGIL)["status"] == "confirmed"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
