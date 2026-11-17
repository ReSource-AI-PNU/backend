"""V3_AUTH=bff: 계정, 조직, 소속, 초대, 체험하기. 메모리 저장과 규칙 정책으로 돈다.

이메일, 이름, 키, 조직, 수치는 모두 지어낸 값이다(실제 값이 아니다).
"""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app as fastapi_app
from app.v3 import accounts, api, service
from app.v3.account_store import InMemoryAccounts
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore

KEY = "test-bff-key-" + "k" * 24
HANBIT, SAEGIL, NAMHAE, MISO = "org_hanbit", "org_saegil", "org_namhae", "org_miso"
ADMIN = "org_admin"
PASSWORD = "가상-비밀번호-1234"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_llm", "rule")
    monkeypatch.setattr(settings, "v3_auth", "bff")
    monkeypatch.setattr(settings, "v3_bff_key", KEY)
    clock = Clock()
    state = service.App(
        InMemoryStore(),
        InMemoryRegistry(),
        auth_mode="bff",
        accounts=InMemoryAccounts(clock),
        today="2026-11-17",
    )
    api.set_state(state)
    with TestClient(fastapi_app) as client:
        first = client.post("/api/v3/demo/reset", headers=bff())  # 빈 저장소의 첫 시드
        assert first.status_code == 200, first.text
        yield client, state, clock
    api.set_state(None)


def bff(user_id=None, org_id=None, **extra):
    headers = {"Authorization": f"Bearer {KEY}", **extra}
    if user_id:
        headers["X-User-Id"] = user_id
    if org_id:
        headers["X-Org-Id"] = org_id
    return headers


def demo(org_id):
    return bff(org_id=org_id, **{"X-Demo-View": "1"})


def error(response, status):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    return body["error"]["code"]


