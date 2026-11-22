"""가입하지 않은 업체 후보(결과 한 장의 outsideVendors)와 견적 요청서. 시연 시나리오를 규칙 정책으로 돌린다."""

import pytest

from app.v3 import outreach, service
from app.v3.evidence.validator import find_numbers
from app.v3.public_vendors import treatment
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


def test_outside_candidates_show_the_2024_treatment_with_ref_evidence(waiting):
    """결과표 한 줄의 처리량·처리능력은 claim이고, 첫 근거는 그 값의 ref, 둘째는 허가 명부 한 줄이다."""
    app, view = waiting
    outside = view["result"]["outsideVendors"]
    source = outside["source"]["treatment2024"]
    assert source["asOf"] == "2024-12-31" and source["published"] == "2025-12-31"
    assert source["url"].startswith("https://www.recycling-info.or.kr/")
    candidates = [c for s in outside["streams"] for c in s["candidates"]]
    shown = [c for c in candidates if c["treatment2024"]]
    assert shown, "녹산 근처 폐합성수지 후보 가운데 2024년 처리 실적이 붙은 곳이 있어야 한다"
    for candidate in shown:
        t = candidate["treatment2024"]
        assert t["sheet"].startswith("8-") and t["asOf"] == "2024-12-31"
        for field, unit in (("tonsPerYear", "톤/년"), ("capacityTonsPerDay", "톤/일")):
            claim = t[field]
            if claim is None:
                continue
            assert claim["unit"] == unit and claim["evidence"][1] == candidate["evidence"][0]
            ref = app.registry.get(claim["evidence"][0], HANBIT)
            assert ref.kind == "ref" and ref.value == claim["value"] and ref.unit == unit
            assert ref.id.startswith(f"ref:처리업체현황:{t['sheet'].split('.')[0]}-{t['row']}-")
            name = ref.excerpt.split(" · ")[0]
            assert treatment.name_key(name) == treatment.name_key(candidate["name"])
            assert "2024년 처리량" in ref.excerpt and ref.meta["sheet"] == t["sheet"]
            assert ref.access == ("*",)  # 공표 자료라 누구나 본다
        assert t["evidence"] == [
            t[f]["evidence"][0] for f in ("tonsPerYear", "capacityTonsPerDay") if t[f]
        ]
    # 처리 실적은 표시용이다: 손익 계산·배정에 쓰는 claim에 결과표 근거가 섞이지 않는다
    used = str({k: v for k, v in view["result"].items() if k != "outsideVendors"})
    assert "ref:처리업체현황" not in used


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


def _shared_candidate(view):
    """불량 사출품(400kg)과 스프루·러너(600kg) 모두의 후보인 업체."""
    streams = {s["streamId"]: s for s in view["result"]["outsideVendors"]["streams"]}
    sprue = {c["id"] for c in streams["st_sprue"]["candidates"]}
    return next(c for c in streams["st_defect"]["candidates"] if c["id"] in sprue)


def _joined(parts):
    return "".join(p["text"] if p["type"] == "text" else p["display"] for p in parts)


def test_quote_request_follows_the_chosen_stream_for_the_same_vendor(waiting):
    """같은 업체에 불량 사출품 400kg과 스프루·러너 600kg을 번갈아 물어도 요청서가 각각 그 폐기물이다."""
    app, view = waiting
    sid, vendor = view["session"]["id"], _shared_candidate(view)
    letters = {}
    for stream_id in ("st_defect", "st_sprue", "st_defect"):
        letter = service.quote_request(app, sid, HANBIT, stream_id, vendor["id"])
        assert letter["validation"]["ok"], letter["validation"]
        assert (letter["streamId"], letter["vendorId"]) == (stream_id, vendor["id"])
        # 미리보기 조각을 이으면 복사할 글이다
        assert _joined(letter["parts"]) == letter["rendered"]
        if stream_id in letters:
            assert letter["rendered"] == letters[stream_id]["rendered"]  # 다시 골라도 같은 글
        letters[stream_id] = letter
    defect, sprue = letters["st_defect"]["rendered"], letters["st_sprue"]["rendered"]
    assert "- 품목: PP 사출 불량품" in defect and "- 한 달 발생량: 400 kg/월 (계량값)" in defect
    assert "- 품목: PP 스프루·러너" in sprue and "- 한 달 발생량: 600 kg/월 (계량값)" in sprue
    assert "600" not in defect and "400" not in sprue
    assert letters["st_defect"]["item"]["name"] == "PP 사출 불량품"
    assert letters["st_sprue"]["item"]["monthlyKg"]["value"] == 600


