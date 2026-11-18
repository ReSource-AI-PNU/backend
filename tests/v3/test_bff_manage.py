"""V3_AUTH=bff: 조직 고치기, 구성원 빼기·나가기, 소유자 지정, 비밀번호 바꾸기, 탈퇴, 운영자 명령.

이메일, 이름, 키, 조직, 수치는 모두 지어낸 값이다(실제 값이 아니다).
"""

import json

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
from tests.v3.test_bff_accounts import (
    ADMIN,
    HANBIT,
    KEY,
    PASSWORD,
    SAEGIL,
    Clock,
    answer_until_settled,
    bff,
    error,
    make_new_vendor,
    new_org,
    signup,
)

NEW_PASSWORD = "새-가상-비밀번호-5678"


@pytest.fixture
def world(monkeypatch):
    """test_bff_accounts의 world와 같다: bff 모드, 메모리 저장, 규칙 정책, 시드를 올린 빈 저장소."""
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
        first = client.post("/api/v3/demo/reset", headers=bff())
        assert first.status_code == 200, first.text
        yield client, state, clock
    api.set_state(None)


def invite_member(client, owner, org, user):
    token = client.post(f"/api/v3/orgs/{org}/invites", headers=bff(owner)).json()["token"]
    accepted = client.post(f"/api/v3/invites/{token}/accept", headers=bff(user))
    assert accepted.status_code == 200, accepted.text
    return token


def members(client, user, org):
    listed = client.get(f"/api/v3/orgs/{org}/members", headers=bff(user))
    assert listed.status_code == 200, listed.text
    return {m["userId"]: m["memberRole"] for m in listed.json()}


def delete_me(client, user, password=PASSWORD):
    return client.request(
        "DELETE", "/api/v3/accounts/me", headers=bff(user), json={"password": password}
    )


def login(client, email, password=PASSWORD):
    return client.post(
        "/api/v3/accounts/login", headers=bff(), json={"email": email, "password": password}
    )


