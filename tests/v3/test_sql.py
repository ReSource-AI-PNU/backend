"""SqlRegistry와 SqlStore가 메모리 구현과 같은 동작을 하는지 SQLite로 확인한다."""

import json

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import (
    EvidenceForbidden,
    EvidenceNotFound,
    InMemoryRegistry,
)
from app.v3.sql import SqlRegistry, SqlStore, create_tables
from app.v3.store import InMemoryStore, NotFound, VersionConflict

CLOCK = "2026-11-17T00:00:00+00:00"


def clock():
    return CLOCK


def plain(value):
    """JSON으로 한 번 돌린 값(튜플은 리스트). 두 구현의 내용을 비교할 때 쓴다."""
    return json.loads(json.dumps(value, ensure_ascii=False))


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'v3.db'}", connect_args={"check_same_thread": False}
    )
    create_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture(params=["memory", "sql"])
def registry(request, engine):
    return InMemoryRegistry() if request.param == "memory" else SqlRegistry(engine)


@pytest.fixture(params=["memory", "sql"])
def store(request, engine):
    made = InMemoryStore(clock) if request.param == "memory" else SqlStore(engine, clock)
    made.put_org({"id": "org_a", "name": "가", "role": "emitter"})
    made.put_org({"id": "org_b", "name": "나", "role": "emitter"})
    made.put_org({"id": "org_v", "name": "다", "role": "vendor"})
    made.put_stream({"id": "st_a1", "orgId": "org_a", "monthlyKg": 600})
    made.put_stream({"id": "st_a2", "orgId": "org_a", "monthlyKg": 100})
    made.put_stream({"id": "st_b1", "orgId": "org_b", "monthlyKg": 900})
    return made


def evidence(evidence_id="measure:org_a:st_a1:오염도@2026-11-10", **changes):
    base = {
        "id": evidence_id,
        "kind": evidence_id.split(":", 1)[0],
        "excerpt": "오염도 측정",
        "source": "측정값 CSV",
        "as_of": "2026-11-10",
        "value": 1.5,
        "unit": "%",
        "access": ("org_a",),
        "meta": {"row": 2, "columns": ["오염도"]},
    }
    return Evidence(**{**base, **changes})


# ── 근거 레지스트리 ──────────────────────────────────────────────────
def test_registry_versions(registry):
    first = registry.put(evidence())
    assert first.version == 1
    assert registry.put(evidence()).version == 1  # 내용이 같으면 그대로
    assert registry.put(evidence(value=2.0)).version == 2
    assert registry.put(evidence(value=2.0)).version == 2
    saved = registry.get(first.id)
    assert (saved.value, saved.unit, saved.version, saved.as_of) == (2.0, "%", 2, "2026-11-10")
    assert saved.meta == {"row": 2, "columns": ["오염도"]}
    assert registry.exists(first.id) and not registry.exists("measure:없는:것")


def test_registry_org_filter(registry):
    mine = registry.put(evidence())
    law = registry.put(
        evidence(
            "law:폐기물관리법:제17조@2026-01-01",
            value=None,
            unit=None,
            excerpt="사업장폐기물배출자는",
            access=("*",),
            valid_until="2026-12-31",
        )
    )
    assert registry.get(mine.id, "org_a").id == mine.id
    assert registry.get(mine.id).id == mine.id  # org_id가 없으면 시스템 내부 호출
    with pytest.raises(EvidenceForbidden):
        registry.get(mine.id, "org_b")
    with pytest.raises(EvidenceNotFound):
        registry.get("measure:없는:것", "org_a")
    assert [e.id for e in registry.list(org_id="org_b")] == [law.id]
    assert [e.id for e in registry.list(org_id="org_a")] == [law.id, mine.id]
    assert [e.id for e in registry.list(kinds=["measure"], org_id="org_a")] == [mine.id]
    assert registry.list(kinds=["measure"], org_id="org_b") == []
    assert registry.get(law.id, "org_b").valid_until == "2026-12-31"
    assert registry.get(law.id, "org_b").value is None


def test_registry_keeps_empty_access_private(registry):
    """access가 비어 있으면 어느 조직도 볼 수 없어야 한다(공개로 바뀌면 안 된다)."""
    hidden = registry.put(evidence("measure:org_a:st_a1:수분@2026-11-10", access=()))
    assert registry.get(hidden.id).access == ()
    with pytest.raises(EvidenceForbidden):
        registry.get(hidden.id, "org_a")
    assert registry.list(org_id="org_a") == []


