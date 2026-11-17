"""저장소의 진행 기록(activity)과 실행 자리(runs): 메모리와 SQL(SQLite 파일)이 같게 돈다."""

from __future__ import annotations

import time

import pytest

from app.v3.store import InMemoryStore


@pytest.fixture(params=["memory", "sql"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore(lambda: "2026-11-17T00:00:00+00:00")
        return
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine

    from app.v3.sql import SqlStore, create_tables

    engine = create_engine(
        f"sqlite:///{tmp_path / 'v3.db'}", connect_args={"check_same_thread": False}
    )
    create_tables(engine)
    yield SqlStore(engine, lambda: "2026-11-17T00:00:00+00:00")
    engine.dispose()


def test_activity_and_run_lease_behave_the_same_in_both_stores(store):
    first = store.add_activity("ses_1", {"event": "activity", "kind": "drafting", "text": "가"})
    second = store.add_activity("ses_1", {"event": "status", "status": "running"})
    store.add_activity("ses_2", {"event": "activity", "kind": "sent", "text": "나"})
    assert (first["seq"], second["seq"]) == (1, 2) and first["at"] == "2026-11-17T00:00:00+00:00"
    assert [e["seq"] for e in store.list_activity("ses_1")] == [1, 2]
    assert store.list_activity("ses_1", after=1) == [second]
    assert store.list_activity("ses_1", after=2) == [] and store.list_activity("nope") == []
    now = time.time()
    run = {"runId": "run_1", "mode": "async", "startedAt": "", "heartbeat": now}
    assert store.claim_run("ses_1", run, now - 60) is None
    other = {**run, "runId": "run_2"}
    assert store.claim_run("ses_1", other, now - 60) == run  # 살아 있는 자리는 그대로
    assert store.touch_run("ses_1", "run_1", now + 5) is True
    assert store.touch_run("ses_1", "run_2", now + 5) is False
    assert store.get_run("ses_1")["heartbeat"] == now + 5
    store.release_run("ses_1", "run_2")  # 남의 자리는 비우지 않는다
    assert store.get_run("ses_1")["runId"] == "run_1"
    assert store.claim_run("ses_1", other, now + 10) is None  # 멈춘 자리는 차지한다
    assert store.get_run("ses_1")["runId"] == "run_2"
    store.release_run("ses_1", "run_2")
    assert store.get_run("ses_1") is None and store.get_run("nope") is None


def test_purge_and_reset_remove_activity_and_runs(store):
    store.put_org({"id": "org_a", "name": "가", "role": "emitter"})
    store.put_session({"id": "ses_a", "emitterOrgId": "org_a", "status": "idle"})
    store.add_activity("ses_a", {"event": "activity", "kind": "sent"})
    store.claim_run("ses_a", {"runId": "run_1", "heartbeat": time.time()}, 0)
    store.purge_orgs(["org_a"])
    assert store.list_activity("ses_a") == [] and store.get_run("ses_a") is None
    store.add_activity("ses_b", {"event": "activity", "kind": "sent"})
    store.reset()
    assert store.list_activity("ses_b") == []