# ── 조직 고치기 ──────────────────────────────────────────────────────
def test_owner_edits_the_org_and_others_cannot(world):
    client, state, _ = world
    owner = signup(client, "edit-owner@example.com")
    member = signup(client, "edit-member@example.com")
    stranger = signup(client, "edit-stranger@example.com")
    org = new_org(client, owner, "(주)가상정밀", "emitter", region="부산 강서구")["id"]
    invite_member(client, owner, org, member)
    path = f"/api/v3/orgs/{org}"

    patched = client.patch(
        path,
        headers=bff(owner),
        json={
            "name": " (주)가상정밀공업 ",
            "bizNo": "1234567890",
            "address": "부산 강서구 가상로 1",
            "location": [128.83, 35.09],
            "kind": "emitter",  # 같은 값이면 받는다
            "unknown": "무시한다",
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "(주)가상정밀공업" and body["bizNo"] == "123-45-67890"
    assert body["region"] == "부산 강서구" and body["location"] == [128.83, 35.09]
    assert body["memberRole"] == "owner" and body["kind"] == "emitter" and body["demo"] is False
    assert state.store.get_org(org)["address"] == "부산 강서구 가상로 1"
    # null이나 빈 글은 그 항목을 비운다. 이름은 비울 수 없다
    cleared = client.patch(path, headers=bff(owner), json={"region": None, "bizNo": ""})
    assert cleared.json()["region"] is None and cleared.json()["bizNo"] is None
    for bad in (
        {"name": " "},
        {"kind": "vendor"},
        {"bizNo": "12-34"},
        {"location": [200, 35]},
        {"address": "가" * 201},
    ):
        assert error(client.patch(path, headers=bff(owner), json=bad), 422) == "INVALID_VALUE"
    assert error(client.patch(path, headers=bff(member), json={"name": "x"}), 403) == "OWNER_ONLY"
    assert error(client.patch(path, headers=bff(stranger), json={"name": "x"}), 403) == (
        "NOT_A_MEMBER"
    )
    assert state.store.get_org(org)["name"] == "(주)가상정밀공업"
    # 시연 조직과 운영 조직은 owner여도 고칠 수 없다
    for seeded in (HANBIT, ADMIN):
        accounts.grant_membership(state, "edit-owner@example.com", seeded, "owner")
        refused = client.patch(f"/api/v3/orgs/{seeded}", headers=bff(owner), json={"name": "x"})
        assert error(refused, 403) == "DEMO_READ_ONLY"
    assert state.store.get_org(HANBIT)["name"] != "x"


# ── 구성원, 소유자 ───────────────────────────────────────────────────
def test_members_leave_owners_transfer_and_the_last_owner_stays(world):
    client, _, _ = world
    owner = signup(client, "m-owner@example.com", "대표")
    member = signup(client, "m-member@example.com", "담당")
    other = signup(client, "m-other@example.com", "다른 담당")
    org = new_org(client, owner, "(주)가상자원", "vendor")["id"]
    invite_member(client, owner, org, member)
    invite_member(client, owner, org, other)
    path = f"/api/v3/orgs/{org}/members"

    # member는 다른 사람을 뺄 수 없고 소유자를 지정할 수 없다
    assert error(client.delete(f"{path}/{other}", headers=bff(member)), 403) == "OWNER_ONLY"
    promote = client.post(f"/api/v3/orgs/{org}/owners/{other}", headers=bff(member))
    assert error(promote, 403) == "OWNER_ONLY"
    # 마지막 owner는 빠질 수 없다
    assert error(client.delete(f"{path}/{owner}", headers=bff(owner)), 409) == "LAST_OWNER"
    assert error(client.delete(f"{path}/usr_nobody", headers=bff(owner)), 404) == (
        "MEMBER_NOT_FOUND"
    )
    # member가 나가면서 남긴 쓰지 않은 초대는 지운다(owner가 다시 member로 넣은 뒤 확인한다)
    left = client.delete(f"{path}/{member}", headers=bff(member))
    assert left.status_code == 204 and left.content == b""
    assert member not in members(client, owner, org)
    assert error(client.get(path, headers=bff(member)), 403) == "NOT_A_MEMBER"
    # owner가 다른 구성원을 owner로 지정한다(이미 owner면 그대로)
    made = client.post(f"/api/v3/orgs/{org}/owners/{other}", headers=bff(owner))
    assert made.status_code == 200, made.text
    assert made.json() == {
        "userId": other,
        "name": "다른 담당",
        "email": "m-other@example.com",
        "memberRole": "owner",
    }
    again = client.post(f"/api/v3/orgs/{org}/owners/{other}", headers=bff(owner))
    assert again.json()["memberRole"] == "owner"
    gone = client.post(f"/api/v3/orgs/{org}/owners/{member}", headers=bff(owner))
    assert error(gone, 404) == "MEMBER_NOT_FOUND"
    # 이제 처음 owner가 나갈 수 있고, 새 owner가 초대한 것은 그 사람이 나가면 지워진다
    pending = client.post(f"/api/v3/orgs/{org}/invites", headers=bff(other)).json()["token"]
    assert client.delete(f"{path}/{owner}", headers=bff(owner)).status_code == 204
    assert members(client, other, org) == {other: "owner"}
    assert error(client.delete(f"{path}/{other}", headers=bff(other)), 409) == "LAST_OWNER"
    assert client.get(f"/api/v3/invites/{pending}", headers=bff()).status_code == 200
    invite_member(client, other, org, member)
    client.post(f"/api/v3/orgs/{org}/owners/{member}", headers=bff(other))
    assert client.delete(f"{path}/{other}", headers=bff(member)).status_code == 204
    assert error(client.get(f"/api/v3/invites/{pending}", headers=bff()), 410) == "INVITE_INVALID"


# ── 비밀번호 ─────────────────────────────────────────────────────────
def test_change_password(world):
    client, state, _ = world
    user = signup(client, "pw@example.com")
    path = "/api/v3/accounts/password"
    before = state.accounts.get_user(user)["passwordHash"]
    wrong = client.post(
        path, headers=bff(user), json={"currentPassword": "틀린-0000", "newPassword": NEW_PASSWORD}
    )
    assert error(wrong, 401) == "INVALID_CREDENTIALS"
    for new in ("1234567", PASSWORD, None):
        body = {"currentPassword": PASSWORD, "newPassword": new}
        assert error(client.post(path, headers=bff(user), json=body), 422) == "INVALID_VALUE"
    assert error(client.post(path, headers=bff(), json={}), 401) == "USER_REQUIRED"
    done = client.post(
        path, headers=bff(user), json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD}
    )
    assert done.status_code == 204 and done.content == b""
    after = state.accounts.get_user(user)["passwordHash"]
    assert after != before and after.startswith("scrypt$") and NEW_PASSWORD not in after
    assert error(login(client, "pw@example.com"), 401) == "INVALID_CREDENTIALS"
    assert login(client, "pw@example.com", NEW_PASSWORD).status_code == 200
    # 틀린 횟수는 로그인과 같은 제한에 센다(이메일마다 15분에 10번)
    for _ in range(10):
        body = {"currentPassword": "틀린-0000", "newPassword": PASSWORD}
        assert error(client.post(path, headers=bff(user), json=body), 401)
    blocked = {"currentPassword": NEW_PASSWORD, "newPassword": PASSWORD}
    assert error(client.post(path, headers=bff(user), json=blocked), 429) == "TOO_MANY_ATTEMPTS"
    assert error(login(client, "pw@example.com", NEW_PASSWORD), 429) == "TOO_MANY_ATTEMPTS"


# ── 탈퇴 ─────────────────────────────────────────────────────────────
def test_a_member_deletes_the_account_and_the_org_stays(world):
    client, state, _ = world
    owner = signup(client, "d-owner@example.com")
    member = signup(client, "d-member@example.com", "지울 사람")
    org = new_org(client, owner, "(주)남는조직", "emitter")["id"]
    invite_member(client, owner, org, member)
    assert error(delete_me(client, member, "틀린-0000"), 401) == "INVALID_CREDENTIALS"
    deleted = delete_me(client, member)
    assert deleted.status_code == 204, deleted.text
    assert state.accounts.get_user(member) is None
    assert state.accounts.find_user("d-member@example.com") is None
    assert error(client.get("/api/v3/accounts/me", headers=bff(member)), 401) == "UNKNOWN_USER"
    assert error(login(client, "d-member@example.com"), 401) == "INVALID_CREDENTIALS"
    assert members(client, owner, org) == {owner: "owner"}
    assert state.store.get_org(org)["name"] == "(주)남는조직"
    # 같은 이메일로 다시 가입할 수 있다(새 사용자다)
    assert signup(client, "d-member@example.com") != member


def test_a_sole_owner_with_members_must_transfer_first(world):
    client, state, _ = world
    owner = signup(client, "t-owner@example.com")
    member = signup(client, "t-member@example.com")
    org = new_org(client, owner, "(주)넘길조직", "vendor")["id"]
    invite_member(client, owner, org, member)
    refused = delete_me(client, owner)
    assert error(refused, 409) == "TRANSFER_OWNERSHIP_FIRST"
    assert "(주)넘길조직" in refused.json()["error"]["message"]
    assert state.accounts.get_user(owner) is not None  # 아무것도 지우지 않았다
    assert members(client, owner, org) == {owner: "owner", member: "member"}
    client.post(f"/api/v3/orgs/{org}/owners/{member}", headers=bff(owner))
    assert delete_me(client, owner).status_code == 204
    assert members(client, member, org) == {member: "owner"}
    assert state.store.get_vendor_profile(org)["orgId"] == org


def _emitter_with_session(client, email="gone-emitter@example.com", material="PP"):
    user = signup(client, email, "배출 담당")
    org = new_org(
        client, user, "(주)떠날사출", "emitter", region="부산 강서구", location=[128.83, 35.09]
    )["id"]
    headers = bff(user, org)
    stream_id = f"st_{org[4:]}_runner"
    put = client.put(
        f"/api/v3/orgs/{org}/streams/{stream_id}",
        headers=headers,
        json={
            "name": "런너",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": material,
            "origin": "PIR",
            "form": "가공 전",
            "monthlyKg": 800,
            "bulkDensityKgPerM3": 250,
        },
    )
    assert put.status_code == 200, put.text
    session_id = client.post("/api/v3/sessions", headers=headers, json={}).json()["session"]["id"]
    view = answer_until_settled(client, session_id, headers)
    return user, org, stream_id, session_id, view


def test_deleting_a_sole_owned_emitter_deletes_its_data_only(world):
    client, state, _ = world
    vendor_user, vendor = make_new_vendor(client)
    user, org, _, session_id, view = _emitter_with_session(client)
    assert any(t["vendorOrgId"] == vendor for t in view["threads"])
    mine = [e.id for e in state.registry.list(org_id=None) if org in e.access]
    private = [e.id for e in state.registry.list(org_id=None) if tuple(e.access) == (org,)]
    assert private
    demo_sessions = [s["id"] for s in state.store.list_sessions(HANBIT)]
    pending = client.post(f"/api/v3/orgs/{org}/invites", headers=bff(user)).json()["token"]

    assert delete_me(client, user).status_code == 204
    # 사용자, 소속, 초대
    assert state.accounts.get_user(user) is None
    assert state.accounts.list_memberships(org_id=org) == []
    assert error(client.get(f"/api/v3/invites/{pending}", headers=bff()), 410) == "INVITE_INVALID"
    # 조직 레코드는 개인·사업자 정보 없는 자리만 남는다
    assert state.store.get_org(org) == {
        "id": org,
        "name": "탈퇴한 배출 기업",
        "role": "emitter",
        "demo": False,
        "deleted": True,
        "deletedAt": state.store.get_org(org)["deletedAt"],
    }
    # 흐름, 진단(대화·질문·승인·문서·실행 기록), 그 조직만 보던 근거는 없다
    assert state.store.list_streams(org) == []
    assert state.store.list_sessions(org) == []
    assert state.store.list_threads(session_id=session_id) == []
    assert state.store.list_activity(session_id) == []
    assert not any(state.registry.exists(e) for e in private)
    # 업체에게도 그 진단은 사라지고, 업체의 자료와 다른 조직의 자료는 그대로다
    as_vendor = client.get(f"/api/v3/sessions/{session_id}", headers=bff(vendor_user, vendor))
    assert error(as_vendor, 404) == "SESSION_NOT_FOUND"
    assert state.store.get_vendor_profile(vendor)["permits"]
    assert [s["id"] for s in state.store.list_sessions(HANBIT)] == demo_sessions
    shared = [e for e in mine if e not in private]
    assert all(state.registry.exists(e) for e in shared)  # 상대에게 열린 근거는 상대의 기록이다
    # 지운 조직은 목록에 나오지 않고 구성원을 들일 수 없다
    assert org not in {o["id"] for o in service.list_orgs(state)}
    with pytest.raises(ValueError):
        accounts.grant_membership(state, "d-x@example.com", org)


def test_deleting_a_vendor_closes_open_threads_and_keeps_other_orgs_consistent(world):
    client, state, _ = world
    vendor_user, vendor = make_new_vendor(client)
    user, org, _, session_id, view = _emitter_with_session(client, "stay-emitter@example.com")
    headers = bff(user, org)
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == vendor)
    assert thread["status"] in ("open", "agreed"), thread
    before = view["session"]["status"]
    fleet_evidence = state.store.get_vendor_profile(vendor)["fleet"]["evidence"]
    permit_evidence = state.store.get_vendor_profile(vendor)["permits"][0]["evidence"]

    assert delete_me(client, vendor_user).status_code == 204
    after = client.get(f"/api/v3/sessions/{session_id}", headers=headers)
    assert after.status_code == 200, after.text
    closed = next(t for t in after.json()["threads"] if t["vendorOrgId"] == vendor)
    assert closed["status"] == "handoff"
    assert closed["statusReason"] == accounts.DEPARTED_REASON
    assert closed["vendorName"] == "탈퇴한 업체"
    assert closed["messages"][-1]["type"] == "handoff"
    assert closed["id"] not in {a["threadId"] for a in after.json()["approvals"]}
    assert all(a["vendorOrgId"] != vendor for a in after.json()["result"]["assignments"])
    assert after.json()["session"]["status"] in (before, "idle")
    # 남은 대화는 그대로 승인할 수 있다
    for item in after.json()["approvals"]:
        approved = client.post(
            f"/api/v3/sessions/{session_id}/approvals",
            headers=headers,
            json={"threadId": item["threadId"]},
        )
        assert approved.status_code == 200, approved.text
    # 업체 프로필과 그 업체만 보던 근거(차량)는 지우고, 상대에게 열린 허가증 근거는 남긴다
    with pytest.raises(KeyError):
        state.store.get_vendor_profile(vendor)
    assert not any(state.registry.exists(e) for e in fleet_evidence)
    assert all(state.registry.exists(e) for e in permit_evidence)
    # 상대에게는 지운 자리만 보이고, 새 진단은 그 업체를 찾지 않는다
    listed = {o["id"]: o for o in client.get("/api/v3/orgs", headers=headers).json()}
    assert listed[vendor]["name"] == "탈퇴한 업체" and "location" not in listed[vendor]
    fresh = client.post("/api/v3/sessions", headers=headers, json={}).json()["session"]["id"]
    again = answer_until_settled(client, fresh, headers)
    assert vendor not in {t["vendorOrgId"] for t in again["threads"]}
    assert {SAEGIL} <= {t["vendorOrgId"] for t in again["threads"]}


