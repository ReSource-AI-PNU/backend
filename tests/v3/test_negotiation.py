"""⑥ 합의 전 단가 협상(2026-11-19): 회사 측이 요청 폭만큼 나은 단가를 한 번 요청하고, 업체 측이 자기 범위 안에서 답한다."""

from app.v3 import service
from app.v3.a2a.message import Message
from app.v3.evidence.validator import validate_message
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    TODAY,
    approve_all,
    make_app,
    to_approval,
)


def by_stream(view):
    return {a["streamId"]: a for a in view["result"]["assignments"]}


def test_company_asks_once_and_vendors_answer_inside_their_ceiling():
    app = make_app(negotiate=True)
    view = to_approval(app)
    assert view["session"]["status"] == "awaiting_approval"
    price = {sid: a["unitPrice"]["value"] for sid, a in by_stream(view).items()}
    # 요청 폭 10%: 미소 200 → 220(상한 230 안), 새길 필름 150 → 165, 스프루 320 → 요청 352 대신 상한 340,
    # 남해 처리비 80 → 72(상한 처리비 50보다 큰 처리비라 받아들임)
    assert price == {"st_defect": 220.0, "st_film": 165.0, "st_sprue": 340.0, "st_purge": -72.0}
    # 화면 뷰는 메시지 meta를 다 싣지 않으므로 저장소의 대화로 본다
    for thread in app.store.list_threads(session_id=view["session"]["id"]):
        if thread["status"] != "agreed":
            continue
        messages = thread["messages"]
        asked = [m for m in messages if (m.get("meta") or {}).get("priceAsks")]
        assert len(asked) == 1 and asked[0]["from"] == HANBIT and asked[0]["type"] == "question"
        answer = next(m for m in messages if m["seq"] == asked[0]["seq"] + 1)
        assert (
            answer["from"] == thread["vendorOrgId"]
            and "단가 조정 요청을 검토했습니다" in answer["text"]
        )
    saegil = next(
        t for t in view["threads"] if t["vendorOrgId"] == SAEGIL and t["status"] == "agreed"
    )
    assert "정해 둔 범위 안에서" in saegil["messages"][-1]["text"]  # 스프루는 일부만 받아들였다


def test_negotiated_prices_flow_into_the_result_without_a_price_mismatch():
    plain = to_approval(make_app())
    app = make_app(negotiate=True)
    view = to_approval(app)
    gained = view["result"]["totals"]["difference"]["value"]
    assert gained > plain["result"]["totals"]["difference"]["value"]
    for assignment in view["result"]["assignments"]:
        assert all(c["key"] != "price_differs" for c in assignment["checks"])
        first = assignment["unitPrice"]["evidence"][0]
        assert first.startswith("calc:negotiate_reply:"), first
    assert any(a["id"] == "assumption:negotiation.askPct@v1" for a in view["result"]["assumptions"])
    # 상한은 업체만의 값이다. 협상 단가 계산의 입력에 싣지 않는다
    reply = app.registry.get(by_stream(view)["st_sprue"]["unitPrice"]["evidence"][0], HANBIT)
    assert (
        "ceiling" not in str(reply.meta.get("inputs"))
        and 340 not in reply.meta["inputs"]["args"].values()
    )


def test_every_negotiation_message_passes_the_validator_and_rerun_does_not_ask_again():
    app = make_app(negotiate=True)
    view = to_approval(app)
    sid = view["session"]["id"]
    for raw in app.store.list_threads(session_id=sid):
        for item in raw["messages"]:
            message = Message.from_dict({**item, "threadId": raw["id"]})
            result = validate_message(message, app.registry, today=TODAY)
            assert result.ok, (item["type"], [r.to_dict() for r in result.rejections])
    before = sum(
        1
        for t in app.store.list_threads(session_id=sid)
        for m in t["messages"]
        if (m.get("meta") or {}).get("priceAsks")
    )
    again = service.run_session(app, sid, HANBIT)
    after = sum(
        1
        for t in app.store.list_threads(session_id=sid)
        for m in t["messages"]
        if (m.get("meta") or {}).get("priceAsks")
    )
    assert before == after == 3 and again["session"]["status"] == "awaiting_approval"


def test_a_vendor_without_a_ceiling_keeps_its_first_price():
    app = make_app(negotiate=True)
    profile = app.store.get_vendor_profile(MISO)
    profile["guardrails"].pop("ceilingPriceKrwPerKg")
    app.store.put_vendor_profile(profile)
    view = to_approval(app)
    assert by_stream(view)["st_defect"]["unitPrice"]["value"] == 200.0
    miso = next(t for t in view["threads"] if t["vendorOrgId"] == MISO and t["status"] == "agreed")
    assert "더 조정하기 어렵습니다" in miso["messages"][-1]["text"]


def test_documents_after_approval_carry_the_negotiated_price():
    app = make_app(negotiate=True)
    done = approve_all(app, to_approval(app))
    assert done["session"]["status"] == "confirmed"
    request = next(
        d
        for d in done["documents"]
        if d["kind"] == "request" and NAMHAE in str(d.get("title")) or "남해" in str(d.get("title"))
    )
    assert request["validation"]["ok"] and "합의 처리비 72원/kg" in request["rendered"]
