"""가입하지 않은 처리 업체 명부(공공데이터) 검색."""

from __future__ import annotations

from app.v3 import public_vendors as pv

NOKSAN = (128.8550, 35.0925)  # 부산 강서구 녹산국가산업단지 [경도, 위도]
CHANGWON = (128.6811, 35.2280)


def test_registry_keeps_only_recyclers_with_coordinates():
    data = pv.registry()
    assert data["count"] == len(data["vendors"]) > 1000
    assert all("재활용업" in v["business"] for v in data["vendors"])
    assert all(33 <= v["lat"] <= 39 and 124 <= v["lon"] <= 132 for v in data["vendors"])
    assert len({v["id"] for v in data["vendors"]}) == len(data["vendors"])
    assert data["url"].startswith("https://www.data.go.kr/")


def test_bulk_filled_codes_are_not_trusted():
    """김해시는 거의 모든 업체에 51-03-01만, 부산 강서구는 00-00-00만 적었다(2026-11 원본)."""
    vendors = pv.registry()["vendors"]
    gimhae = [v for v in vendors if (v.get("agency") or "").startswith("경상남도 김해시")]
    gangseo = [v for v in vendors if v.get("agency") == "부산광역시 강서구"]
    assert gimhae and not any(v["codesReliable"] for v in gimhae if v["codes"] == ["51-03-01"])
    assert gangseo and not any(v["codesReliable"] for v in gangseo)


def test_code_names_follow_the_waste_classification_table():
    assert pv.code_name("51-03-01") == "폐합성수지류(폐염화비닐수지류는 제외한다)"
    assert pv.code_name("51-03-00") == "폐합성고분자화합물"  # 이 판에 없는 번호는 상위 분류 이름
    assert pv.code_name("99-99-99") is None


def test_stream_codes_by_material_and_form():
    assert pv.stream_codes({"material": "PP"}) == ["51-03-01", "51-03-00"]
    assert pv.stream_codes({"material": "PVC"}) == ["51-03-03", "51-03-00"]
    assert pv.stream_codes({"material": "PS", "form": "발포 완충재"}) == ["51-03-06", "51-03-00"]
    film = {"material": "LDPE", "origin": "PCR", "name": "입고 포장 필름"}
    assert pv.stream_codes(film) == ["51-03-01", "51-03-07", "51-03-00"]
    assert pv.stream_codes({"material": None}) == []


def test_search_ranks_confirmed_permits_first_then_nearby_name_matches():
    result = pv.search({"material": "PP", "origin": "PIR"}, NOKSAN)
    candidates = result["candidates"]
    confirmed = [c for c in candidates if c["match"] == "confirmed"]
    check = [c for c in candidates if c["match"] == "check"]
    assert 1 <= len(confirmed) <= pv.LIMIT and len(check) <= pv.CHECK_LIMIT
    assert candidates[: len(confirmed)] == confirmed
    assert [c["distanceKm"] for c in confirmed] == sorted(c["distanceKm"] for c in confirmed)
    assert all(c["matchedCodes"] for c in confirmed)
    # 확인 필요 후보는 허가 정보로 맞는 가장 먼 곳보다 가깝고, 상호에 플라스틱을 다루는 말이 있다
    assert all(c["distanceKm"] < confirmed[-1]["distanceKm"] for c in check)
    assert all(pv.PLASTIC_NAME.search(c["name"]) and not c["matchedCodes"] for c in check)
    assert all(c["distanceKm"] <= pv.RADIUS_KM for c in candidates)
    assert len({pv._norm(c["name"]) for c in candidates}) == len(candidates)
    assert result["source"] == "공공데이터포털 전국폐기물처리업소표준데이터"


def test_search_skips_registered_vendors_and_explains_missing_inputs():
    first = pv.search({"material": "PP"}, CHANGWON)["candidates"][0]
    again = pv.search({"material": "PP"}, CHANGWON, exclude_names=(f"(주) {first['name']}",))
    assert first["id"] not in {c["id"] for c in again["candidates"]}
    assert pv.search({"material": "PP"}, None)["reason"] == "회사 위치가 없어 찾지 않았습니다."
    assert pv.search({}, CHANGWON)["reason"] == "재질이 확인되지 않아 찾지 않았습니다."


def test_non_plastic_streams_get_no_plastic_recyclers():
    """알루미늄·금속 스크랩·폐목재는 폐합성수지 분류번호로 찾지 않는다(전에는 재질이 있으면 모두 플라스틱으로 봤다)."""
    for stream in (
        {"material": "알루미늄", "wasteItem": "금속 스크랩"},
        {"material": "알루미늄"},
        {"material": "철", "wasteItem": "금속 스크랩"},
        {"material": "PP", "wasteItem": "금속 스크랩"},
        {"material": "목재", "wasteItem": "폐목재"},
    ):
        assert pv.stream_codes(stream) == [], stream
        result = pv.search(stream, NOKSAN)
        assert result["candidates"] == [] and result["reason"] == pv.NON_PLASTIC_REASON
    assert pv.stream_codes({"material": "PP", "wasteItem": "폐플라스틱"}) == [
        "51-03-01",
        "51-03-00",
    ]


def test_recheck_says_whether_a_candidate_still_fits_the_current_stream():
    pp = {"material": "PP", "origin": "PIR"}
    found = pv.search(pp, NOKSAN)["candidates"]
    confirmed = next(c for c in found if c["match"] == "confirmed")
    check = next(c for c in found if c["match"] == "check")
    assert pv.recheck(pp, confirmed["id"], NOKSAN) == {
        "ok": True,
        "reason": None,
        "match": "confirmed",
        "matchedCodes": confirmed["matchedCodes"],
    }
    assert pv.recheck(pp, check["id"], NOKSAN)["match"] == "check"
    # 같은 플라스틱이면 재질이 바뀌어도(PP -> PE) 후보다
    assert pv.recheck({"material": "PE"}, confirmed["id"], NOKSAN)["ok"]
    aluminum = {"material": "알루미늄", "wasteItem": "금속 스크랩"}
    for candidate in found:
        assert pv.recheck(aluminum, candidate["id"], NOKSAN) == {
            "ok": False,
            "reason": "non_plastic",
            "match": None,
            "matchedCodes": [],
        }
    assert pv.recheck({}, confirmed["id"], NOKSAN)["reason"] == "unknown_material"
    assert pv.recheck(pp, "pv_none", NOKSAN)["reason"] == "not_listed"
    assert pv.recheck(pp, confirmed["id"], None)["reason"] == "no_origin"
    assert pv.recheck(pp, confirmed["id"], (126.9780, 37.5665))["reason"] == "too_far"  # 서울
    named = pv.recheck(pp, confirmed["id"], NOKSAN, exclude_names=(f"(주) {confirmed['name']}",))
    assert named["reason"] == "registered"