def _only_vendor_for_pc(client):
    """PC(폴리카보네이트)를 받는 업체는 이 업체 하나다(시연 업체는 PC 허가가 없다). 값은 가상이다."""
    user = signup(client, "pc-vendor@example.com", "업체 담당")
    org = new_org(client, user, "(주)가상PC자원", "vendor", location=[128.97, 35.09])["id"]
    profile = {
        "permits": [{"wasteItem": "폐플라스틱", "materials": ["PC"]}],
        "criteria": [
            {
                "id": "pc_new_pc_b",
                "material": "PC",
                "form": None,
                "grade": "B",
                "maxContaminationPct": 10,
                "maxMoisturePct": 3,
                "unitPriceKrwPerKg": 150,
                "minMonthlyKg": 100,
            }
        ],
        "fleet": {"depot": [128.97, 35.09], "capacityKg": 3000, "capacityM3": 15, "costPerKm": 900},
        "guardrails": {"floorPriceKrwPerKg": 100, "ceilingPriceKrwPerKg": 300},
    }
    saved = client.put(f"/api/v3/vendors/{org}", headers=bff(user, org), json=profile)
    assert saved.status_code == 200, saved.text
    return user, org


def test_a_vendor_with_a_confirmed_deal_keeps_its_profile_out_of_discovery(world):
    client, state, _ = world
    vendor_user, vendor = _only_vendor_for_pc(client)
    user, org, _, session_id, view = _emitter_with_session(
        client, "deal@example.com", material="PC"
    )
    headers = bff(user, org)
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == vendor)
    assert thread["status"] == "agreed", thread
    for side in (headers, bff(vendor_user, vendor)):
        approved = client.post(
            f"/api/v3/sessions/{session_id}/approvals",
            headers=side,
            json={"threadId": thread["id"]},
        )
        assert approved.status_code == 200, approved.text
    confirmed = client.get(f"/api/v3/sessions/{session_id}", headers=headers).json()
    assert next(t for t in confirmed["threads"] if t["id"] == thread["id"])["status"] == (
        "confirmed"
    )
    assert delete_me(client, vendor_user).status_code == 204
    kept = state.store.get_vendor_profile(vendor)
    assert kept["departed"] is True and kept["criteria"]
    assert all(state.registry.exists(e) for e in kept["fleet"]["evidence"])
    view = client.get(f"/api/v3/sessions/{session_id}", headers=headers)
    assert view.status_code == 200
    still = next(t for t in view.json()["threads"] if t["id"] == thread["id"])
    assert still["status"] == "confirmed" and still["vendorName"] == "탈퇴한 업체"
    fresh = client.post("/api/v3/sessions", headers=headers, json={}).json()["session"]["id"]
    again = answer_until_settled(client, fresh, headers)
    assert vendor not in {t["vendorOrgId"] for t in again["threads"]}