def test_registry_clear_removes_everything(registry):
    """service.reset_demo가 부른다. 두 구현 모두 근거를 비우고 버전도 처음부터 센다."""
    registry.put(evidence())
    registry.put(evidence(value=2.0))
    registry.clear()
    assert registry.list() == [] and not registry.exists(evidence().id)
    assert registry.put(evidence()).version == 1


def test_sql_registry_persists_across_instances(engine):
    SqlRegistry(engine).put(evidence())
    again = SqlRegistry(engine)
    assert again.get(evidence().id, "org_a").value == 1.5
    assert again.put(evidence()).version == 1


# ── 저장소 ───────────────────────────────────────────────────────────
def test_reads_are_deep_copies(store):
    stream = store.get_stream("st_a1")
    stream["monthlyKg"] = 1
    store.list_streams("org_a")[0]["monthlyKg"] = 2
    assert store.get_stream("st_a1")["monthlyKg"] == 600
    source = {"id": "st_a3", "orgId": "org_a", "measure": {"contaminationPct": 1}}
    saved = store.put_stream(source)
    source["measure"]["contaminationPct"] = 99
    saved["measure"]["contaminationPct"] = 98
    assert store.get_stream("st_a3")["measure"]["contaminationPct"] == 1


def test_org_boundary_on_streams(store):
    assert [s["id"] for s in store.list_streams("org_a")] == ["st_a1", "st_a2"]
    assert [s["id"] for s in store.list_streams("org_b")] == ["st_b1"]
    assert [o["id"] for o in store.list_orgs(role="emitter")] == ["org_a", "org_b"]
    assert store.streams_visible_to("org_v") == []
    with pytest.raises(NotFound):
        store.get_stream("st_a1", "org_b")
    with pytest.raises(NotFound):
        store.get_stream("st_a1", "org_v")
    with pytest.raises(ValueError, match="orgId"):
        store.put_stream({"id": "st_x"})
    thread = {
        "id": "th_1",
        "sessionId": "ses_1",
        "emitterOrgId": "org_a",
        "vendorOrgId": "org_v",
        "streamIds": ["st_a1"],
        "status": "not_opened",
    }
    store.put_thread(thread)
    assert store.streams_visible_to("org_v") == []  # 아직 열리지 않았다
    store.put_thread({**thread, "status": "open"})
    assert [s["id"] for s in store.streams_visible_to("org_v")] == ["st_a1"]
    assert store.get_stream("st_a1", "org_v")["id"] == "st_a1"
    with pytest.raises(NotFound):
        store.get_stream("st_a2", "org_v")
    assert [t["id"] for t in store.list_threads(org_id="org_v")] == ["th_1"]
    assert [t["id"] for t in store.list_threads(session_id="ses_1")] == ["th_1"]
    assert store.list_threads(org_id="org_b") == []
    assert store.list_threads(session_id="ses_2") == []
    assert store.get_thread("th_1")["status"] == "open"


def test_vendor_profiles(store):
    store.put_vendor_profile({"orgId": "org_v", "permits": []})
    assert store.get_vendor_profile("org_v") == {"orgId": "org_v", "permits": []}
    assert [p["orgId"] for p in store.list_vendor_profiles()] == ["org_v"]
    with pytest.raises(NotFound):
        store.get_vendor_profile("org_a")


def test_session_versions(store):
    saved = store.put_session(
        {"id": store.next_id("ses"), "emitterOrgId": "org_a", "status": "idle"}
    )
    assert (saved["id"], saved["version"], saved["createdAt"]) == ("ses_1", 1, CLOCK)
    again = store.put_session({**saved, "status": "running"}, expected_version=1)
    assert again["version"] == 2
    with pytest.raises(VersionConflict):
        store.put_session({**saved, "status": "idle"}, expected_version=1)
    assert store.get_session("ses_1")["status"] == "running"
    assert store.next_id("ses") == "ses_2" and store.next_id("th") == "th_1"
    assert [s["id"] for s in store.list_sessions("org_a")] == ["ses_1"]
    assert [s["id"] for s in store.list_sessions()] == ["ses_1"]
    assert store.list_sessions("org_b") == []
    store.put_thread(
        {"id": "th_1", "sessionId": "ses_1", "emitterOrgId": "org_a", "vendorOrgId": "org_v"}
    )
    assert [s["id"] for s in store.list_sessions("org_v")] == ["ses_1"]
    with pytest.raises(NotFound):
        store.get_session("ses_9")


