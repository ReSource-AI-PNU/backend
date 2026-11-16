"""HTTP 층의 조직 식별: header 모드(시연 전용)와 token 모드(조직별 서비스 토큰). 수치는 가상이다.

token 모드에서는 X-Org-Id를 바꿔도 다른 조직의 뷰를 받지 못하고, GET /orgs는 자기 조직과 대화
상대만 준다. 아래 토큰은 테스트용으로 지어낸 글이다(실제 값이 아니다).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.v3 import devserver, httpio, service
from tests.v3.runtime_fixtures import (
    ADMIN,
    DAON,
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    TODAY,
    make_app,
    to_approval,
)

TOKENS = {
    HANBIT: "test-hanbit-" + "a" * 12,
    DAON: "test-daon-" + "b" * 14,
    SAEGIL: "test-saegil-" + "c" * 12,
    ADMIN: "test-admin-" + "d" * 13,
}
ROOT = Path(__file__).resolve().parents[2]


def call(base, method, path, headers=None, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def bearer(org):
    return {"Authorization": f"Bearer {TOKENS[org]}"}


@pytest.fixture
def token_server():
    app = devserver.build_app(today=TODAY, env={}, demo_open=False)
    auth = httpio.make_auth("token", TOKENS)
    server = devserver.make_server(app, "127.0.0.1", 0, auth=auth)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield app, f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_token_mode_needs_a_token_and_ignores_the_org_header(token_server):
    _app, base = token_server
    assert call(base, "GET", "/health") == (200, {"ok": True})
    assert call(base, "GET", "/health", {"Authorization": "Bearer wrong"}) == (200, {"ok": True})
    path = f"/orgs/{HANBIT}/streams"
    # 헤더만으로는 아무 조직도 될 수 없다
    status, body = call(base, "GET", path, {"X-Org-Id": HANBIT})
    assert status == 401 and body["error"]["code"] == "TOKEN_REQUIRED"
    status, body = call(base, "GET", path, {"Authorization": "Bearer " + "z" * 24})
    assert status == 401 and body["error"]["code"] == "INVALID_TOKEN"
    status, body = call(base, "GET", path, {"Authorization": "Basic abc"})
    assert status == 401 and body["error"]["code"] == "TOKEN_REQUIRED"
    status, streams = call(base, "GET", path, bearer(HANBIT))
    assert status == 200 and {s["id"] for s in streams} >= {"st_sprue", "st_film"}
    # 다온의 토큰에 한빛의 조직 헤더를 붙여도 다온이다
    status, body = call(base, "GET", path, {**bearer(DAON), "X-Org-Id": HANBIT})
    assert status == 403 and body["error"]["code"] == "FORBIDDEN"
    # 관리자 헤더를 붙여도 관리자가 되지 않는다
    status, body = call(base, "GET", "/admin/status", {**bearer(HANBIT), "X-Org-Id": ADMIN})
    assert status == 403 and body["error"]["code"] == "FORBIDDEN"
    status, body = call(base, "GET", "/admin/status", bearer(ADMIN))
    assert status == 200 and body["auth"] == {"mode": "token", "demoOnly": False}
    # 시연 되돌리기도 토큰의 조직으로 판단한다
    status, body = call(base, "POST", "/demo/reset", {"X-Org-Id": ADMIN})
    assert status == 401 and body["error"]["code"] == "ORG_REQUIRED"
    assert call(base, "POST", "/demo/reset", bearer(HANBIT))[0] == 403
    assert call(base, "POST", "/demo/reset", bearer(ADMIN))[0] == 200


def test_token_mode_lists_only_self_and_counterparts(token_server):
    _app, base = token_server
    status, body = call(base, "GET", "/orgs")
    assert status == 401 and body["error"]["code"] == "TOKEN_REQUIRED"
    status, body = call(base, "GET", "/orgs", {"X-Org-Id": HANBIT})
    assert status == 401
    # 진단을 하기 전에는 자기 조직뿐이다
    for org in (HANBIT, DAON, SAEGIL):
        status, orgs = call(base, "GET", "/orgs", bearer(org))
        assert status == 200 and [o["id"] for o in orgs] == [org]
    _, view = call(base, "POST", "/sessions", bearer(HANBIT), {})
    session = f"/sessions/{view['session']['id']}"
    for _ in range(4):
        _, view = call(base, "POST", f"{session}/run", bearer(HANBIT))
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                body = {"questionId": question["id"], "value": "unknown"}
                call(base, "POST", f"{session}/answers", bearer(HANBIT), body)
    assert view["session"]["status"] == "awaiting_approval"
    _, orgs = call(base, "GET", "/orgs", bearer(HANBIT))
    assert {o["id"] for o in orgs} == {HANBIT, SAEGIL, NAMHAE, MISO}  # 다른 배출 기업은 없다
    _, orgs = call(base, "GET", "/orgs", bearer(SAEGIL))
    assert {o["id"] for o in orgs} == {SAEGIL, HANBIT}  # 다른 업체도 없다
    _, orgs = call(base, "GET", "/orgs", bearer(DAON))
    assert [o["id"] for o in orgs] == [DAON]
    _, orgs = call(base, "GET", "/orgs", bearer(ADMIN))
    assert {HANBIT, DAON, SAEGIL, NAMHAE, MISO, ADMIN} <= {o["id"] for o in orgs}
    # 뷰도 토큰의 조직 것이다: 다온의 토큰으로는 한빛의 진단이 없는 것으로 보인다
    status, body = call(base, "GET", session, {**bearer(DAON), "X-Org-Id": HANBIT})
    assert status == 404 and body["error"]["code"] == "SESSION_NOT_FOUND"
    status, theirs = call(base, "GET", session, bearer(SAEGIL))
    assert status == 200 and theirs["viewer"] == {"orgId": SAEGIL, "role": "vendor"}


def test_header_mode_is_the_default_and_says_it_is_for_demos_only():
    app = devserver.build_app(today=TODAY, env={})
    server = devserver.make_server(app, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"
    try:
        status, orgs = call(base, "GET", "/orgs")
        assert status == 200 and {HANBIT, DAON, ADMIN} <= {o["id"] for o in orgs}
        status, body = call(base, "GET", "/admin/status", {"X-Org-Id": ADMIN})
        assert status == 200 and body["auth"] == {"mode": "header", "demoOnly": True}
        # header 모드에서는 토큰을 보지 않는다
        status, _ = call(base, "GET", f"/orgs/{HANBIT}/streams", bearer(HANBIT))
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_service_list_orgs_scope():
    app = make_app()
    everyone = [o["id"] for o in service.list_orgs(app)]
    assert everyone[0] == HANBIT and everyone[-1] == ADMIN
    assert [o["id"] for o in service.list_orgs(app, HANBIT, True)] == [HANBIT]
    to_approval(app)
    assert {o["id"] for o in service.list_orgs(app, HANBIT, True)} == {HANBIT, SAEGIL, NAMHAE, MISO}
    assert {o["id"] for o in service.list_orgs(app, MISO, True)} == {MISO, HANBIT}
    assert [o["id"] for o in service.list_orgs(app, DAON, True)] == [DAON]
    assert [o["id"] for o in service.list_orgs(app, ADMIN, True)] == everyone
    for viewer, code in ((None, "ORG_REQUIRED"), ("org_없음", "UNKNOWN_ORG")):
        with pytest.raises(service.ServiceError) as caught:
            service.list_orgs(app, viewer, True)
        assert caught.value.code == code and caught.value.status == 401


def test_devserver_options(tmp_path, capsys):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps(TOKENS), encoding="utf-8")
    auth = devserver.load_auth("token", str(tokens))
    assert auth.mode == "token" and auth.viewer(bearer(SAEGIL)) == SAEGIL
    assert devserver.load_auth("header", None).mode == "header"
    assert (
        devserver.load_auth("header", str(tokens)).tokens == ()
    )  # header 모드는 파일을 읽지 않는다
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({HANBIT: "short"}), encoding="utf-8")
    for mode, path in (
        ("token", None),
        ("token", str(tmp_path / "없음.json")),
        ("token", str(bad)),
    ):
        with pytest.raises(SystemExit) as caught:
            devserver.load_auth(mode, path)
        assert TOKENS[HANBIT] not in str(caught.value)
    with pytest.raises(SystemExit):  # 모르는 모드는 인자 단계에서 거부한다
        devserver.main(["--auth", "entra"])
    capsys.readouterr()
    assert "Authorization" in devserver.ALLOWED_HEADERS  # 브라우저에서 토큰을 보낼 수 있다


def test_docs_say_header_mode_is_for_demos_only():
    sentence = "header 모드는 시연 전용이며 공개 주소에 그대로 두면 조직 경계가 지켜지지 않는다"
    assert sentence == httpio.HEADER_MODE_WARNING
    for name in ("README.md", "docs/v3/CONTRACT.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert sentence in text, name
        assert "V3_AUTH" in text and "V3_ORG_TOKENS" in text, name
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "V3_AUTH=header" in example and "\nV3_ORG_TOKENS=\n" in example