def test_new_routes_exist_only_in_bff_mode(world, monkeypatch):
    client, _, _ = world
    monkeypatch.setattr(settings, "v3_auth", "header")
    for method, path in (
        ("PATCH", "/api/v3/orgs/org_x"),
        ("DELETE", "/api/v3/orgs/org_x/members/usr_x"),
        ("POST", "/api/v3/orgs/org_x/owners/usr_x"),
        ("POST", "/api/v3/accounts/password"),
        ("DELETE", "/api/v3/accounts/me"),
    ):
        assert error(client.request(method, path, json={}), 404) == "NOT_FOUND"


# ── 운영자 명령 ──────────────────────────────────────────────────────
def test_operator_lists_and_deletes_users(capsys):
    from scripts.v3 import accounts_admin

    state = service.App(InMemoryStore(), InMemoryRegistry(), auth_mode="bff", today="2026-11-17")
    service.seed(state)
    owner = accounts.signup(
        state, {"email": "ops-a@example.com", "password": PASSWORD, "name": "가"}
    )
    member = accounts.signup(
        state, {"email": "ops_b@example.com", "password": PASSWORD, "name": "나"}
    )
    org = accounts.create_org(
        state, owner["user"]["id"], {"name": "(주)운영가상", "kind": "emitter"}
    )
    state.accounts.add_membership(
        {
            "userId": member["user"]["id"],
            "orgId": org["id"],
            "memberRole": "member",
            "createdAt": "2026-11-18T00:00:00+00:00",
        }
    )
    capsys.readouterr()
    assert accounts_admin.main(["list-users", "--like", "ops_b"], app=state) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["email"] for r in rows] == ["ops_b@example.com"]  # _는 글자 그대로다
    assert rows[0]["memberships"][0]["memberRole"] == "member" and "passwordHash" not in rows[0]
    assert accounts_admin.main(["list-users", "--like", "*@EXAMPLE.com"], app=state) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    # 다른 구성원이 있으면 --force-org 없이는 지우지 않는다
    assert accounts_admin.main(["delete-user", "OPS-A@example.com"], app=state) == 2
    assert "TRANSFER_OWNERSHIP_FIRST" in capsys.readouterr().err
    assert accounts_admin.main(["delete-user", "nobody@example.com"], app=state) == 2
    assert accounts_admin.main(["delete-user", "ops-a@example.com", "--force-org"], app=state) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["deletedOrgs"] == [org["id"]] and summary["leftOrgs"] == []
    assert state.accounts.find_user("ops-a@example.com") is None
    assert state.accounts.list_memberships(user_id=member["user"]["id"]) == []
    assert state.store.get_org(org["id"])["deleted"] is True


