"""가입하지 않은 업체 후보(결과 한 장의 outsideVendors)와 견적 요청서. 시연 시나리오를 규칙 정책으로 돌린다."""

import pytest

from app.v3 import service
from app.v3.evidence.validator import find_numbers
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, make_app, start


@pytest.fixture(scope="module")
def waiting():
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    return app, view


def test_result_lists_outside_candidates_per_stream_with_evidence(waiting):
    app, view = waiting
    outside = view["result"]["outsideVendors"]
    assert outside["source"]["url"].startswith("https://www.data.go.kr/")
    assert {s["streamId"] for s in outside["streams"]} == {s["id"] for s in view["streams"]}
    candidates = [c for s in outside["streams"] for c in s["candidates"]]
    assert candidates, "시연 회사(녹산) 근처에 폐합성수지 재활용업 후보가 있어야 한다"
    names = {app.store.get_org(p["orgId"])["name"] for p in app.store.list_vendor_profiles()}
    for candidate in candidates:
        assert candidate["name"] not in names  # 가입 업체는 후보에 없다
        assert candidate["match"] in ("confirmed", "check")
        ref = app.registry.get(candidate["evidence"][0], HANBIT)
        assert ref.kind == "ref" and candidate["name"] in ref.excerpt
        distance = candidate["distance"]
        assert distance["unit"] == "km" and distance["evidence"][0].startswith("calc:")
        assert app.registry.get(distance["evidence"][0], HANBIT).value == distance["value"]


def test_vendor_view_does_not_carry_outside_candidates(waiting):
    app, view = waiting
    vendor_view = service.get_view(app, view["session"]["id"], SAEGIL)
    assert "outsideVendors" not in (vendor_view["result"] or {})


def test_quote_request_passes_the_validator_and_keeps_contacts_out_of_the_text(waiting):
    app, view = waiting
    stream = next(s for s in view["result"]["outsideVendors"]["streams"] if s["candidates"])
    candidate = stream["candidates"][0]
    letter = service.quote_request(
        app, view["session"]["id"], HANBIT, stream["streamId"], candidate["id"]
    )
    assert letter["validation"]["ok"], letter["validation"]
    assert letter["recipient"]["name"] == candidate["name"]
    assert letter["recipient"]["evidence"] == candidate["evidence"]
    assert "견적을 여쭙니다" in letter["text"] and "{{monthlyKg}}" in letter["text"]
    assert candidate["address"] not in letter["text"]
    assert not find_numbers(letter["text"])  # 숫자는 모두 자리표시자다
    assert (
        "확인 필요"
        not in letter["rendered"].split("여쭙는 것")[0].split("한 달 발생량")[1].splitlines()[0]
    )


def test_quote_request_is_for_the_emitter_and_listed_candidates_only(waiting):
    app, view = waiting
    sid = view["session"]["id"]
    stream = next(s for s in view["result"]["outsideVendors"]["streams"] if s["candidates"])
    candidate = stream["candidates"][0]
    with pytest.raises(service.ServiceError) as err:
        service.quote_request(app, sid, SAEGIL, stream["streamId"], candidate["id"])
    assert err.value.code in ("FORBIDDEN", "SESSION_NOT_FOUND")
    with pytest.raises(service.ServiceError) as err:
        service.quote_request(app, sid, HANBIT, stream["streamId"], "pv_none")
    assert err.value.code == "NOT_FOUND"


def test_quote_request_opens_in_the_read_only_demo_app(waiting):
    """체험하기는 저장소와 레지스트리에 쓰지 못한다. 요청서는 이미 있는 근거만 읽어 만든다."""
    from app.v3.demo.scope import demo_app

    app, view = waiting
    stream = next(s for s in view["result"]["outsideVendors"]["streams"] if s["candidates"])
    candidate = stream["candidates"][0]
    letter = service.quote_request(
        demo_app(app), view["session"]["id"], HANBIT, stream["streamId"], candidate["id"]
    )
    assert letter["validation"]["ok"] and letter["recipient"]["evidence"] == candidate["evidence"]
