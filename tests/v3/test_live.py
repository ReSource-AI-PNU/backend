"""실시간 진행: 뒤에서 도는 실행(POST /run?mode=async), 실행 자리(409, 멈춘 자리), SSE 스트림.

메모리 저장과 규칙 정책으로 돈다(SQL은 SQLite 파일). 네트워크를 쓰지 않는다. 회사와 수치는 모두 가상이다.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app as fastapi_app
from app.v3 import api, service
from app.v3.agents.policy_rule import RulePolicy
from app.v3.demo.seed import DEMO_ANSWERS
from app.v3.store import InMemoryStore
from tests.v3.runtime_fixtures import (
    ADMIN,
    DAON,
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    make_app,
    only_for,
    scripted,
)

BAD_OFFER = {  # 자리표시자 밖에 숫자가 있는 초안(근거 검증기가 거부한다)
    "type": "offer",
    "stage": 5,
    "streamIds": ["st_sprue"],
    "text": "스프루·런너는 kg당 320원에 받겠습니다.",
    "toolCalls": [],
    "terms": {"items": [{"streamId": "st_sprue", "unitPriceRef": "pc_saegil_pp_a_unitPrice"}]},
}


def org(org_id, **extra):
    return {"X-Org-Id": org_id, **extra}


def wait_until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("기다린 일이 일어나지 않았다")
        time.sleep(0.01)


class Gate:
    """armed일 때 회사 측 모델 호출을 release까지 붙잡아 둔다(실행이 도는 중인 순간을 만든다)."""

    def __init__(self) -> None:
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def hold(self) -> None:
        if self.armed and not self.release.is_set():
            self.entered.set()
            assert self.release.wait(30), "문이 열리지 않았다"


class GatedRule:
    provider, model = RulePolicy.provider, RulePolicy.model

    def __init__(self, gate: Gate, boom: bool = False) -> None:
        self.gate, self.boom = gate, boom

    def complete(self, messages, *, tools=None, response_schema=None):
        if self.boom and self.gate.armed:
            self.gate.entered.set()
            raise RuntimeError("가상 장애")
        self.gate.hold()
        return RulePolicy().complete(messages, tools=tools, response_schema=response_schema)


@pytest.fixture
def world(monkeypatch):
    """(client, state, gate). 회사 측 모델은 Gate를 지나는 규칙 정책이다."""
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_llm", "rule")
    monkeypatch.setattr(settings, "v3_auth", "header")
    gate = Gate()
    state = make_app(lambda role, _org, _model: GatedRule(gate) if role == "company" else None)
    api.set_state(state)
    with TestClient(fastapi_app) as client:
        yield client, state, gate
        gate.release.set()
        assert api.wait_runs(30)
    api.set_state(None)


def sse(lines, stop=None):
    """SSE 줄(iter_lines)을 [{"event", "id", "data"}]로 읽는다. done이나 stop(item)이 참인 데서 멈춘다.

    같은 응답을 이어 읽으려면 같은 줄 반복자를 다시 넘긴다.
    """
    items, block = [], {}
    for line in lines:
        if line.startswith(":"):
            continue
        if line:
            field, _, value = line.partition(":")
            block[field] = value.removeprefix(" ")
            continue
        if "data" in block:
            item = {
                "event": block.get("event", "message"),
                "id": int(block["id"]) if "id" in block else None,
                "data": json.loads(block["data"]),
            }
            items.append(item)
            if item["event"] == "done" or (stop is not None and stop(item)):
                return items
        block = {}
    return items


def stream(client, session_id, headers, **params):
    with client.stream(
        "GET", f"/api/v3/sessions/{session_id}/stream", headers=headers, params=params
    ) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        return sse(response.iter_lines())


def answer_open(client, session_id):
    view = client.get(f"/api/v3/sessions/{session_id}", headers=org(HANBIT)).json()
    for question in view["questions"]:
        if not question["answered"]:
            value = DEMO_ANSWERS.get((question["streamId"], question["field"]), "unknown")
            answered = client.post(
                f"/api/v3/sessions/{session_id}/answers",
                headers=org(HANBIT),
                json={"questionId": question["id"], "value": value},
            )
            assert answered.status_code == 200, answered.text


def run_async_until(client, session_id, reached):
    """비동기로 돌리며 담당자 질문에 답한다. reached()가 참이 되면 그 실행의 202 본문을 준다."""
    for _ in range(6):
        started = client.post(f"/api/v3/sessions/{session_id}/run?mode=async", headers=org(HANBIT))
        assert started.status_code == 202, started.text
        wait_until(lambda: reached() or not api._running)
        if reached():
            return started.json()
        answer_open(client, session_id)
    raise AssertionError("기다린 지점에 닿지 않았다")


def new_session(client):
    created = client.post("/api/v3/sessions", headers=org(HANBIT), json={})
    assert created.status_code == 200, created.text
    return created.json()["session"]["id"]


def to_approval_async(client, session_id):
    for _ in range(6):
        started = client.post(f"/api/v3/sessions/{session_id}/run?mode=async", headers=org(HANBIT))
        assert started.status_code == 202, started.text
        assert api.wait_runs(30)
        view = client.get(f"/api/v3/sessions/{session_id}", headers=org(HANBIT)).json()
        if view["session"]["status"] != "awaiting_answers":
            return view
        answer_open(client, session_id)
    raise AssertionError("승인 대기에 닿지 않았다")


# ── 뒤에서 도는 실행과 실행 자리 ─────────────────────────────────────
def test_async_run_returns_202_and_finishes(world):
    client, state, _ = world
    sid = new_session(client)
    started = client.post(f"/api/v3/sessions/{sid}/run?mode=async", headers=org(HANBIT))
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["sessionId"] == sid and body["status"] == "running"
    assert body["runId"].startswith("run_") and isinstance(body["after"], int)
    assert api.wait_runs(30)
    view = to_approval_async(client, sid)
    assert view["session"]["status"] == "awaiting_approval"
    assert state.store.get_run(sid) is None  # 끝나면 자리를 비운다
    # 동기 실행(질의 없음, mode=sync)은 전처럼 진단 뷰를 준다. 모르는 mode는 422
    again = client.post(f"/api/v3/sessions/{sid}/run", headers=org(HANBIT))
    assert again.status_code == 200 and again.json()["session"]["status"] == "awaiting_approval"
    assert client.post(f"/api/v3/sessions/{sid}/run?mode=sync", headers=org(HANBIT)).is_success
    wrong = client.post(f"/api/v3/sessions/{sid}/run?mode=later", headers=org(HANBIT))
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "INVALID_VALUE"
    # 업체는 진단을 진행할 수 없다(동기와 같다)
    vendor = client.post(f"/api/v3/sessions/{sid}/run?mode=async", headers=org(SAEGIL))
    assert vendor.status_code == 403 and vendor.json()["error"]["code"] == "FORBIDDEN"


def test_second_run_while_running_is_409_and_events_are_live(world):
    client, state, gate = world
    sid = new_session(client)
    gate.armed = True
    started = run_async_until(client, sid, gate.entered.is_set)
    # 실행은 회사 측 모델 호출에 붙잡혀 있다. 그 앞의 일은 이미 진행 기록으로 보인다
    view = client.get(f"/api/v3/sessions/{sid}", headers=org(HANBIT)).json()
    assert view["session"]["status"] == "running"
    listed = client.get("/api/v3/sessions", headers=org(HANBIT)).json()
    assert next(s for s in listed if s["id"] == sid)["status"] == "running"
    for method, path, body in (
        ("POST", f"/api/v3/sessions/{sid}/run?mode=async", None),
        ("POST", f"/api/v3/sessions/{sid}/run", None),
        ("POST", f"/api/v3/sessions/{sid}/answers", {"questionId": "q_1", "value": "unknown"}),
    ):
        busy = client.request(method, path, headers=org(HANBIT), json=body)
        assert busy.status_code == 409, (path, busy.text)
        assert busy.json()["error"]["code"] == "RUN_IN_PROGRESS"
    with client.stream(
        "GET",
        f"/api/v3/sessions/{sid}/stream",
        headers=org(HANBIT),
        params={"after": started["after"]},
    ) as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        before = sse(
            lines,
            stop=lambda item: item["event"] == "activity" and item["data"]["kind"] == "drafting",
        )
        # 작성 중이 보이는 동안 실행은 아직 끝나지 않았다(끝에 한꺼번에 나오는 것이 아니다)
        assert not gate.release.is_set() and api._running
        assert before[-1]["data"]["author"] == {
            "side": "company",
            "kind": "agent",
            "label": "회사 측 AI",
        }
        assert before[0]["id"] == started["after"] + 1
        assert before[0]["event"] == "status" and before[0]["data"]["status"] == "running"
        gate.release.set()
        after = sse(lines)
    items = before + after
    ids = [item["id"] for item in items if item["id"] is not None]
    assert ids == sorted(set(ids))  # 순서대로, 겹치지 않게
    assert items[-1]["event"] == "done" and items[-1]["data"]["status"] in (
        "awaiting_answers",
        "awaiting_approval",
    )
    assert items[-1]["data"]["lastSeq"] == ids[-1]
    kinds = [i["data"]["kind"] for i in items if i["event"] == "activity"]
    first = kinds.index("drafting")
    assert "validating" in kinds[first:] and "sent" in kinds[first:]
    assert state.store.get_run(sid) is None


def test_stream_orders_events_and_resumes_without_duplicates(world):
    client, _, _ = world
    sid = new_session(client)
    view = to_approval_async(client, sid)
    full = stream(client, sid, org(HANBIT))
    assert full[-1] == {
        "event": "done",
        "id": None,
        "data": {"status": "awaiting_approval", "lastSeq": full[-2]["id"]},
    }
    ids = [item["id"] for item in full[:-1]]
    assert ids == list(range(1, len(ids) + 1))  # 배출 기업은 모든 기록을 본다
    assert {item["event"] for item in full} == {"activity", "status", "question", "done"}
    # 시연 시나리오(규칙 정책)에서도 대화마다 작성 중 → 근거 확인 → 보냄이 보인다
    threads = {t["id"]: t for t in view["threads"] if t["status"] != "not_opened"}
    for thread_id, thread in threads.items():
        mine = [
            i["data"]
            for i in full
            if i["event"] == "activity" and i["data"]["threadId"] == thread_id
        ]
        steps = [d["kind"] for d in mine if d["kind"] in ("drafting", "validating", "sent")]
        assert steps[:3] == ["drafting", "validating", "sent"], thread_id
        sent = [d for d in mine if d["kind"] == "sent"]
        assert [d["message"] for d in sent] == thread["messages"]  # 진단 뷰와 같은 메시지
        assert all(d["evidenceCount"] == len(d["message"]["claims"]) for d in sent)
        assert all(d["vendorName"] == thread["vendorName"] for d in mine)
    questions = [i for i in full if i["event"] == "question"]
    assert questions and all(q["data"]["questions"] for q in questions)
    status = [i["data"] for i in full if i["event"] == "status"]
    assert status[-1]["status"] == "awaiting_approval" and len(status[-1]["stages"]) == 6
    # 이어 받기: Last-Event-ID와 ?after= 가운데 큰 값 다음부터, 겹치지 않게
    middle = ids[len(ids) // 2]
    tail = [item for item in full if item["id"] is None or item["id"] > middle]
    assert stream(client, sid, org(HANBIT, **{"Last-Event-ID": str(middle)})) == tail
    assert stream(client, sid, org(HANBIT), after=middle) == tail
    assert stream(client, sid, org(HANBIT, **{"Last-Event-ID": str(middle)}), after=2) == tail
    assert stream(client, sid, org(HANBIT, **{"Last-Event-ID": "x"}), after=middle) == tail
    end = stream(client, sid, org(HANBIT), after=ids[-1])
    assert end == [{"event": "done", "id": None, "data": full[-1]["data"]}]
    wrong = client.get(f"/api/v3/sessions/{sid}/stream?after=-1", headers=org(HANBIT))
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "INVALID_VALUE"


def test_vendor_sees_only_its_own_thread(world):
    client, _, _ = world
    sid = new_session(client)
    to_approval_async(client, sid)
    full = stream(client, sid, org(HANBIT))
    for vendor in (SAEGIL, NAMHAE, MISO):
        view = client.get(f"/api/v3/sessions/{sid}", headers=org(vendor)).json()
        mine = {t["id"]: t for t in view["threads"]}
        assert len(mine) == 1
        items = stream(client, sid, org(vendor))
        assert items[-1]["event"] == "done"
        assert "question" not in {i["event"] for i in items}  # 담당자 질문은 배출 기업만
        shown = [i["data"] for i in items if i["event"] == "activity"]
        assert shown and all(d["threadId"] in mine for d in shown)
        assert all(d["vendorOrgId"] == vendor for d in shown)
        # 회사 측의 작성·검증·도구는 보이지 않고, 보낸 메시지와 넘김만 보인다
        for data in shown:
            if (data["author"] or {}).get("side") == "company":
                assert data["kind"] in ("sent", "handoff"), data
            assert "codes" not in data
        sent = [d["message"] for d in shown if d["kind"] == "sent"]
        assert sent == next(iter(mine.values()))["messages"]
        assert {d["kind"] for d in shown} >= {"sent", "drafting", "validating"}
        others = {
            i["data"]["threadId"]
            for i in full
            if i["event"] == "activity" and i["data"]["threadId"] not in (None, *mine)
        }
        assert others and not others & {d["threadId"] for d in shown}
        assert all(i["data"]["status"] for i in items if i["event"] == "status")
    # 당사자가 아니면 진단 뷰와 같이 404
    other = client.get(f"/api/v3/sessions/{sid}/stream", headers=org(DAON))
    assert other.status_code == 404 and other.json()["error"]["code"] == "SESSION_NOT_FOUND"
    missing = client.get(f"/api/v3/sessions/{sid}/stream", headers={})
    assert missing.status_code == 401


def test_rejections_show_reasons_and_codes_only_to_admins(monkeypatch):
    """업체 측 첫 초안이 거부되면 거부 → 다시 쓰기 → 보냄이 보인다. 코드는 관리자에게만 나간다."""
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_auth", "header")
    state = make_app(only_for("vendor", SAEGIL, scripted(json.dumps(BAD_OFFER))))
    api.set_state(state)
    try:
        with TestClient(fastapi_app) as client:
            sid = new_session(client)
            to_approval_async(client, sid)
            thread = next(
                t
                for t in client.get(f"/api/v3/sessions/{sid}", headers=org(HANBIT)).json()[
                    "threads"
                ]
                if t["vendorOrgId"] == SAEGIL and t["status"] != "not_opened"
            )
            views = {who: stream(client, sid, org(who)) for who in (HANBIT, SAEGIL, ADMIN)}
    finally:
        api.set_state(None)
    for who, items in views.items():
        steps = [
            i["data"]
            for i in items
            if i["event"] == "activity"
            and i["data"]["threadId"] == thread["id"]
            and (i["data"]["author"] or {}).get("side") == "vendor"
            and i["data"]["kind"] in ("drafting", "rejected", "rewriting", "sent")
        ]
        kinds = [d["kind"] for d in steps]
        assert kinds[:4] == ["drafting", "rejected", "rewriting", "sent"], (who, kinds)
        rejected = steps[1]
        assert rejected["reasons"][0] == "근거에 없는 숫자" and "320" not in rejected["text"]
        assert ("codes" in rejected) is (who == ADMIN)
        if who == ADMIN:
            assert rejected["codes"][0] == "NUMBER_OUTSIDE_PLACEHOLDER"
            assert len(rejected["codes"]) == len(rejected["reasons"])
            assert all(d["message"] is None for d in steps if d["kind"] == "sent")
        else:
            assert steps[3]["message"]["validation"]["attempts"] == 2
        assert "NUMBER_OUTSIDE_PLACEHOLDER" not in json.dumps(
            [d["text"] for d in steps], ensure_ascii=False
        )


class ThreadedGateway:
    """업체 측이 다른 프로세스에 있을 때처럼: 다른 스레드에서 답해 실시간 알림이 닿지 않는다."""

    def __init__(self, app) -> None:
        self.app = app

    def deliver(self, vendor_org, message, context):
        out = {}

        def answer():
            out["reply"] = service.handle_vendor_message(self.app, vendor_org, message, context)

        worker = threading.Thread(target=answer)
        worker.start()
        worker.join()
        return out["reply"]


def test_remote_vendor_rejections_are_filled_from_its_report():
    app = make_app(only_for("vendor", SAEGIL, scripted(json.dumps(BAD_OFFER))))
    app.vendor_gateway = ThreadedGateway(app)
    sid = service.create_session(app, HANBIT, HANBIT)["session"]["id"]
    view = service.run_session(app, sid, HANBIT)
    for _ in range(4):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                service.answer_question(app, sid, question["id"], "unknown", HANBIT)
        view = service.run_session(app, sid, HANBIT)
    shown = service.stream_events(app, sid, ADMIN)
    assert shown["running"] is False and shown["status"] == view["session"]["status"]
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == SAEGIL)
    vendor = [
        e["data"]
        for e in shown["events"]
        if e["event"] == "activity"
        and e["data"]["threadId"] == thread["id"]
        and (e["data"]["author"] or {}).get("side") == "vendor"
    ]
    kinds = [d["kind"] for d in vendor if d["kind"] in ("drafting", "rejected", "rewriting")]
    assert kinds[:3] == ["drafting", "rejected", "rewriting"]
    assert (
        "NUMBER_OUTSIDE_PLACEHOLDER" in next(d for d in vendor if d["kind"] == "rejected")["codes"]
    )
    # 회사 측이 받은 답을 다시 확인하는 것도 보인다(업체 측 말풍선에, 회사 측이 한 일로)
    assert any(d["kind"] == "validating" and "회사 측" in d["text"] for d in vendor)


# ── 체험하기와 멈춘 자리 ─────────────────────────────────────────────
KEY = "test-bff-key-" + "k" * 24


def bff(user_id=None, org_id=None, **extra):
    headers = {"Authorization": f"Bearer {KEY}", **extra}
    if user_id:
        headers["X-User-Id"] = user_id
    if org_id:
        headers["X-Org-Id"] = org_id
    return headers


def test_demo_view_streams_demo_sessions_but_cannot_run(monkeypatch):
    from app.v3.account_store import InMemoryAccounts
    from app.v3.evidence.registry import InMemoryRegistry

    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_llm", "rule")
    monkeypatch.setattr(settings, "v3_auth", "bff")
    monkeypatch.setattr(settings, "v3_bff_key", KEY)
    state = service.App(
        InMemoryStore(),
        InMemoryRegistry(),
        auth_mode="bff",
        accounts=InMemoryAccounts(),
        today="2026-11-17",
    )
    api.set_state(state)
    try:
        with TestClient(fastapi_app) as client:
            assert client.post("/api/v3/demo/reset", headers=bff()).status_code == 200
            demo = bff(org_id=HANBIT, **{"X-Demo-View": "1"})
            listed = client.get("/api/v3/sessions", headers=demo).json()[0]  # 맨 앞이 시연 진단
            items = stream(client, listed["id"], demo)
            assert items[-1]["data"]["status"] == "awaiting_approval"
            assert any(i["event"] == "activity" and i["data"]["kind"] == "sent" for i in items)
            vendor = stream(client, listed["id"], bff(org_id=SAEGIL, **{"X-Demo-View": "1"}))
            assert all(
                i["data"]["vendorOrgId"] == SAEGIL for i in vendor if i["event"] == "activity"
            )
            for path in (
                f"/api/v3/sessions/{listed['id']}/run?mode=async",
                f"/api/v3/sessions/{listed['id']}/run",
            ):
                refused = client.post(path, headers=demo)
                assert refused.status_code == 403
                assert refused.json()["error"]["code"] == "DEMO_READ_ONLY"
            # 실제 조직의 진단은 체험하기로 스트림을 열 수 없다
            user = client.post(
                "/api/v3/accounts/signup",
                headers=bff(),
                json={"email": "live@example.com", "password": "가상-비밀번호-1234", "name": "가"},
            ).json()["user"]["id"]
            real = client.post(
                "/api/v3/orgs",
                headers=bff(user),
                json={"name": "(주)실시간가상", "kind": "emitter"},
            ).json()["id"]
            created = client.post("/api/v3/sessions", headers=bff(user, real), json={})
            real_sid = created.json()["session"]["id"]
            assert client.get(
                f"/api/v3/sessions/{real_sid}/stream", headers=bff(user, real)
            ).is_success
            hidden = client.get(f"/api/v3/sessions/{real_sid}/stream", headers=demo)
            assert hidden.status_code == 404
            own = client.get(
                f"/api/v3/sessions/{real_sid}/stream",
                headers=bff(org_id=real, **{"X-Demo-View": "1"}),
            )
            assert own.status_code == 403 and own.json()["error"]["code"] == "NOT_DEMO_ORG"
    finally:
        api.set_state(None)


def test_stale_running_lease_is_treated_as_idle_and_can_be_taken(world):
    client, state, _ = world
    sid = new_session(client)
    # 프로세스가 죽은 실행: 진단은 running으로 남았고 자리의 heartbeat는 오래전에 끊겼다
    session = state.store.get_session(sid)
    state.store.put_session({**session, "status": "running"})
    dead = {"runId": "run_dead", "mode": "async", "startedAt": "", "heartbeat": time.time() - 3600}
    assert state.store.claim_run(sid, dead, stale_before=0) is None
    view = client.get(f"/api/v3/sessions/{sid}", headers=org(HANBIT)).json()
    assert view["session"]["status"] == "idle"
    assert stream(client, sid, org(HANBIT))[-1]["data"]["status"] == "idle"
    started = client.post(f"/api/v3/sessions/{sid}/run?mode=async", headers=org(HANBIT))
    assert started.status_code == 202, started.text
    assert api.wait_runs(30)
    assert state.store.get_run(sid) is None
    events = state.store.list_events(sid)
    taken = [e for e in events if e["kind"] == "error" and e["detail"].get("reason") == "stale"]
    assert taken and taken[0]["detail"]["runId"] == "run_dead"
    assert state.store.get_session(sid)["status"] != "running"
    # 살아 있는 자리(방금 heartbeat)는 차지하지 못한다
    alive = {"runId": "run_other", "mode": "async", "startedAt": "", "heartbeat": time.time()}
    assert state.store.claim_run(sid, alive, stale_before=time.time() - 60) is None
    busy = client.post(f"/api/v3/sessions/{sid}/run?mode=async", headers=org(HANBIT))
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert (
        client.get(f"/api/v3/sessions/{sid}", headers=org(HANBIT)).json()["session"]["status"]
        == "running"
    )
    state.store.release_run(sid, "run_other")


def test_errors_in_the_background_run_are_recorded_and_the_session_stops(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_auth", "header")
    gate = Gate()
    state = make_app(lambda role, _o, _m: GatedRule(gate, boom=True) if role == "company" else None)
    api.set_state(state)
    try:
        with TestClient(fastapi_app) as client:
            sid = new_session(client)
            gate.armed = True
            run_async_until(client, sid, gate.entered.is_set)
            assert api.wait_runs(30)
            session = state.store.get_session(sid)
            assert session["status"] == "idle" and state.store.get_run(sid) is None
            errors = [e for e in state.store.list_events(sid) if e["kind"] == "error"]
            assert errors and errors[-1]["detail"]["error"] == "RuntimeError"
            assert "가상 장애" not in json.dumps(errors, ensure_ascii=False)
            items = stream(client, sid, org(HANBIT))
            assert items[-1]["data"]["status"] == "idle"
            notes = [i["data"]["text"] for i in items if i["event"] == "activity"]
            assert any("오류" in text for text in notes)
            # 동기 실행의 오류는 전처럼 500이고, 진단은 running으로 남지 않는다
            failed = client.post(f"/api/v3/sessions/{sid}/run", headers=org(HANBIT))
            assert failed.status_code == 500 and failed.json()["error"]["code"] == "INTERNAL"
            assert state.store.get_session(sid)["status"] == "idle"
            assert state.store.get_run(sid) is None
    finally:
        api.set_state(None)


# ── SQL: 기록은 하나씩 커밋된다 ──────────────────────────────────────
def test_sql_activity_is_committed_while_the_run_is_still_going(tmp_path):
    from sqlalchemy import create_engine

    from app.v3.sql import SqlRegistry, SqlStore, create_tables

    engine = create_engine(
        f"sqlite:///{tmp_path / 'v3.db'}", connect_args={"check_same_thread": False}
    )
    create_tables(engine)
    gate = Gate()
    app = service.App(
        SqlStore(engine),
        SqlRegistry(engine),
        llm_factory=lambda role, _o, _m: GatedRule(gate) if role == "company" else None,
        today="2026-11-17",
    )
    service.seed(app)
    sid = service.create_session(app, HANBIT, HANBIT)["session"]["id"]
    gate.armed = True
    worker = None
    try:
        for _ in range(6):
            started = service.start_run(app, sid, HANBIT)
            worker = threading.Thread(target=service.execute_run, args=(app, sid, started["runId"]))
            worker.start()
            wait_until(lambda w=worker: gate.entered.is_set() or not w.is_alive())
            if gate.entered.is_set():
                break
            worker.join()
            view = service.get_view(app, sid, HANBIT)
            for question in view["questions"]:
                if not question["answered"]:
                    service.answer_question(app, sid, question["id"], "unknown", HANBIT)
        assert gate.entered.is_set() and worker.is_alive()
        # 다른 연결(새 저장소 객체)로 읽어도 지금 실행의 기록이 이미 보인다
        reader = service.App(SqlStore(engine), SqlRegistry(engine), today="2026-11-17")
        shown = service.stream_events(reader, sid, HANBIT, started["after"])
        assert shown["running"] is True and shown["status"] == "running"
        kinds = [e["data"].get("kind") for e in shown["events"] if e["event"] == "activity"]
        assert kinds[-1] == "drafting"
        with pytest.raises(service.ServiceError) as busy:
            service.start_run(reader, sid, HANBIT)
        assert busy.value.code == "RUN_IN_PROGRESS" and busy.value.status == 409
    finally:
        gate.release.set()
        if worker is not None:
            worker.join(30)
    done = service.stream_events(reader, sid, HANBIT, shown["lastSeq"])
    assert done["running"] is False and done["status"] != "running"
    seqs = [e["id"] for e in done["events"]]
    assert seqs == sorted(set(seqs)) and seqs[0] > shown["lastSeq"]
    assert "sent" in [e["data"].get("kind") for e in done["events"]]
    engine.dispose()


# ── 로컬 서버(devserver)도 같은 두 경로를 준다 ───────────────────────
def test_devserver_runs_in_the_background_and_streams():
    import urllib.error
    import urllib.request

    from app.v3 import devserver

    app = devserver.build_app(today="2026-11-17")
    server = devserver.make_server(app, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"X-Org-Id": HANBIT, "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read().decode("utf-8")

    try:
        status, _, body = call("POST", "/sessions", {})
        sid = json.loads(body)["session"]["id"]
        for _ in range(6):
            status, _, body = call("POST", f"/sessions/{sid}/run?mode=async")
            assert status == 202 and json.loads(body)["status"] == "running", body
            wait_until(lambda: app.store.get_run(sid) is None)
            view = json.loads(call("GET", f"/sessions/{sid}")[2])
            if view["session"]["status"] != "awaiting_answers":
                break
            for question in view["questions"]:
                if not question["answered"]:
                    call(
                        "POST",
                        f"/sessions/{sid}/answers",
                        {"questionId": question["id"], "value": "unknown"},
                    )
        assert view["session"]["status"] == "awaiting_approval"
        status, headers, text = call("GET", f"/sessions/{sid}/stream")
        assert status == 200 and headers["Content-Type"].startswith("text/event-stream")
        assert headers["Cache-Control"] == "no-cache" and headers["X-Accel-Buffering"] == "no"
        items = sse(text.split("\n"))
        ids = [i["id"] for i in items if i["id"] is not None]
        assert ids == list(range(1, len(ids) + 1))
        assert items[-1]["data"] == {"status": "awaiting_approval", "lastSeq": ids[-1]}
        kinds = [i["data"]["kind"] for i in items if i["event"] == "activity"]
        assert {"drafting", "validating", "sent"} <= set(kinds)
        middle = ids[len(ids) // 2]
        resumed = sse(
            call("GET", f"/sessions/{sid}/stream", headers={"Last-Event-ID": str(middle)})[2].split(
                "\n"
            )
        )
        assert resumed == [i for i in items if i["id"] is None or i["id"] > middle]
        status, _, body = call("GET", f"/sessions/{sid}/stream?after=x")
        assert status == 422 and json.loads(body)["error"]["code"] == "INVALID_VALUE"
        status, _, body = call("GET", f"/sessions/{sid}/stream", headers={"X-Org-Id": DAON})
        assert status == 404
        status, _, body = call("POST", f"/sessions/{sid}/run?mode=later")
        assert status == 422 and json.loads(body)["error"]["code"] == "INVALID_VALUE"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
