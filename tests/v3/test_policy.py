import pytest

from app.v3.a2a.message import Claim, Message
from app.v3.a2a.policy import (
    QuestionPolicy,
    can_open_thread,
    check_disclosure,
    permit_evidence,
)
from app.v3.demo.seed import seed_demo
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore


def q(n, stream="st_film"):
    return {"id": f"q_{n}", "streamId": stream, "field": f"f{n}", "text": f"질문 {n}"}


def test_three_per_batch_two_batches():
    policy = QuestionPolicy()
    pending = [q(n) for n in range(1, 9)]
    first = policy.next_batch(pending, [])
    assert [x["id"] for x in first] == ["q_1", "q_2", "q_3"] and {x["batch"] for x in first} == {1}
    assert policy.next_batch(pending, first) == []  # 답을 기다리는 동안 새 묶음을 내지 않는다
    history = [policy.record_answer(x, "unknown") for x in first]
    second = policy.next_batch(pending, history)
    assert [x["id"] for x in second] == ["q_4", "q_5", "q_6"] and second[0]["batch"] == 2
    history += [policy.record_answer(x, 1.5) for x in second]
    assert not policy.can_ask(history) and policy.next_batch(pending, history) == []
    left = {x["id"] for x in policy.unresolved(pending, history)}
    assert left == {"q_1", "q_2", "q_3", "q_7", "q_8"}


def test_unknown_is_valid_and_not_asked_again():
    policy = QuestionPolicy()
    history = [policy.record_answer({**q(1), "batch": 1}, "모름")]
    assert history[0]["answered"] and history[0]["unknown"] and history[0]["answer"] == "unknown"
    assert policy.next_batch([q(1)], history) == []
    again = policy.next_batch([q(1)], history, new_info=[("st_film", "f1")])
    assert [x["id"] for x in again] == ["q_1"] and again[0]["batch"] == 2
    with pytest.raises(ValueError, match="빈 답"):
        policy.record_answer(q(1), " ")


def test_no_padding_and_no_duplicates():
    policy = QuestionPolicy()
    assert policy.next_batch([], []) == []
    assert len(policy.next_batch([q(1), q(1)], [])) == 1


def test_can_open_thread():
    met = {
        "status": "calculated",
        "output": {"matched": True},
        "checks": [
            {"key": "permit", "status": "met", "evidence": ["doc:org_saegil:허가증:포장재"]}
        ],
    }
    assert can_open_thread(met)[0] is True
    assert permit_evidence(met) == ["doc:org_saegil:허가증:포장재"]
    unmet = {
        "status": "calculated",
        "output": {"matched": False},
        "checks": [
            {
                "key": "permit",
                "status": "unmet",
                "text": "포장재(LDPE)가 허가 품목에 없습니다.",
                "evidence": ["doc:org_miso:허가증:폐플라스틱"],
            }
        ],
    }
    ok, reason = can_open_thread(unmet)
    assert ok is False and "허가 품목" in reason and "LDPE" in reason
    assert can_open_thread({"status": "needs_information", "output": {}, "checks": []})[0] is False
    assert can_open_thread(None)[0] is False


@pytest.fixture
def demo():
    store, registry = InMemoryStore(lambda: "2026-11-17T00:00:00+00:00"), InMemoryRegistry()
    seed_demo(store, registry, "2026-11-17")
    return store, registry


def test_disclosure_blocks_other_emitter(demo):
    store, registry = demo

    def send(text, claims=(), streams=()):
        return Message(
            "th_1", "org_saegil", "org_hanbit", "offer", 5, text, stream_ids=streams, claims=claims
        )

    assert check_disclosure(send("화요일 노선에 함께 실을 수 있습니다."), registry, store) == []
    named = check_disclosure(send("다온산업 물량과 함께 싣겠습니다."), registry, store)
    assert [p["code"] for p in named] == ["OTHER_EMITTER_NAMED"] and named[0]["orgId"] == "org_daon"
    other = Claim(
        "c1", "number", 900, ("measure:org_daon:st_daon_pp:월발생량@2026-11-10",), "kg/월"
    )
    leaked = check_disclosure(send("같은 노선 물량은 {{c1}}입니다.", (other,)), registry, store)
    assert [p["code"] for p in leaked] == ["OTHER_EMITTER_EVIDENCE"]
    own = Claim("c1", "number", 600, ("measure:org_hanbit:st_sprue:월발생량@2026-11-10",), "kg/월")
    assert check_disclosure(send("귀사 물량은 {{c1}}입니다.", (own,)), registry, store) == []
    stream = check_disclosure(send("함께 싣습니다.", streams=("st_daon_pp",)), registry, store)
    assert stream[0]["code"] == "OTHER_EMITTER_STREAM"


def test_disclosure_follows_calc_chain(demo):
    from app.v3.evidence.model import Evidence

    store, registry = demo
    registry.put(
        Evidence(
            id="calc:route_extra_cost:abc:합계물량",
            kind="calc",
            excerpt="노선 합계",
            source="도구",
            as_of="2026-11-17",
            value=1500,
            unit="kg/월",
            meta={"inputEvidence": ["measure:org_daon:st_daon_pp:월발생량@2026-11-10"]},
        )
    )
    claim = Claim("c1", "number", 1500, ("calc:route_extra_cost:abc:합계물량",), "kg/월")
    m = Message(
        "th_1", "org_saegil", "org_hanbit", "offer", 5, "노선 합계는 {{c1}}입니다.", claims=(claim,)
    )
    assert check_disclosure(m, registry, store)[0]["code"] == "OTHER_EMITTER_EVIDENCE"
    # 배출 기업이 업체에 보내는 메시지는 이 검사의 대상이 아니다
    back = Message("th_1", "org_hanbit", "org_saegil", "answer", 4, "네.")
    assert check_disclosure(back, registry, store) == []


def test_can_open_thread_matches_real_match_permit_output(demo):
    """judge.match_permit이 실제로 내는 결과를 그대로 넣어 본다."""
    from app.v3.demo.seed import share_evidence
    from app.v3.judge.toolbox import call_tool

    store, registry = demo
    today = "2026-11-17"

    def permit(stream_id, vendor_id, vendor=None):
        profile = vendor or store.get_vendor_profile(vendor_id)
        for item in profile.get("permits") or []:
            for evidence_id in item["evidence"]:
                share_evidence(registry, evidence_id, "org_hanbit")  # 허가증은 대조 전에 연다
        return call_tool(
            "match_permit",
            {"stream": store.get_stream(stream_id), "vendor": profile},
            registry=registry,
            org_id="org_hanbit",
            today=today,
        )

    film_miso = permit("st_film", "org_miso")
    ok, reason = can_open_thread(film_miso)
    assert ok is False and "허가 품목" in reason
    assert permit_evidence(film_miso) == ["human:org_miso:허가품목-폐플라스틱"]

    film_saegil = permit("st_film", "org_saegil")
    assert can_open_thread(film_saegil)[0] is True
    assert permit_evidence(film_saegil) == ["doc:org_saegil:허가증:포장재"]

    no_permit = {**store.get_vendor_profile("org_miso"), "permits": []}
    unknown = permit("st_film", "org_miso", no_permit)
    assert unknown["status"] == "needs_information"
    ok, reason = can_open_thread(unknown)
    assert ok is False and "확인하지 못해" in reason