def test_questions_documents_events(store):
    store.put_question("ses_1", {"id": "q_1", "batch": 1, "answered": False})
    store.put_question("ses_1", {"id": "q_2", "batch": 1, "answered": False})
    store.put_question("ses_1", {"id": "q_1", "batch": 1, "answered": True})
    assert store.list_questions("ses_1") == [
        {"id": "q_1", "batch": 1, "answered": True},
        {"id": "q_2", "batch": 1, "answered": False},
    ]
    store.put_document("ses_1", {"id": "doc_1", "kind": "request", "title": "요청서"})
    assert store.list_documents("ses_1")[0]["kind"] == "request" and store.list_documents("x") == []
    first = store.add_event(
        "ses_1", "tool", "tool_call", "경로 판정", detail={"tool": "judge_route"}
    )
    second = store.add_event("ses_1", "validator", "validation", "통과", thread_id="th_1")
    other = store.add_event("ses_2", "system", "message", "다른 세션")
    assert (first["seq"], second["seq"], other["seq"]) == (1, 2, 1)
    assert (second["threadId"], first["at"], first["detail"]) == (
        "th_1",
        CLOCK,
        {"tool": "judge_route"},
    )
    assert set(first) == {"seq", "at", "actor", "kind", "threadId", "summary", "detail"}
    assert [e["seq"] for e in store.list_events("ses_1")] == [1, 2]
    assert store.list_events("ses_1")[0] == first


def test_approvals_and_block(store):
    assert store.list_approvals("ses_1") == []
    store.set_approval("ses_1", "th_1", "emitter")
    item = store.set_approval("ses_1", "th_1", "vendor")
    assert item["emitter"] and item["vendor"] and not item["blocked"]
    blocked = store.block_approval("ses_1", "th_1", "근거 확인 실패 항목이 있습니다.")
    assert blocked["blocked"] and not blocked["emitter"] and not blocked["vendor"]
    with pytest.raises(PermissionError):
        store.set_approval("ses_1", "th_1", "emitter")
    store.block_approval("ses_1", "th_1", None)
    store.set_approval("ses_1", "th_1", "emitter")
    assert store.list_approvals("ses_1") == [
        {
            "threadId": "th_1",
            "emitter": True,
            "vendor": False,
            "blocked": False,
            "blockedReason": None,
        }
    ]
    with pytest.raises(ValueError, match="emitter"):
        store.set_approval("ses_1", "th_1", "admin")


def test_reset_clears_records_and_counters(store):
    store.next_id("ses")
    store.add_event("ses_1", "system", "message", "지울 기록")
    store.put_question("ses_1", {"id": "q_1"})
    store.reset()
    assert store.list_orgs() == [] and store.list_streams("org_a") == []
    assert store.list_events("ses_1") == [] and store.list_questions("ses_1") == []
    assert store.next_id("ses") == "ses_1"


def test_sql_store_persists_and_leaves_registry_alone(engine):
    first = SqlStore(engine, clock)
    first.put_org({"id": "org_a", "name": "가", "role": "emitter"})
    SqlRegistry(engine).put(evidence())
    second = SqlStore(engine, clock)
    assert second.get_org("org_a")["name"] == "가"
    second.reset()
    assert first.list_orgs() == []
    assert SqlRegistry(engine).exists(evidence().id)  # reset은 근거를 지우지 않는다


def test_demo_seed_gives_same_state_in_both_stores(engine):
    """시연 데이터를 넣으면 두 구현의 내용이 같아야 한다."""
    from app.v3.demo.seed import seed_demo

    memory_store, memory_registry = InMemoryStore(clock), InMemoryRegistry()
    sql_store, sql_registry = SqlStore(engine, clock), SqlRegistry(engine)
    for _ in range(2):  # 다시 넣어도 같다
        seed_demo(memory_store, memory_registry, "2026-11-17")
        seed_demo(sql_store, sql_registry, "2026-11-17")
    assert memory_store.list_orgs()
    assert sql_store.list_orgs() == plain(memory_store.list_orgs())
    assert sql_store.list_vendor_profiles() == plain(memory_store.list_vendor_profiles())
    for org in memory_store.list_orgs():
        assert sql_store.list_streams(org["id"]) == plain(memory_store.list_streams(org["id"]))
        mine = plain([e.to_dict() for e in memory_registry.list(org_id=org["id"])])
        assert plain([e.to_dict() for e in sql_registry.list(org_id=org["id"])]) == mine
