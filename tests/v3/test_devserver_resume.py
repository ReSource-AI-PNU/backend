"""--store 재시작: 저장소와 근거 레지스트리를 파일에서 읽어 이전 진단을 이어 간다."""

import json

import pytest

from app.v3 import devserver, service
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import (
    EvidenceForbidden,
    EvidenceNotFound,
    InMemoryRegistry,
    JsonFileRegistry,
)
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    MISO_VISITS,
    SAEGIL,
    TODAY,
    approve_all,
    recheck,
    to_approval,
)


def evidence(evidence_id="doc:org_a:기준표:단가", value=320.0, **over):
    base = {
        "id": evidence_id,
        "kind": evidence_id.split(":", 1)[0],
        "label": "단가(가상)",
        "value": value,
        "unit": "원/kg",
        "excerpt": "단가 가상 값",
        "source": "가상 문서",
        "as_of": TODAY,
        "access": ("org_a",),
        "meta": {"ownerOrgId": "org_a", "parts": ("a", "b"), "inputs": {"n": 1}},
    }
    return Evidence(**{**base, **over})


# ── JsonFileRegistry ─────────────────────────────────────────────────
def test_file_registry_behaves_like_the_memory_one(tmp_path):
    path = tmp_path / "deep" / "evidence.json"
    file_registry, memory = JsonFileRegistry(path), InMemoryRegistry()
    for registry in (file_registry, memory):
        first = registry.put(evidence())
        assert first.version == 1 and registry.put(evidence()) is first  # 같은 내용이면 그대로
        assert registry.put(evidence(value=330.0)).version == 2  # 내용이 바뀌면 판이 오른다
        registry.put(evidence("law:폐기물관리법:제17조@2026-01-01", None, unit=None, access=("*",)))
        assert registry.get("doc:org_a:기준표:단가", "org_a").value == 330.0
        with pytest.raises(EvidenceForbidden):
            registry.get("doc:org_a:기준표:단가", "org_b")
        with pytest.raises(EvidenceNotFound):
            registry.get("doc:없음", "org_a")
        assert [e.id for e in registry.list(org_id="org_b")] == [
            "law:폐기물관리법:제17조@2026-01-01"
        ]
        assert registry.exists("doc:org_a:기준표:단가")
    assert [e.id for e in file_registry.list()] == [e.id for e in memory.list()]


def test_file_registry_reloads_with_versions_and_writes_only_changes(tmp_path):
    path = tmp_path / "evidence.json"
    first = JsonFileRegistry(path)
    first.put(evidence())
    first.put(evidence(value=330.0))
    assert json.loads(path.read_text(encoding="utf-8"))["evidence"][0]["version"] == 2
    again = JsonFileRegistry(path)
    loaded = again.get("doc:org_a:기준표:단가", "org_a")
    assert loaded.value == 330.0 and loaded.version == 2 and loaded.access == ("org_a",)
    assert loaded.meta == {"ownerOrgId": "org_a", "parts": ["a", "b"], "inputs": {"n": 1}}
    # 파일에서 읽은 것과 같은 내용을 다시 넣어도 판이 오르지 않고 파일도 다시 쓰지 않는다
    stamp = path.stat().st_mtime_ns
    assert again.put(evidence(value=330.0)).version == 2
    assert path.stat().st_mtime_ns == stamp
    again.clear()
    assert JsonFileRegistry(path).list() == []
    assert not list(tmp_path.glob("*.tmp"))


# ── 로컬 서버 ────────────────────────────────────────────────────────
def test_evidence_file_sits_next_to_the_store_file(tmp_path):
    assert devserver.evidence_file(str(tmp_path / "demo.json")) == tmp_path / "demo.evidence.json"


