import pytest

from app.v3 import ids
from app.v3.a2a.message import Claim, Message
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, InMemoryRegistry


def make(eid="assumption:현행처리-혼합소각@v1", **kw):
    base = {
        "id": eid,
        "kind": eid.split(":")[0],
        "excerpt": "가정",
        "source": "팀 가정",
        "as_of": "2026-11-16",
    }
    return Evidence(**{**base, **kw})


def test_evidence_id_must_match_kind():
    with pytest.raises(ValueError):
        Evidence(id="ref:x", kind="law", excerpt="", source="", as_of="2026-01-01")


def test_registry_access_and_versioning():
    reg = InMemoryRegistry()
    private = reg.put(make("doc:org_a:계약서:단가", value=250, unit="원/kg", access=("org_a",)))
    assert reg.get(private.id, "org_a").value == 250
    with pytest.raises(EvidenceForbidden):
        reg.get(private.id, "org_b")
    with pytest.raises(EvidenceNotFound):
        reg.get("doc:org_a:없음:x", "org_a")
    assert (
        reg.put(make("doc:org_a:계약서:단가", value=250, unit="원/kg", access=("org_a",))).version
        == 1
    )
    assert (
        reg.put(make("doc:org_a:계약서:단가", value=260, unit="원/kg", access=("org_a",))).version
        == 2
    )
    assert [e.id for e in reg.list(org_id="org_b")] == []


def test_expiry_uses_iso_dates():
    ev = make(valid_until="2026-11-30")
    assert not ev.expired("2026-11-30")
    assert ev.expired("2026-12-01")


def test_calc_id_is_deterministic():
    a = ids.calc_id("cost_carbon", {"b": 2, "a": 1}, "월손익")
    b = ids.calc_id("cost_carbon", {"a": 1, "b": 2}, "월손익")
    assert a == b and a.startswith("calc:cost_carbon:")


def test_message_round_trip():
    msg = Message(
        "th_1",
        "org_a",
        "org_b",
        "offer",
        5,
        "단가는 {{c1}}입니다.",
        stream_ids=("st_sprue",),
        claims=(Claim("c1", "number", 300, ("doc:org_b:매입기준표:PP-A",), "원/kg"),),
    )
    assert Message.from_dict(msg.to_dict()) == msg
    with pytest.raises(ValueError):
        Message("th_1", "a", "b", "hello", 1, "")