def test_quote_request_asks_settlement_terms_and_marks_where_each_value_came_from(waiting):
    app, view = waiting
    vendor = _shared_candidate(view)
    letter = service.quote_request(app, view["session"]["id"], HANBIT, "st_defect", vendor["id"])
    text = letter["rendered"]
    for ask in (
        "□ 매입(귀사가 값을 치름) □ 무상 인수 □ 유상 처리(저희가 처리비를 냄)",
        "단가: kg당 ____원",
        "방문 수거: □ 가능",
        "최소 수거량: 한 번에 ____kg 이상",
        "운반비: □ 단가에 포함 □ 별도(한 번에 ____원, 부담: □ 귀사 □ 저희)",
        "그 밖에 저희가 낼 비용",
        "예상 정산(한 달 발생량 400 kg/월 기준)",
        "□ 받는 돈 □ 내는 돈",
    ):
        assert ask in text, ask
    # 알린 값, 사진 추정, 미확인을 가른다
    assert "- 오염도: 6% (사진 추정)" in text and "- 수분: 미확인" in text
    assert "- 섞인 것: 종이 라벨, 금속 인서트 (담당자 입력)" in text
    status = {f["key"]: f["status"] for f in letter["facts"]}
    assert status["monthlyKg"] == "known" and status["contaminationPct"] == "estimated"
    assert status["moisturePct"] == "unknown"
    assert "거래나 수거를 정하는 요청이 아닙니다" in text


def test_demo_letters_keep_the_demo_mark_and_real_ones_do_not(waiting):
    from app.v3 import outreach

    app, view = waiting
    vendor = _shared_candidate(view)
    letter = service.quote_request(app, view["session"]["id"], HANBIT, "st_sprue", vendor["id"])
    assert letter["demo"] is True
    assert letter["rendered"].startswith(outreach.DEMO_MARK)
    assert letter["parts"][0]["text"].startswith(outreach.DEMO_MARK)
    emitter = app.store.get_org(HANBIT)
    stream = app.store.get_stream("st_sprue")
    real = outreach.quote_request(app.registry, emitter, stream, vendor["id"], app.today)
    assert real["demo"] is False and outreach.DEMO_MARK not in real["rendered"]
    assert real["rendered"].startswith("견적 요청서")


def test_making_a_quote_request_writes_nothing(waiting):
    """초안은 업체에 연락하지 않고, 견적을 확정하지 않고, 한 달 손익을 고치지 않는다(저장소가 그대로다)."""
    import copy

    app, view = waiting
    sid, vendor = view["session"]["id"], _shared_candidate(view)
    before = copy.deepcopy(app.store._data)
    evidence = {e.id for e in app.registry.list(org_id=None)}
    result = copy.deepcopy(service.get_view(app, sid, HANBIT)["result"]["totals"])
    for stream_id in ("st_defect", "st_sprue"):
        service.quote_request(app, sid, HANBIT, stream_id, vendor["id"])
    assert app.store._data == before
    assert {e.id for e in app.registry.list(org_id=None)} == evidence
    assert service.get_view(app, sid, HANBIT)["result"]["totals"] == result