def test_sql_storage_deletes_an_account_with_its_org(tmp_path):
    """SQL 저장(SQLite)에서도 탈퇴가 같은 것을 지우고 남긴다(진단, 흐름, 근거, 조직 자리)."""
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
    service.reset_demo(state)
    demo = [s["id"] for s in state.store.list_sessions()]
    user = accounts.signup(
        state, {"email": "sql-d@example.com", "password": PASSWORD, "name": "가"}
    )
    user_id = user["user"]["id"]
    org = accounts.create_org(
        state, user_id, {"name": "(주)SQL떠남", "kind": "emitter", "location": [128.83, 35.09]}
    )["id"]
    stream_id = f"st_{org[4:]}_a"
    service.update_stream(
        state,
        org,
        stream_id,
        {"name": "가상 흐름", "wasteItem": "폐플라스틱", "material": "PP", "monthlyKg": 300},
        org,
    )
    session_id = service.create_session(state, org, org)["session"]["id"]
    service.run_session(state, session_id, org)
    private = [e.id for e in state.registry.list(org_id=None) if tuple(e.access) == (org,)]
    assert private
    with pytest.raises(service.ServiceError) as caught:
        accounts.delete_account(state, user_id, {"password": "틀린-0000"})
    assert caught.value.code == "INVALID_CREDENTIALS"
    accounts.delete_account(state, user_id, {"password": PASSWORD})
    assert state.accounts.get_user(user_id) is None
    assert state.accounts.find_user("sql-d@example.com") is None
    assert state.accounts.list_memberships(org_id=org) == []
    assert state.store.get_org(org)["deleted"] is True
    assert "location" not in state.store.get_org(org)
    assert state.store.list_streams(org) == []
    assert [s["id"] for s in state.store.list_sessions()] == demo
    assert state.store.list_activity(session_id) == []
    assert not any(state.registry.exists(e) for e in private)
    engine.dispose()
