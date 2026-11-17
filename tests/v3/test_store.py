import pytest

from app.v3.a2a.message import Message
from app.v3.a2a.thread import Thread
from app.v3.store import InMemoryStore, JsonFileStore, NotFound, VersionConflict

CLOCK = "2026-11-17T00:00:00+00:00"


def make(store):
    store.put_org({"id": "org_a", "name": "가", "role": "emitter"})
    store.put_org({"id": "org_b", "name": "나", "role": "emitter"})
    store.put_org({"id": "org_v", "name": "다", "role": "vendor"})
    store.put_stream({"id": "st_a1", "orgId": "org_a", "monthlyKg": 600})
    store.put_stream({"id": "st_a2", "orgId": "org_a", "monthlyKg": 100})
    store.put_stream({"id": "st_b1", "orgId": "org_b", "monthlyKg": 900})
    return store


@pytest.fixture
def store():
    return make(InMemoryStore(lambda: CLOCK))


def test_reads_are_deep_copies(store):
    stream = store.get_stream("st_a1")
    stream["monthlyKg"] = 1
    store.list_streams("org_a")[0]["monthlyKg"] = 2
    assert store.get_stream("st_a1")["monthlyKg"] == 600
    source = {"id": "st_a3", "orgId": "org_a", "measure": {"contaminationPct": 1}}
    store.put_stream(source)
    source["measure"]["contaminationPct"] = 99
    assert store.get_stream("st_a3")["measure"]["contaminationPct"] == 1


def test_org_boundary_on_streams(store):
    assert [s["id"] for s in store.list_streams("org_a")] == ["st_a1", "st_a2"]
    assert [s["id"] for s in store.list_streams("org_b")] == ["st_b1"]
    assert store.streams_visible_to("org_v") == []
    with pytest.raises(NotFound):
        store.get_stream("st_a1", "org_b")
    with pytest.raises(NotFound):
        store.get_stream("st_a1", "org_v")
    thread = Thread("th_1", "ses_1", "org_a", "org_v", stream_ids=("st_a1",))
    store.put_thread(thread)
    assert store.streams_visible_to("org_v") == []  # 아직 열리지 않았다
    thread.append(Message("th_1", "org_a", "org_v", "inquiry", 4, "문의"), now=store.now())
    store.put_thread(thread)
    assert [s["id"] for s in store.streams_visible_to("org_v")] == ["st_a1"]
    assert store.get_stream("st_a1", "org_v")["id"] == "st_a1"
    with pytest.raises(NotFound):
        store.get_stream("st_a2", "org_v")
    assert [t["id"] for t in store.list_threads(org_id="org_v")] == ["th_1"]
    assert store.list_threads(org_id="org_b") == []
    assert Thread.from_dict(store.get_thread("th_1")).status == "open"


def test_session_versions(store):
    saved = store.put_session(
        {"id": store.next_id("ses"), "emitterOrgId": "org_a", "status": "idle"}
    )
    assert (saved["id"], saved["version"], saved["createdAt"]) == ("ses_1", 1, CLOCK)
    again = store.put_session({**saved, "status": "running"}, expected_version=1)
    assert again["version"] == 2
    with pytest.raises(VersionConflict):
        store.put_session({**saved, "status": "idle"}, expected_version=1)
    assert store.next_id("ses") == "ses_2" and store.next_id("th") == "th_1"
    assert [s["id"] for s in store.list_sessions("org_a")] == ["ses_1"]
    assert store.list_sessions("org_b") == []
    store.put_thread(Thread("th_1", "ses_1", "org_a", "org_v"))
    assert [s["id"] for s in store.list_sessions("org_v")] == ["ses_1"]
    with pytest.raises(NotFound):
        store.get_session("ses_9")


def test_questions_documents_events(store):
    store.put_question("ses_1", {"id": "q_1", "batch": 1, "answered": False})
    store.put_question("ses_1", {"id": "q_1", "batch": 1, "answered": True})
    assert store.list_questions("ses_1") == [{"id": "q_1", "batch": 1, "answered": True}]
    store.put_document("ses_1", {"id": "doc_1", "kind": "request", "title": "요청서"})
    assert store.list_documents("ses_1")[0]["kind"] == "request" and store.list_documents("x") == []
    first = store.add_event(
        "ses_1", "tool", "tool_call", "경로 판정", detail={"tool": "judge_route"}
    )
    second = store.add_event("ses_1", "validator", "validation", "통과", thread_id="th_1")
    assert (first["seq"], second["seq"], second["threadId"], first["at"]) == (1, 2, "th_1", CLOCK)
    assert set(first) == {"seq", "at", "actor", "kind", "threadId", "summary", "detail"}
    assert [e["seq"] for e in store.list_events("ses_1")] == [1, 2]


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


def test_json_file_store_persists(tmp_path):
    path = tmp_path / "state" / "store.json"
    first = make(JsonFileStore(path, lambda: CLOCK))
    first.put_vendor_profile({"orgId": "org_v", "permits": []})
    first.add_event("ses_1", "system", "message", "저장 확인")
    second = JsonFileStore(path, lambda: CLOCK)
    assert second.snapshot() == first.snapshot()
    assert second.get_vendor_profile("org_v") == {"orgId": "org_v", "permits": []}
    assert [o["id"] for o in second.list_orgs(role="emitter")] == ["org_a", "org_b"]
    second.reset()
    assert JsonFileStore(path).list_orgs() == []