def test_restart_with_the_same_store_continues_the_diagnosis(tmp_path):
    store_path = str(tmp_path / "demo.json")
    first = devserver.build_app(store_path=store_path, today=TODAY)
    assert first.resumed is False  # 파일이 없었으므로 시연 시작 상태
    # 미소환경의 노선 밖 수거 답(가상). 없으면 운반비 미정이라 그 대화는 확정할 수 없다(계약 검토 담당)
    service.update_vendor(first, MISO, {"transport": {"offRoute": dict(MISO_VISITS)}}, MISO)
    waiting = to_approval(first)
    sid = waiting["session"]["id"]
    assert waiting["session"]["status"] == "awaiting_approval"
    thread = next(t for t in waiting["threads"] if t["vendorOrgId"] == SAEGIL and t["messages"])
    service.approve(first, sid, thread["id"], HANBIT)  # 한쪽만 승인한 채로 서버를 내린다
    before = service.get_view(first, sid, HANBIT)
    count = len(first.registry.list(org_id=None))
    del first

    second = devserver.build_app(store_path=store_path, today=TODAY)
    assert second.resumed is True
    assert type(second.registry).__name__ == "JsonFileRegistry"
    assert len(second.registry.list(org_id=None)) == count  # 시드로 덮어쓰지 않았다
    after = service.get_view(second, sid, HANBIT)
    assert after == before  # 메시지, 결과 한 장, 승인, 실행 기록이 그대로다
    checked, passed = recheck(second, after)
    assert checked > 0 and passed == checked  # 인용한 근거를 다시 읽어 검증할 수 있다
    for item in after["result"]["assignments"]:
        for key in ("unitPrice", "monthlyAfter", "difference"):
            if item[key]:
                assert service.get_evidence(second, item[key]["evidence"][0], HANBIT)["evidence"]
    # 이어서 승인하면 확정되고 문서가 만들어진다
    done = approve_all(second, after)
    assert done["session"]["status"] == "confirmed" and len(done["documents"]) >= 2
    assert all(d["validation"]["ok"] for d in done["documents"])

    third = devserver.build_app(store_path=store_path, today=TODAY)  # 한 번 더 내렸다 올려도 같다
    assert third.resumed and service.get_view(third, sid, HANBIT) == done
    # 새 진단도 번호가 이어진다
    assert service.create_session(third, HANBIT, HANBIT)["session"]["id"] != sid


def test_reset_flag_and_missing_evidence_file_start_fresh(tmp_path):
    store_path = str(tmp_path / "demo.json")
    first = devserver.build_app(store_path=store_path, today=TODAY)
    sid = to_approval(first)["session"]["id"]
    fresh_count = len(devserver.build_app(today=TODAY).registry.list(org_id=None))
    assert len(first.registry.list(org_id=None)) > fresh_count  # 진단이 근거를 더했다

    reset = devserver.build_app(store_path=store_path, today=TODAY, reset=True)
    assert reset.resumed is False and reset.store.list_sessions() == []
    assert len(reset.registry.list(org_id=None)) == fresh_count  # 이전 실행의 근거가 남지 않는다
    with pytest.raises(service.ServiceError):
        service.get_view(reset, sid, HANBIT)

    # 저장소 파일만 있고 근거 파일이 없으면 이어 갈 수 없다
    to_approval(reset)
    devserver.evidence_file(store_path).unlink()
    alone = devserver.build_app(store_path=store_path, today=TODAY)
    assert alone.resumed is False and alone.store.list_sessions() == []
    assert len(alone.registry.list(org_id=None)) == fresh_count

    # 이어 간 뒤의 시연 되돌리기는 두 파일을 모두 처음 상태로 돌린다
    to_approval(alone)
    resumed = devserver.build_app(store_path=store_path, today=TODAY)
    assert resumed.resumed is True and resumed.store.list_sessions()
    service.reset_demo(resumed)
    after = devserver.build_app(store_path=store_path, today=TODAY)
    assert after.store.list_sessions() == []
    assert len(after.registry.list(org_id=None)) == fresh_count


def test_main_accepts_the_reset_flag(tmp_path, monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_build(**options):
        seen.update(options)
        raise Stop

    monkeypatch.setattr(devserver, "build_app", fake_build)
    with pytest.raises(Stop):
        devserver.main(["--store", str(tmp_path / "s.json"), "--reset", "--demo-closed"])
    assert seen["reset"] is True and seen["demo_open"] is False
    with pytest.raises(Stop):
        devserver.main([])
    assert seen["reset"] is False and seen["demo_open"] is True and seen["store_path"] is None