def signup(client, email, name="가상 담당자"):
    response = client.post(
        "/api/v3/accounts/signup",
        headers=bff(),
        json={"email": email, "password": PASSWORD, "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["user"]["id"]


def new_org(client, user_id, name, kind, **fields):
    response = client.post(
        "/api/v3/orgs", headers=bff(user_id), json={"name": name, "kind": kind, **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


# ── 계정 ─────────────────────────────────────────────────────────────
def test_signup_login_and_me(world):
    client, state, _ = world
    created = client.post(
        "/api/v3/accounts/signup",
        headers=bff(),
        json={"email": "  Kim@Example.COM ", "password": PASSWORD, "name": " 김가상 "},
    )
    assert created.status_code == 201, created.text
    user = created.json()["user"]
    assert user["email"] == "kim@example.com" and user["name"] == "김가상"
    assert set(user) == {"id", "email", "name"} and user["id"].startswith("usr_")
    stored = state.accounts.get_user(user["id"])["passwordHash"]
    assert stored.startswith("scrypt$16384$8$1$") and PASSWORD not in stored
    assert PASSWORD not in created.text

    login = client.post(
        "/api/v3/accounts/login",
        headers=bff(),
        json={"email": "KIM@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert login.json() == {"user": user, "memberships": []}
    org = new_org(client, user["id"], "(주)가상정밀", "emitter", bizNo="1234567890")
    assert org["kind"] == "emitter" and org["memberRole"] == "owner" and org["demo"] is False
    assert org["bizNo"] == "123-45-67890" and org["id"].startswith("org_")
    me = client.get("/api/v3/accounts/me", headers=bff(user["id"]))
    assert me.status_code == 200, me.text
    assert me.json()["memberships"] == [
        {"orgId": org["id"], "orgName": "(주)가상정밀", "kind": "emitter", "memberRole": "owner"}
    ]
    assert "passwordHash" not in me.text


def test_signup_rejects_duplicates_and_bad_values(world):
    client, _, _ = world
    signup(client, "dup@example.com")
    again = client.post(
        "/api/v3/accounts/signup",
        headers=bff(),
        json={"email": "DUP@example.com ", "password": PASSWORD, "name": "다른 사람"},
    )
    assert error(again, 409) == "EMAIL_TAKEN"
    for body in (
        {"email": "short@example.com", "password": "1234567", "name": "가"},
        {"email": "not-an-email", "password": PASSWORD, "name": "가"},
        {"email": "noname@example.com", "password": PASSWORD, "name": " "},
    ):
        assert error(client.post("/api/v3/accounts/signup", headers=bff(), json=body), 422) == (
            "INVALID_VALUE"
        )


def test_wrong_password_and_unknown_email_get_the_same_error(world):
    client, _, _ = world
    signup(client, "lee@example.com")
    wrong = client.post(
        "/api/v3/accounts/login",
        headers=bff(),
        json={"email": "lee@example.com", "password": "틀린-비밀번호-0000"},
    )
    unknown = client.post(
        "/api/v3/accounts/login",
        headers=bff(),
        json={"email": "nobody@example.com", "password": PASSWORD},
    )
    assert error(wrong, 401) == error(unknown, 401) == "INVALID_CREDENTIALS"
    assert wrong.json() == unknown.json()


def test_too_many_failed_logins_are_refused(world):
    client, _, _ = world
    signup(client, "park@example.com")
    body = {"email": "park@example.com", "password": "틀린-비밀번호-0000"}
    for _ in range(10):
        assert error(client.post("/api/v3/accounts/login", headers=bff(), json=body), 401)
    right = {"email": "park@example.com", "password": PASSWORD}
    blocked = client.post("/api/v3/accounts/login", headers=bff(), json=right)
    assert error(blocked, 429) == "TOO_MANY_ATTEMPTS"
    # 다른 이메일은 막히지 않는다
    other = {"email": "other@example.com", "password": PASSWORD}
    assert error(client.post("/api/v3/accounts/login", headers=bff(), json=other), 401)


# ── BFF 키와 소속 ────────────────────────────────────────────────────
def test_bff_key_is_required_everywhere_but_health(world, monkeypatch):
    client, _, _ = world
    user = signup(client, "choi@example.com")
    org = new_org(client, user, "(주)가상화학", "emitter")["id"]
    path = f"/api/v3/orgs/{org}/streams"
    headers = {"X-User-Id": user, "X-Org-Id": org}
    assert error(client.get(path, headers=headers), 401) == "INVALID_BFF_KEY"
    wrong = {**headers, "Authorization": "Bearer " + "z" * 37}
    assert error(client.get(path, headers=wrong), 401) == "INVALID_BFF_KEY"
    assert client.get(path, headers=bff(user, org)).status_code == 200
    for url in ("/api/v3/demo/orgs", "/api/v3/intake/csv/template", "/api/v3/orgs"):
        assert error(client.get(url), 401) == "INVALID_BFF_KEY"
    login = {"email": "choi@example.com", "password": PASSWORD}
    assert error(client.post("/api/v3/accounts/login", json=login), 401) == "INVALID_BFF_KEY"
    assert client.get("/api/v3/health").json() == {"ok": True}
    # 키가 설정되지 않았으면 모든 요청을 거절한다
    monkeypatch.setattr(settings, "v3_bff_key", "")
    assert error(client.get(path, headers=bff(user, org)), 503) == "BFF_KEY_NOT_SET"
    assert error(client.post("/api/v3/accounts/login", json=login), 503) == "BFF_KEY_NOT_SET"


def test_user_must_be_a_member_of_the_org(world):
    client, _, _ = world
    alice = signup(client, "alice@example.com")
    bob = signup(client, "bob@example.com")
    alice_org = new_org(client, alice, "(주)가상A", "emitter")["id"]
    bob_org = new_org(client, bob, "(주)가상B", "emitter")["id"]
    path = f"/api/v3/orgs/{alice_org}/streams"
    assert error(client.get(path, headers=bff(bob, alice_org)), 403) == "NOT_A_MEMBER"
    # 자기 조직으로 들어와 다른 조직의 경로를 불러도 서비스의 조직 경계에 걸린다
    assert error(client.get(path, headers=bff(bob, bob_org)), 403) == "FORBIDDEN"
    assert error(client.get(path, headers=bff(org_id=alice_org)), 401) == "USER_REQUIRED"
    assert error(client.get(path, headers=bff("usr_missing", alice_org)), 401) == "UNKNOWN_USER"
    assert error(client.get(path, headers=bff(alice)), 401) == "ORG_REQUIRED"
    # 실제 사용자가 시연 조직이나 운영 조직을 X-Org-Id로 대도 구성원이 아니다
    for org in (HANBIT, ADMIN):
        streams = client.get(f"/api/v3/orgs/{org}/streams", headers=bff(alice, org))
        assert error(streams, 403) == "NOT_A_MEMBER"
    members = client.get(f"/api/v3/orgs/{alice_org}/members", headers=bff(bob))
    assert error(members, 403) == "NOT_A_MEMBER"
    assert error(client.get("/api/v3/admin/status", headers=bff(alice, alice_org)), 403) == (
        "FORBIDDEN"
    )
    # 조직 목록은 자기 조직과 대화 상대만
    listed = client.get("/api/v3/orgs", headers=bff(alice, alice_org)).json()
    assert [org["id"] for org in listed] == [alice_org]


# ── 초대 ─────────────────────────────────────────────────────────────
def test_invite_create_accept_and_expire(world):
    client, _, clock = world
    owner = signup(client, "owner@example.com", "대표")
    member = signup(client, "member@example.com", "담당")
    late = signup(client, "late@example.com", "늦은 담당")
    org = new_org(client, owner, "(주)가상자원", "vendor")["id"]

    invite = client.post(f"/api/v3/orgs/{org}/invites", headers=bff(owner))
    assert invite.status_code == 201, invite.text
    token = invite.json()["token"]
    expires = datetime.fromisoformat(invite.json()["expiresAt"])
    assert expires == clock.now + timedelta(days=7) and len(token) >= 40

    # 초대 화면(로그인 전)은 BFF 키만으로 초대한 조직을 본다. 조직 ID나 만든 사람은 싣지 않는다
    peek = client.get(f"/api/v3/invites/{token}", headers=bff())
    assert peek.status_code == 200, peek.text
    assert peek.json() == {
        "orgName": "(주)가상자원",
        "kind": "vendor",
        "expiresAt": invite.json()["expiresAt"],
        "valid": True,
    }
    assert client.get(f"/api/v3/invites/{token}").status_code == 401  # BFF 키가 있어야 한다
    assert error(client.get("/api/v3/invites/없는토큰", headers=bff()), 410) == "INVITE_INVALID"
    accepted = client.post(f"/api/v3/invites/{token}/accept", headers=bff(member))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {
        "orgId": org,
        "orgName": "(주)가상자원",
        "kind": "vendor",
        "memberRole": "member",
    }
    used = client.post(f"/api/v3/invites/{token}/accept", headers=bff(late))
    assert error(used, 410) == "INVITE_INVALID"
    assert error(client.get(f"/api/v3/invites/{token}", headers=bff()), 410) == "INVITE_INVALID"
    assert error(client.post("/api/v3/invites/없는토큰/accept", headers=bff(late)), 410) == (
        "INVITE_INVALID"
    )
    # 구성원은 초대할 수 없고, 구성원 목록은 볼 수 있다
    by_member = client.post(f"/api/v3/orgs/{org}/invites", headers=bff(member))
    assert error(by_member, 403) == "OWNER_ONLY"
    members = client.get(f"/api/v3/orgs/{org}/members", headers=bff(member)).json()
    assert {(m["email"], m["memberRole"]) for m in members} == {
        ("owner@example.com", "owner"),
        ("member@example.com", "member"),
    }
    assert all(set(m) == {"userId", "name", "email", "memberRole"} for m in members)
    # 7일이 지나면 만료된다
    expiring = client.post(f"/api/v3/orgs/{org}/invites", headers=bff(owner)).json()["token"]
    assert client.get(f"/api/v3/invites/{expiring}", headers=bff()).status_code == 200
    clock.now += timedelta(days=7, seconds=1)
    assert error(client.get(f"/api/v3/invites/{expiring}", headers=bff()), 410) == "INVITE_INVALID"
    expired = client.post(f"/api/v3/invites/{expiring}/accept", headers=bff(late))
    assert error(expired, 410) == "INVITE_INVALID"
    assert client.get(f"/api/v3/orgs/{org}/streams", headers=bff(late, org)).status_code == 403


# ── 체험하기 ─────────────────────────────────────────────────────────
def test_demo_view_reads_demo_orgs_only_and_only_with_get(world):
    client, _, _ = world
    listed = client.get("/api/v3/demo/orgs", headers=bff())
    assert listed.status_code == 200, listed.text
    assert listed.json() == [
        {"id": HANBIT, "name": "(주)한빛정밀", "kind": "emitter"},
        {"id": "org_daon", "name": "(주)다온산업", "kind": "emitter"},
        {"id": SAEGIL, "name": "(주)새길자원", "kind": "vendor"},
        {"id": NAMHAE, "name": "(주)남해열분해", "kind": "vendor"},
        {"id": MISO, "name": "(주)미소환경", "kind": "vendor"},
    ]
    streams = client.get(f"/api/v3/orgs/{HANBIT}/streams", headers=demo(HANBIT))
    assert streams.status_code == 200 and len(streams.json()) == 4
    sessions = client.get("/api/v3/sessions", headers=demo(HANBIT)).json()
    assert len(sessions) == 1
    view = client.get(f"/api/v3/sessions/{sessions[0]['id']}", headers=demo(HANBIT))
    assert view.status_code == 200 and view.json()["session"]["status"] == "awaiting_approval"
    assert client.get("/api/v3/sessions", headers=demo(SAEGIL)).status_code == 200
    # 쓰는 요청은 모두 막는다
    sid = sessions[0]["id"]
    for method, path, body in (
        ("POST", "/api/v3/sessions", {}),
        ("POST", f"/api/v3/sessions/{sid}/run", {}),
        ("POST", f"/api/v3/sessions/{sid}/approvals", {"threadId": "th_1"}),
        ("PUT", f"/api/v3/orgs/{HANBIT}/streams/st_sprue", {"monthlyKg": 1}),
        ("POST", "/api/v3/demo/reset", {}),
    ):
        response = client.request(method, path, headers=demo(HANBIT), json=body)
        assert error(response, 403) == "DEMO_READ_ONLY", (method, path)
    # 시연 조직이 아니면(운영 조직, 없는 조직) 체험하기로 볼 수 없다
    for org in (ADMIN, "org_missing"):
        response = client.get(f"/api/v3/orgs/{org}/streams", headers=demo(org))
        assert error(response, 403) == "NOT_DEMO_ORG"
    assert error(client.get("/api/v3/admin/status", headers=demo(HANBIT)), 403) == "FORBIDDEN"
    assert error(client.get("/api/v3/sessions", headers=bff(**{"X-Demo-View": "1"})), 401) == (
        "ORG_REQUIRED"
    )


def test_real_orgs_are_never_readable_through_the_demo_view(world):
    client, _, _ = world
    user = signup(client, "real@example.com")
    org = new_org(client, user, "(주)실제가상", "emitter")["id"]
    assert error(client.get(f"/api/v3/orgs/{org}/streams", headers=demo(org)), 403) == (
        "NOT_DEMO_ORG"
    )
    # 시연 조직으로 들어와 실제 조직의 경로를 불러도 보이지 않는다
    streams = client.get(f"/api/v3/orgs/{org}/streams", headers=demo(HANBIT))
    assert streams.status_code in (403, 404)
    orgs = client.get("/api/v3/orgs", headers=demo(HANBIT)).json()
    assert org not in {item["id"] for item in orgs}


# ── 새 조직의 진단 ───────────────────────────────────────────────────
def answer_until_settled(client, session_id, headers):
    view = client.post(f"/api/v3/sessions/{session_id}/run", headers=headers).json()
    for _ in range(4):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                answered = client.post(
                    f"/api/v3/sessions/{session_id}/answers",
                    headers=headers,
                    json={"questionId": question["id"], "value": "unknown"},
                )
                assert answered.status_code == 200, answered.text
        view = client.post(f"/api/v3/sessions/{session_id}/run", headers=headers).json()
    return view


def make_new_vendor(client):
    """가입한 업체: PP 허가와 매입 기준을 채운다(가상 값)."""
    user = signup(client, "vendor@example.com", "업체 담당")
    org = new_org(
        client, user, "(주)가상리사이클", "vendor", region="부산 사하구", location=[128.97, 35.09]
    )["id"]
    empty = client.get(f"/api/v3/vendors/{org}", headers=bff(user, org))
    assert empty.status_code == 200 and empty.json()["permits"] == []
    profile = {
        "permits": [{"wasteItem": "폐플라스틱", "materials": ["PP"]}],
        "criteria": [
            {
                "id": "pc_new_pp_b",
                "material": "PP",
                "form": None,
                "grade": "B",
                "maxContaminationPct": 10,
                "maxMoisturePct": 3,
                "unitPriceKrwPerKg": 210,
                "minMonthlyKg": 100,
            }
        ],
        "fleet": {"depot": [128.97, 35.09], "capacityKg": 3000, "capacityM3": 15, "costPerKm": 900},
        "guardrails": {"floorPriceKrwPerKg": 100, "ceilingPriceKrwPerKg": 300},
    }
    saved = client.put(f"/api/v3/vendors/{org}", headers=bff(user, org), json=profile)
    assert saved.status_code == 200, saved.text
    return user, org


def test_new_emitter_discovers_seed_vendors_and_a_signed_up_vendor(world):
    client, _, _ = world
    empty_user = signup(client, "empty-vendor@example.com")
    empty_vendor = new_org(client, empty_user, "(주)빈프로필", "vendor")["id"]
    vendor_user, vendor = make_new_vendor(client)
    user = signup(client, "emitter@example.com", "배출 담당")
    org = new_org(
        client, user, "(주)가상사출", "emitter", region="부산 강서구", location=[128.83, 35.09]
    )["id"]
    headers = bff(user, org)
    assert client.get(f"/api/v3/orgs/{org}/streams", headers=headers).json() == []
    stream_id = f"st_{org[4:]}_runner"
    put = client.put(
        f"/api/v3/orgs/{org}/streams/{stream_id}",
        headers=headers,
        json={
            "name": "런너",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "가공 전",
            "monthlyKg": 800,
            "bulkDensityKgPerM3": 250,
        },
    )
    assert put.status_code == 200, put.text
    created = client.post("/api/v3/sessions", headers=headers, json={})
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]
    view = answer_until_settled(client, session_id, headers)
    vendors = {thread["vendorOrgId"] for thread in view["threads"]}
    assert {SAEGIL, NAMHAE, MISO, vendor} <= vendors
    assert empty_vendor not in vendors  # 허가 품목도 매입 기준도 없는 업체는 탐색에 나오지 않는다
    opened = next(t for t in view["threads"] if t["vendorOrgId"] == vendor)
    assert opened["status"] != "not_opened", opened["statusReason"]

    # 대화 상대가 된 업체는 그 진단을 보고, 배출 기업은 그 업체의 공개 프로필을 본다
    as_vendor = client.get(f"/api/v3/sessions/{session_id}", headers=bff(vendor_user, vendor))
    assert as_vendor.status_code == 200, as_vendor.text
    assert client.get(f"/api/v3/vendors/{vendor}", headers=headers).status_code == 200
    # 대화 상대가 아닌 실제 조직에게는 업체 프로필도 진단도 보이지 않는다
    other = signup(client, "other-emitter@example.com")
    other_org = new_org(client, other, "(주)남의회사", "emitter")["id"]
    assert error(client.get(f"/api/v3/vendors/{vendor}", headers=bff(other, other_org)), 404)
    hidden = client.get(f"/api/v3/sessions/{session_id}", headers=bff(other, other_org))
    assert error(hidden, 404) == "SESSION_NOT_FOUND"
    # 시연 업체로 체험하기를 열어도 실제 배출 기업의 진단은 보이지 않는다
    for demo_vendor in (SAEGIL, NAMHAE, MISO):
        listed = client.get("/api/v3/sessions", headers=demo(demo_vendor)).json()
        assert session_id not in {item["id"] for item in listed}
        viewed = client.get(f"/api/v3/sessions/{session_id}", headers=demo(demo_vendor))
        assert error(viewed, 404) == "SESSION_NOT_FOUND"
        orgs = client.get("/api/v3/orgs", headers=demo(demo_vendor)).json()
        assert org not in {item["id"] for item in orgs}
        found = client.get("/api/v3/evidence", params={"q": "런너"}, headers=demo(demo_vendor))
        assert all(org not in item["evidence"]["id"] for item in found.json())
        assert all(org not in item["evidence"].get("access", []) for item in found.json())
    # 시연 업체의 허가증 근거는 실제 배출 기업에도 열렸지만 체험하기에서도 그대로 보인다
    permit = client.get(f"/api/v3/vendors/{SAEGIL}", headers=demo(SAEGIL)).json()["permits"][0]
    evidence_id = permit["evidence"][0]
    assert client.get(f"/api/v3/evidence/{evidence_id}", headers=headers).status_code == 200
    shown = client.get(f"/api/v3/evidence/{evidence_id}", headers=demo(SAEGIL))
    assert shown.status_code == 200 and org not in shown.text
    hanbit = client.get("/api/v3/sessions", headers=demo(HANBIT)).json()[0]["id"]
    graph = client.get(f"/api/v3/sessions/{hanbit}", headers=demo(HANBIT)).json()["graph"]
    assert evidence_id in {node["id"] for node in graph["nodes"]}


# ── 시연 되돌리기 ────────────────────────────────────────────────────
def test_demo_reset_keeps_real_data_and_leaves_a_demo_diagnosis(world):
    client, state, _ = world
    user = signup(client, "keep@example.com")
    org = new_org(client, user, "(주)남는회사", "emitter")["id"]
    headers = bff(user, org)
    put = client.put(
        f"/api/v3/orgs/{org}/streams/st_{org[4:]}_a",
        headers=headers,
        json={"name": "가상 흐름", "wasteItem": "폐플라스틱", "material": "PP", "monthlyKg": 300},
    )
    assert put.status_code == 200, put.text
    session_id = client.post("/api/v3/sessions", headers=headers, json={}).json()["session"]["id"]
    answer_until_settled(client, session_id, headers)
    before = client.get(f"/api/v3/sessions/{session_id}", headers=headers).json()

    # 관리자만 되돌린다. 운영자는 스크립트로 관리자 조직에 넣는다
    admin = signup(client, "admin@example.com", "운영자")
    assert error(client.post("/api/v3/demo/reset", headers=bff(user, org)), 403) == "FORBIDDEN"
    assert error(client.post("/api/v3/demo/reset", headers=bff(admin, ADMIN)), 403) == (
        "NOT_A_MEMBER"
    )
    accounts.grant_membership(state, "admin@example.com", ADMIN)
    reset = client.post("/api/v3/demo/reset", headers=bff(admin, ADMIN))
    assert reset.status_code == 200, reset.text
    assert reset.json()["demoSessionStatus"] == "awaiting_approval"

    hanbit = client.get("/api/v3/sessions", headers=demo(HANBIT)).json()
    assert [s["id"] for s in hanbit] == [reset.json()["demoSessionId"]]
    assert hanbit[0]["status"] == "awaiting_approval"
    view = client.get(f"/api/v3/sessions/{hanbit[0]['id']}", headers=demo(HANBIT)).json()
    assert view["approvals"] and view["threads"]
    # 실제 조직의 조직·흐름·진단은 그대로다
    after = client.get(f"/api/v3/sessions/{session_id}", headers=headers)
    assert after.status_code == 200
    assert after.json()["session"] == before["session"]
    assert len(client.get(f"/api/v3/orgs/{org}/streams", headers=headers).json()) == 1
    me = client.get("/api/v3/accounts/me", headers=bff(user)).json()
    assert [m["orgId"] for m in me["memberships"]] == [org]
    # 한 번 더 되돌려도 시연 진단은 하나다
    again = client.post("/api/v3/demo/reset", headers=bff(admin, ADMIN))
    assert again.status_code == 200
    assert len(client.get("/api/v3/sessions", headers=demo(HANBIT)).json()) == 1


def test_accounts_routes_do_not_exist_outside_bff_mode(world, monkeypatch):
    client, _, _ = world
    monkeypatch.setattr(settings, "v3_auth", "header")
    body = {"email": "x@example.com", "password": PASSWORD, "name": "가"}
    assert error(client.post("/api/v3/accounts/signup", json=body), 404) == "NOT_FOUND"
    assert error(client.get("/api/v3/demo/orgs"), 404) == "NOT_FOUND"


# ── 운영자 명령과 설정 ───────────────────────────────────────────────
def test_operator_script_resets_demo_and_grants_admin(capsys):
    from scripts.v3 import accounts_admin

    state = service.App(InMemoryStore(), InMemoryRegistry(), auth_mode="bff", today="2026-11-17")
    assert accounts_admin.main(["reset-demo"], app=state) == 0
    assert '"demoSessionStatus": "awaiting_approval"' in capsys.readouterr().out
    assert accounts_admin.main(["grant-admin", "nobody@example.com"], app=state) == 2
    accounts.signup(state, {"email": "ops@example.com", "password": PASSWORD, "name": "운영"})
    assert accounts_admin.main(["grant-admin", "OPS@example.com"], app=state) == 0
    me = accounts.login(state, {"email": "ops@example.com", "password": PASSWORD})
    assert me["memberships"] == [
        {"orgId": ADMIN, "orgName": "Re:Source AI 운영", "kind": "admin", "memberRole": "owner"}
    ]
    header_mode = service.App(InMemoryStore(), InMemoryRegistry())
    assert accounts_admin.main(["reset-demo"], app=header_mode) == 2


def test_bff_auth_settings():
    from app.v3 import devserver, httpio

    auth = httpio.make_auth("BFF", bff_key=f"  {KEY} ")
    assert auth.mode == "bff" and auth.scoped and auth.bff_key == KEY and KEY not in repr(auth)
    who = auth.bff({"Authorization": f"Bearer {KEY}", "X-User-Id": "usr_a", "X-Org-Id": "org_a"})
    assert who == httpio.BffIdentity("usr_a", "org_a", False)
    assert auth.bff({"authorization": f"bearer {KEY}", "x-demo-view": "1"}).demo is True
    with pytest.raises(RuntimeError):
        auth.viewer({"X-Org-Id": "org_a"})  # bff 모드는 소속을 확인하지 않고 조직을 정하지 않는다
    assert httpio.make_auth("bff").bff_key == ""  # 키가 없어도 뜨고, 요청마다 503이다
    with pytest.raises(service.ServiceError) as caught:
        httpio.make_auth("bff").bff({"Authorization": f"Bearer {KEY}"})
    assert (caught.value.code, caught.value.status) == ("BFF_KEY_NOT_SET", 503)
    assert httpio.make_auth("header", bff_key=KEY).bff_key == ""
    with pytest.raises(SystemExit):
        devserver.load_auth("bff", None, {})  # 로컬 서버는 bff 모드를 쓰지 않는다
    with pytest.raises(SystemExit):
        devserver.load_auth(None, None, {"V3_AUTH": "bff"})


def test_sql_storage_runs_the_same_bff_flow(tmp_path):
    """SQL 저장(SQLite)에서도 가입, 조직, 시연 되돌리기(시연 조직만)가 같게 돈다."""
    from sqlalchemy import create_engine

    from app.v3.sql import SqlAccounts, SqlRegistry, SqlStore, create_tables

    engine = create_engine(
        f"sqlite:///{tmp_path / 'v3.db'}", connect_args={"check_same_thread": False}
    )
    create_tables(engine)
    state = service.App(
        SqlStore(engine),
        SqlRegistry(engine),
        auth_mode="bff",
        accounts=SqlAccounts(engine),
        today="2026-11-17",
    )
    first = service.reset_demo(state)
    assert first["demoSessionStatus"] == "awaiting_approval"
    user = accounts.signup(state, {"email": "sql@example.com", "password": PASSWORD, "name": "가"})
    org = accounts.create_org(state, user["user"]["id"], {"name": "(주)SQL가상", "kind": "vendor"})
    assert state.store.get_vendor_profile(org["id"])["permits"] == []
    with pytest.raises(service.ServiceError) as caught:
        accounts.signup(state, {"email": "SQL@example.com", "password": PASSWORD, "name": "나"})
    assert caught.value.code == "EMAIL_TAKEN"
    second = service.reset_demo(state, ADMIN)
    assert second["demoSessionStatus"] == "awaiting_approval"
    assert second["demoSessionId"] != first["demoSessionId"]
    assert [s["id"] for s in state.store.list_sessions()] == [second["demoSessionId"]]
    assert state.store.get_org(org["id"])["name"] == "(주)SQL가상"
    assert accounts.me(state, user["user"]["id"])["memberships"][0]["orgId"] == org["id"]
    engine.dispose()


def test_signed_up_names_do_not_block_unrelated_vendor_messages():
    """가입한 조직의 이름이 흔한 낱말이어도 대화한 적 없는 업체의 메시지를 막지 않는다."""
    from app.v3.a2a.message import Message
    from app.v3.a2a.policy import check_disclosure

    state = service.App(InMemoryStore(), InMemoryRegistry(), today="2026-11-17")
    service.seed(state)
    state.store.put_org({"id": "org_x1", "name": "(주)플라스틱", "role": "emitter", "demo": False})
    message = Message("th_1", SAEGIL, HANBIT, "offer", 5, "폐플라스틱 분쇄품을 받습니다.")
    assert check_disclosure(message, state.registry, state.store) == []
    state.store.put_thread(
        {"id": "th_x", "sessionId": "ses_x", "emitterOrgId": "org_x1", "vendorOrgId": SAEGIL}
    )
    named = check_disclosure(message, state.registry, state.store)
    assert [p["code"] for p in named] == ["OTHER_EMITTER_NAMED"]
    daon = Message("th_1", SAEGIL, HANBIT, "offer", 5, "다온산업 물량과 함께 싣겠습니다.")
    assert check_disclosure(daon, state.registry, state.store)  # 시연 조직은 그대로 본다
