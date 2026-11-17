"""업체 측 에이전트를 따로 띄운 서버(vendor_devserver.py)와 회사 측 서버(devserver.py)를 함께 돌린다.

두 서버를 스레드로 띄우고 HTTP로만 부른다. 같은 저장 파일 둘(저장소, 근거)을 본다.
수치는 가상이고, 서비스 키는 테스트용으로 지어낸 글이다.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app.v3 import devserver, localshare, service, vendor_devserver
from app.v3.agents.gateway import GatewayError

TODAY = "2026-11-18"
HANBIT, NAMHAE, SAEGIL = "org_hanbit", "org_namhae", "org_saegil"
KEY = "test-vendor-service"
JSON_HEADERS = {"Content-Type": "application/json"}


def call(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, headers={**JSON_HEADERS, **(headers or {})}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def serve(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return worker


def stop(server, worker):
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


@pytest.fixture
def pair(tmp_path):
    """(회사 측 앱, 회사 측 주소, 업체 측 상태, 업체 측 주소). 남해열분해만 따로 띄운다."""
    store_path = str(tmp_path / "demo.json")
    vendor_app = vendor_devserver.build_vendor_app(store_path, today=TODAY, env={})
    agent = vendor_devserver.VendorAgent(vendor_app, NAMHAE, KEY)
    vendor_server = vendor_devserver.make_server(agent, port=0)
    vendor_url = f"http://127.0.0.1:{vendor_server.server_address[1]}"
    env = {"V3_VENDOR_AGENTS": json.dumps({NAMHAE: vendor_url}), "VENDOR_SERVICE_KEY": KEY}
    app = devserver.build_app(store_path=store_path, today=TODAY, env=env, reset=True)
    server = devserver.make_server(app, port=0)
    workers = [(vendor_server, serve(vendor_server)), (server, serve(server))]
    try:
        yield (
            app,
            f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}",
            agent,
            vendor_url,
        )
    finally:
        for running, worker in workers:
            stop(running, worker)


def run_to_approval(base):
    """멈춤 지점마다 질문에 답하며(숫자와 "모름"을 섞어) 승인 대기까지 돌린다."""
    org = {"X-Org-Id": HANBIT}
    status, view = call("POST", f"{base}/sessions", {}, org)
    assert status == 200, view
    session_id = view["session"]["id"]
    for _ in range(8):
        status, view = call("POST", f"{base}/sessions/{session_id}/run", {}, org)
        assert status == 200, view
        waiting = [q for q in view["questions"] if not q["answered"]]
        if not waiting:
            break
        for index, question in enumerate(waiting):
            value = "unknown" if index % 2 else 1
            body = {"questionId": question["id"], "value": value}
            status, answered = call("POST", f"{base}/sessions/{session_id}/answers", body, org)
            assert status == 200, answered
    return view


def summary(view):
    result = view["result"]
    return (
        view["session"]["status"],
        sorted((a["streamId"], a["vendorOrgId"], a["status"]) for a in result["assignments"]),
        {key: (claim or {}).get("value") for key, claim in result["totals"].items()},
        sorted((t["vendorOrgId"], t["status"], len(t["messages"])) for t in view["threads"]),
    )


def test_one_vendor_answers_from_its_own_server(pair, tmp_path):
    app, base, agent, vendor_url = pair
    assert call("GET", f"{vendor_url}/healthz")[1] == {
        "status": "ok",
        "vendorOrgId": NAMHAE,
        "handled": 0,
    }
    view = run_to_approval(base)
    assert view["session"]["status"] == "awaiting_approval"
    threads = {t["vendorOrgId"]: t for t in view["threads"] if t["messages"]}
    namhae = threads[NAMHAE]
    from_vendor = [m for m in namhae["messages"] if m["from"] == NAMHAE]
    assert from_vendor and all(m["validation"]["ok"] for m in namhae["messages"])
    # 남해열분해의 답은 모두 따로 띄운 서버가 썼다. 나머지 업체는 회사 측 프로세스 안에서 답했다
    gateway = app.vendor_gateway
    assert isinstance(gateway, localshare.SplitVendorGateway)
    assert gateway.calls["remote"] == {NAMHAE: len(from_vendor)}
    assert gateway.calls["local"] and NAMHAE not in gateway.calls["local"]
    assert agent.handled == len(from_vendor)
    assert call("GET", f"{vendor_url}/healthz")[1]["handled"] == len(from_vendor)
    # 업체 측 프로세스가 남긴 계산 근거를 회사 측이 다시 읽어 확인했다(같은 근거 파일)
    calc = [
        evidence_id
        for message in from_vendor
        for claim in message["claims"]
        for evidence_id in claim["evidence"]
        if evidence_id.startswith("calc:")
    ]
    assert calc and all(app.registry.exists(evidence_id) for evidence_id in calc)
    # 같은 프로세스에서 모두 답하게 한 것과 결과가 같다
    alone = devserver.build_app(store_path=str(tmp_path / "alone.json"), today=TODAY, env={})
    server = devserver.make_server(alone, port=0)
    worker = serve(server)
    try:
        expected = run_to_approval(f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}")
    finally:
        stop(server, worker)
    assert summary(view) == summary(expected)


def test_vendor_server_checks_key_recipient_and_body(pair):
    _, base, agent, vendor_url = pair
    url = f"{vendor_url}/a2a/messages"
    message = {"threadId": "th_1", "from": HANBIT, "to": NAMHAE, "type": "inquiry", "claims": []}
    key = {"Authorization": f"Bearer {KEY}"}

    def code(response, status):
        assert response[0] == status, response
        assert set(response[1]["error"]) == {"code", "message"}
        return response[1]["error"]["code"]

    body = {"message": message, "context": {}}
    assert code(call("POST", url, body), 401) == "UNAUTHORIZED"
    assert code(call("POST", url, body, {"Authorization": "Bearer wrong"}), 401) == "UNAUTHORIZED"
    assert code(call("POST", url, body, {"Authorization": f"Basic {KEY}"}), 401) == "UNAUTHORIZED"
    other = {"message": {**message, "to": SAEGIL}, "context": {}}
    assert code(call("POST", url, other, key), 403) == "WRONG_RECIPIENT"
    for bad in ({}, {"message": "문의"}, {"message": message, "context": []}):
        assert code(call("POST", url, bad, key), 422) == "INVALID_BODY"
    assert code(call("GET", f"{vendor_url}/api/v3/orgs"), 404) == "NOT_FOUND"
    assert agent.handled == 0
    # 회사 측 API는 업체 측 서버에 없다. 업체 측 주소는 회사 측 서버에 없다
    assert code(call("POST", f"{base}/a2a/messages", body, key), 404) == "NOT_FOUND"


def test_vendor_server_refuses_everything_without_a_key_or_store(tmp_path):
    app = vendor_devserver.build_vendor_app(str(tmp_path / "none.json"), today=TODAY, env={})
    message = {"from": HANBIT, "to": NAMHAE, "type": "inquiry"}
    body = json.dumps({"message": message, "context": {}}).encode()
    headers = {"Authorization": f"Bearer {KEY}"}
    request = devserver.Request("POST", "/a2a/messages", {}, headers, body)
    unset = vendor_devserver.VendorAgent(app, NAMHAE, "")
    with pytest.raises(service.ServiceError) as error:
        unset.route(app, request)
    assert (error.value.code, error.value.status) == ("SERVICE_KEY_NOT_SET", 503)
    nobody = vendor_devserver.VendorAgent(app, "", KEY)
    with pytest.raises(service.ServiceError) as error:
        nobody.route(app, request)
    assert (error.value.code, error.value.status) == ("VENDOR_NOT_SET", 503)
    early = vendor_devserver.VendorAgent(app, NAMHAE, KEY)  # 회사 측 서버가 아직 파일을 쓰지 않았다
    with pytest.raises(service.ServiceError) as error:
        early.route(app, request)
    assert (error.value.code, error.value.status) == ("STORE_NOT_READY", 503)
    assert early.handled == 0 and KEY not in repr(early)


def test_vendor_memo_is_dropped_when_a_thread_starts_again(tmp_path):
    """회사 측이 시연 데이터를 되돌리면 대화 번호가 다시 시작한다. 지난 대화의 메모를 쓰지 않는다."""
    store_path = str(tmp_path / "demo.json")
    devserver.build_app(store_path=store_path, today=TODAY, env={})  # 저장 파일을 만든다
    app = vendor_devserver.build_vendor_app(store_path, today=TODAY, env={})
    agent = vendor_devserver.VendorAgent(app, NAMHAE, KEY)
    app.vendor_memo["th_3"] = [{"name": "judge_route", "arguments": {}, "result": {}}]
    app.vendor_memo["th_9"] = [{"name": "kept", "arguments": {}, "result": {}}]
    inquiry = {"threadId": "th_3", "from": HANBIT, "to": NAMHAE, "type": "inquiry"}
    going = {"thread": {"id": "th_3", "messages": [inquiry, {"from": NAMHAE, "type": "question"}]}}
    agent._refresh(inquiry, going)
    assert "th_3" in app.vendor_memo  # 이어지는 대화의 메모는 둔다
    agent._refresh(inquiry, {"thread": {"id": "th_3", "messages": [inquiry]}})
    assert "th_3" not in app.vendor_memo and "th_9" in app.vendor_memo


def test_reload_reads_what_the_other_process_wrote(tmp_path):
    store_path = str(tmp_path / "demo.json")
    writer = devserver.build_app(store_path=store_path, today=TODAY, env={})
    reader = vendor_devserver.build_vendor_app(store_path, today=TODAY, env={})
    assert reader.store.list_orgs() and reader.registry.list()  # 만들 때 한 번 읽는다
    before = len(reader.registry.list())
    session = service.create_session(writer, HANBIT, HANBIT)["session"]["id"]
    service.run_session(writer, session, HANBIT)
    assert len(reader.registry.list()) == before  # 다시 읽기 전에는 모른다
    assert reader.store.reload() and reader.registry.reload()
    assert len(reader.registry.list()) == len(writer.registry.list()) > before
    assert reader.store.snapshot() == writer.store.snapshot()
    empty = localshare.ReloadingStore(tmp_path / "missing.json")
    assert empty.reload() is False and empty.list_orgs() == []


def test_gateway_settings_are_checked_before_the_server_starts(tmp_path):
    agents = json.dumps({NAMHAE: "http://127.0.0.1:1"})
    store_path = str(tmp_path / "demo.json")
    with pytest.raises(SystemExit, match="VENDOR_SERVICE_KEY"):
        devserver.build_app(store_path=store_path, today=TODAY, env={"V3_VENDOR_AGENTS": agents})
    with pytest.raises(
        SystemExit, match="--store"
    ):  # 메모리 저장으로는 두 프로세스가 나눠 볼 수 없다
        devserver.build_app(
            today=TODAY, env={"V3_VENDOR_AGENTS": agents, "VENDOR_SERVICE_KEY": KEY}
        )
    for wrong in ("[", "[]", json.dumps({NAMHAE: "ftp://x"}), json.dumps({"": "http://x"})):
        with pytest.raises(SystemExit, match="V3_VENDOR_AGENTS"):
            devserver.build_app(store_path=store_path, today=TODAY, env={"V3_VENDOR_AGENTS": wrong})
    assert localshare.vendor_agents("") == {} and localshare.vendor_agents(None) == {}
    plain = devserver.build_app(store_path=store_path, today=TODAY, env={})
    assert type(plain.vendor_gateway).__name__ == "LocalVendorGateway"
    for argv, needed in (
        (["--store", store_path], "VENDOR_ORG_ID"),
        (["--store", store_path, "--vendor", NAMHAE], "VENDOR_SERVICE_KEY"),
    ):
        with pytest.raises(SystemExit, match=needed):
            vendor_devserver.main(argv)


def test_unreachable_vendor_is_handed_to_a_person_not_guessed(tmp_path):
    """따로 띄운 업체 측 에이전트에 닿지 못하면 그 대화만 담당자에게 넘어간다."""
    env = {
        "V3_VENDOR_AGENTS": json.dumps({NAMHAE: "http://127.0.0.1:9"}),
        "VENDOR_SERVICE_KEY": KEY,
    }
    app = devserver.build_app(store_path=str(tmp_path / "demo.json"), today=TODAY, env=env)

    def refused(method, url, headers, body, timeout):
        raise ConnectionRefusedError

    app.vendor_gateway._remote._transport = refused
    with pytest.raises(GatewayError):
        app.vendor_gateway.deliver(NAMHAE, {"to": NAMHAE}, {})
    session = service.create_session(app, HANBIT, HANBIT)["session"]["id"]
    view = service.run_session(app, session, HANBIT)
    for _ in range(6):
        waiting = [q for q in view["questions"] if not q["answered"]]
        if not waiting:
            break
        for question in waiting:
            service.answer_question(app, session, question["id"], "unknown", HANBIT)
        view = service.run_session(app, session, HANBIT)
    states = {t["vendorOrgId"]: t["status"] for t in view["threads"] if t["messages"]}
    assert states[NAMHAE] == "handoff" and states[SAEGIL] != "handoff"
