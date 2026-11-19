import pytest

from app.v3.a2a.message import Claim, Message
from app.v3.a2a.thread import (
    ApprovalRequired,
    InvalidTransition,
    NotParty,
    Thread,
    ThreadError,
    TurnLimitReached,
)

E, V = "org_hanbit", "org_saegil"


def thread(**kw):
    return Thread("th_1", "ses_1", E, V, stream_ids=("st_sprue",), **kw)


def msg(sender, kind, stage=4, text="문의드립니다.", **kw):
    recipient = V if sender == E else E
    return Message("th_1", sender, recipient, kind, stage, text, **kw)


def opened():
    t = thread()
    t.append(msg(E, "inquiry"), now="2026-11-17T00:00:00+00:00")
    return t


def test_starts_with_emitter_inquiry():
    t = thread()
    with pytest.raises(InvalidTransition):
        t.append(msg(V, "offer"))
    with pytest.raises(InvalidTransition):
        t.append(msg(E, "question"))
    first = t.append(msg(E, "inquiry"), now="2026-11-17T00:00:00+00:00")
    assert (t.status, first.seq, first.created_at) == ("open", 1, "2026-11-17T00:00:00+00:00")
    assert t.turns == 1


def test_only_parties_and_known_streams():
    t = opened()
    with pytest.raises(NotParty):
        t.append(Message("th_1", "org_miso", E, "offer", 5, "제안"))
    with pytest.raises(NotParty):
        t.append(Message("th_1", V, "org_daon", "offer", 5, "제안"))
    with pytest.raises(ThreadError):
        t.append(Message("th_9", V, E, "offer", 5, "제안"))
    with pytest.raises(ThreadError):
        t.append(msg(V, "offer", stream_ids=("st_daon_pp",)))


def test_emitter_cannot_offer_and_vendor_cannot_inquire():
    t = opened()
    with pytest.raises(InvalidTransition):
        t.append(msg(E, "offer"))
    with pytest.raises(InvalidTransition):
        t.append(msg(V, "inquiry"))


def test_decline_closes_with_reason_and_evidence():
    t = opened()
    claim = Claim("c1", "clause", "영업대상 폐기물", ("doc:org_saegil:허가증:폐플라스틱",))
    t.append(msg(V, "decline", text="허가 품목 밖입니다: {{c1}}", claims=(claim,)))
    assert t.status == "declined" and t.closed
    assert t.reason_evidence == ("doc:org_saegil:허가증:폐플라스틱",)
    with pytest.raises(InvalidTransition):
        t.append(msg(E, "question"))


def test_turn_limit_forces_handoff():
    t = thread(max_turns=4)
    t.append(msg(E, "inquiry"))
    for sender in (V, E, V):
        t.append(msg(sender, "question" if sender == V else "answer"))
    assert t.turns == 4 and t.turn_limit_reached
    assert t.allowed_types(E) == ("handoff",)
    with pytest.raises(TurnLimitReached):
        t.append(msg(E, "answer"))
    t.append(msg(E, "handoff", text="턴 상한에 닿아 담당자에게 넘깁니다."))
    assert t.status == "handoff" and t.turns == 4 and len(t.messages) == 5


def test_default_limit_is_twelve():
    assert thread().max_turns == 12


def test_confirm_only_after_both_approvals():
    t = opened()
    with pytest.raises(InvalidTransition):
        t.mark_agreed()
    t.append(msg(V, "offer", stage=5, text="조건을 제안합니다."))
    assert t.mark_agreed().seq == 2 and t.status == "agreed"
    with pytest.raises(ApprovalRequired):
        t.append(msg(E, "confirm", stage=6))
    assert t.approve(E) is False
    assert "confirm" not in t.allowed_types(E)
    with pytest.raises(ApprovalRequired):
        t.append(msg(E, "confirm", stage=6))
    assert t.approve(V) is True
    t.append(msg(E, "confirm", stage=6, text="양쪽 승인으로 확정합니다."))
    assert t.status == "confirmed"


def test_counter_after_agreement_resets_approvals():
    t = opened()
    t.append(msg(V, "offer", stage=5))
    t.mark_agreed()
    t.approve(E)
    t.append(msg(V, "counter", stage=5))
    assert t.status == "open" and t.approvals == {"emitter": False, "vendor": False}
    with pytest.raises(InvalidTransition):
        t.approve(E)


def test_refuse_open_blocks_thread():
    t = thread()
    t.refuse_open("허가 품목에 필름이 없습니다.", ["human:org_miso:허가품목-폐플라스틱"])
    assert t.status == "not_opened" and t.blocked and t.allowed_types(E) == ()
    with pytest.raises(InvalidTransition):
        t.append(msg(E, "inquiry"))
    with pytest.raises(InvalidTransition):
        opened().refuse_open("늦었다")


def test_round_trip_matches_contract_shape():
    t = opened()
    claim = Claim("c1", "number", 320, ("doc:org_saegil:매입기준표:pc-단가",), "원/kg")
    sent = msg(V, "offer", stage=5, text="단가는 {{c1}}입니다.", claims=(claim,))
    sent.meta["validation"] = {"ok": True, "attempts": 1, "rejections": []}
    t.append(sent)
    data = t.to_dict()
    assert {
        "id",
        "vendorOrgId",
        "status",
        "statusReason",
        "reasonEvidence",
        "streamIds",
        "turns",
        "maxTurns",
        "messages",
    } <= set(data)
    m = data["messages"][1]
    assert m["id"] == "th_1_m2" and m["from"] == V and m["validation"]["ok"] is True
    assert data["messages"][0]["validation"]["ok"] is None
    again = Thread.from_dict(data)
    assert again.to_dict() == data and again.messages[1].claims == (claim,)