def test_material_change_after_search_blocks_letters_to_the_plastic_candidates():
    """탐색 뒤 불량 사출품을 PP에서 알루미늄·금속 스크랩으로 고치면 플라스틱 후보에게 요청서를 만들지 않는다."""
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    sid = view["session"]["id"]
    candidates = next(
        s for s in view["result"]["outsideVendors"]["streams"] if s["streamId"] == "st_defect"
    )["candidates"]
    assert candidates
    service.update_stream(
        app, HANBIT, "st_defect", {"material": "알루미늄", "wasteItem": "금속 스크랩"}, HANBIT
    )
    for candidate in candidates:
        with pytest.raises(service.ServiceError) as err:
            service.quote_request(app, sid, HANBIT, "st_defect", candidate["id"])
        assert (err.value.code, err.value.status) == ("OUTSIDE_STALE", 409)
        assert err.value.detail["reason"] == "non_plastic"
        assert "재질이 PP에서 알루미늄으로" in err.value.message
        assert "다시 찾아 주세요" in err.value.message
    # 화면의 후보 목록에도 바뀐 칸이 붙는다(저장한 결과는 그대로다)
    entry = next(
        s
        for s in service.get_view(app, sid, HANBIT)["result"]["outsideVendors"]["streams"]
        if s["streamId"] == "st_defect"
    )
    assert [c["field"] for c in entry["changed"]] == ["material", "wasteItem"]
    assert "changed" not in app.store.get_session(sid)["work"]["outside"]["streams"][0]
    # 바뀌지 않은 폐기물은 그대로 만든다
    sprue = service.quote_request(app, sid, HANBIT, "st_sprue", candidates[0]["id"])
    assert sprue["validation"]["ok"] and sprue["changes"] == []


def test_plastic_to_plastic_change_still_fits_and_the_letter_uses_the_current_data():
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    sid = view["session"]["id"]
    candidate = next(
        c
        for s in view["result"]["outsideVendors"]["streams"]
        if s["streamId"] == "st_defect"
        for c in s["candidates"]
        if c["match"] == "confirmed"
    )
    service.update_stream(app, HANBIT, "st_defect", {"material": "PE"}, HANBIT)
    letter = service.quote_request(app, sid, HANBIT, "st_defect", candidate["id"])
    assert letter["validation"]["ok"]
    assert "- 재질: PE (담당자 입력)" in letter["rendered"]
    assert letter["changes"] == [
        {"field": "material", "label": "재질", "before": "PP", "after": "PE"}
    ]


@pytest.mark.parametrize(
    ("estimate", "shown"),
    [
        # 신뢰도가 낮거나, 등록한 재질과 다르거나, 기준이 생기기 전에 저장한 추정은 알리지 않는다
        ({"label": "목재", "confidence": 0.58, "agrees": None, "usable": False}, "- 재질: 미확인"),
        ({"label": "종이·골판지", "confidence": 0.81}, "- 재질: 미확인"),
        (
            {"label": "필름", "confidence": 0.9, "agrees": None, "usable": True},
            "- 재질: 필름 (사진 추정)",
        ),
    ],
)
def test_letter_uses_the_photo_material_only_when_it_can_be_trusted(estimate, shown):
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    candidate = next(
        c
        for s in view["result"]["outsideVendors"]["streams"]
        if s["streamId"] == "st_defect"
        for c in s["candidates"]
        if c["match"] == "confirmed"
    )
    # 재질을 비워 둔 폐기물이면 요청서가 사진 재질 분류로 재질 칸을 채우려 한다(서비스는 후보를 찾은 뒤 재질이
    # 비면 요청서를 막으므로, 요청서를 만드는 함수를 바로 부른다)
    stream = app.store.get_stream("st_defect")
    stream["material"] = ""
    stream["photo"] = {**(stream.get("photo") or {}), "materialEstimate": estimate}
    letter = outreach.quote_request(
        app.registry, app.store.get_org(HANBIT), stream, candidate["id"], app.today
    )
    assert shown in letter["rendered"]
