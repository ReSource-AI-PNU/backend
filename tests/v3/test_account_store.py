"""계정 저장소(메모리·SQL)와 시연 조직만 지우는 저장소 메서드. 이메일과 이름은 지어낸 값이다."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine

from app.v3.account_store import EmailTaken, InMemoryAccounts, LoginLimiter, iso
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.sql import SqlAccounts, SqlRegistry, SqlStore, create_tables
from app.v3.store import InMemoryStore, NotFound

NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'v3.db'}", connect_args={"check_same_thread": False}
    )
    create_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture(params=["memory", "sql"])
def accounts(request, engine):
    clock = lambda: NOW
    return InMemoryAccounts(clock) if request.param == "memory" else SqlAccounts(engine, clock)


def user(user_id="usr_a", email="a@example.com"):
    return {
        "id": user_id,
        "email": email,
        "name": "가나다",
        "passwordHash": "scrypt$16384$8$1$x$y",
        "createdAt": iso(NOW),
    }


def test_users_are_unique_by_email(accounts):
    assert accounts.add_user(user()) == user()
    assert accounts.get_user("usr_a") == user()
    assert accounts.find_user("a@example.com") == user()
    assert accounts.find_user("b@example.com") is None and accounts.get_user("usr_x") is None
    with pytest.raises(EmailTaken):
        accounts.add_user(user("usr_b"))
    assert accounts.get_user("usr_b") is None


def test_memberships_keep_the_first_role(accounts):
    first = {"userId": "usr_a", "orgId": "org_x", "memberRole": "owner", "createdAt": iso(NOW)}
    assert accounts.add_membership(first) == first
    again = accounts.add_membership({**first, "memberRole": "member"})
    assert again["memberRole"] == "owner"
    accounts.add_membership({**first, "userId": "usr_b", "memberRole": "member"})
    accounts.add_membership({**first, "orgId": "org_y"})
    assert accounts.get_membership("usr_a", "org_x")["memberRole"] == "owner"
    assert accounts.get_membership("usr_b", "org_y") is None
    assert {m["userId"] for m in accounts.list_memberships(org_id="org_x")} == {"usr_a", "usr_b"}
    assert {m["orgId"] for m in accounts.list_memberships(user_id="usr_a")} == {"org_x", "org_y"}


def test_invites_are_used_once_and_expire(accounts):
    base = {"orgId": "org_x", "createdBy": "usr_a", "createdAt": iso(NOW), "usedAt": None}
    accounts.add_invite({**base, "tokenHash": "h1", "expiresAt": iso(NOW + timedelta(days=7))})
    accounts.add_invite({**base, "tokenHash": "h2", "expiresAt": iso(NOW + timedelta(days=7))})
    peeked = accounts.get_invite("h1")  # 읽기만 한다(쓰지 않는다)
    assert peeked["orgId"] == "org_x" and peeked["usedAt"] is None
    assert accounts.get_invite("없음") is None
    used = accounts.use_invite("h1", "usr_b", NOW)
    assert used["orgId"] == "org_x" and used["usedBy"] == "usr_b" and used["usedAt"] == iso(NOW)
    assert accounts.get_invite("h1")["usedBy"] == "usr_b"
    assert accounts.use_invite("h1", "usr_c", NOW) is None  # 한 번만
    assert accounts.use_invite("없음", "usr_c", NOW) is None
    assert accounts.use_invite("h2", "usr_c", NOW + timedelta(days=7)) is None  # 만료
    assert accounts.use_invite("h2", "usr_c", NOW + timedelta(days=6)) is not None


def test_login_limiter_counts_failures_in_the_window():
    now = [0.0]
    limiter = LoginLimiter(limit=3, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert not limiter.blocked("a@example.com")
        limiter.failed("a@example.com")
    assert limiter.blocked("a@example.com") and not limiter.blocked("b@example.com")
    now[0] = 61.0
    assert not limiter.blocked("a@example.com")
    limiter.failed("a@example.com")
    limiter.succeeded("a@example.com")
    assert not limiter.blocked("a@example.com")


@pytest.fixture(params=["memory", "sql"])
def store(request, engine):
    made = InMemoryStore() if request.param == "memory" else SqlStore(engine)
    for org_id, role in (("org_d", "emitter"), ("org_dv", "vendor"), ("org_r", "emitter")):
        made.put_org({"id": org_id, "name": org_id, "role": role})
    made.put_stream({"id": "st_d", "orgId": "org_d"})
    made.put_stream({"id": "st_r", "orgId": "org_r"})
    made.put_vendor_profile({"orgId": "org_dv", "permits": []})
    for sid, emitter in (("ses_d", "org_d"), ("ses_r", "org_r")):
        made.put_session({"id": sid, "emitterOrgId": emitter, "status": "idle"})
        made.put_thread(
            {"id": f"th_{sid}", "sessionId": sid, "emitterOrgId": emitter, "vendorOrgId": "org_dv"}
        )
        made.put_question(sid, {"id": "q_1"})
        made.add_event(sid, "system", "stage", "만들었다")
    return made


def test_purge_orgs_removes_only_those_orgs_and_their_sessions(store):
    assert store.next_id("ses") == "ses_1"
    removed = store.purge_orgs({"org_d", "org_dv"})
    assert removed == ["th_ses_d"]
    assert [o["id"] for o in store.list_orgs()] == ["org_r"]
    assert [s["id"] for s in store.list_sessions()] == ["ses_r"]
    with pytest.raises(NotFound):
        store.get_stream("st_d")
    with pytest.raises(NotFound):
        store.get_vendor_profile("org_dv")
    # 실제 조직의 진단은 업체가 지워진 조직이어도 그대로다
    assert store.get_thread("th_ses_r")["vendorOrgId"] == "org_dv"
    assert store.list_questions("ses_r") and store.list_events("ses_r")
    assert store.list_questions("ses_d") == [] and store.list_events("ses_d") == []
    assert store.next_id("ses") == "ses_2"  # ID 카운터는 지우지 않는다


@pytest.mark.parametrize("kind", ["memory", "sql"])
def test_registry_delete_removes_only_the_given_ids(kind, engine):
    registry = InMemoryRegistry() if kind == "memory" else SqlRegistry(engine)
    for evidence_id in ("human:org_d:a", "human:org_d:b", "human:org_r:a"):
        org = evidence_id.split(":")[1]
        registry.put(
            Evidence(
                id=evidence_id,
                kind="human",
                excerpt="가상",
                source="담당자 입력",
                as_of="2026-11-18",
                access=(org,),
            )
        )
    assert registry.delete(["human:org_d:a", "human:org_d:b", "없음"]) == 2
    assert [e.id for e in registry.list()] == ["human:org_r:a"]
