"""가입하지 않은 처리 업체 명부(공공데이터) 검색."""

from __future__ import annotations

from app.v3 import public_vendors as pv
from app.v3.public_vendors import treatment

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


# ── 2024년 처리 실적(「2024 전국 폐기물 발생 및 처리현황」 결과표) ──────────────


def _vendor(vendor_id):
    vendor = pv.get(vendor_id)
    assert vendor is not None, vendor_id
    return vendor


def test_treatment_table_keeps_only_the_needed_columns_and_no_person_names():
    data = treatment.table()
    assert data["year"] == 2024 and data["asOf"] == "2024-12-31"
    assert data["published"] == "2025-12-31"
    assert data["url"].startswith("https://www.recycling-info.or.kr/")
    assert data["sheets"] == [
        "8-가. 재활용처리(중간)",
        "8-나. 재활용처리(최종)",
        "8-다. 재활용처리(종합)",
    ]
    assert data["count"] == len(data["rows"]) > 5000
    keys = {
        "sheet",
        "row",
        "sido",
        "sgg",
        "name",
        "address",
        "waste",
        "facility",
        "capacityTonsPerDay",
        "tonsPerYear",
        "permitDate",
        "returnedOn",
    }
    # 대표자(개인 이름)와 전화번호는 남기지 않는다
    assert all(set(row) == keys for row in data["rows"])
    assert all(row["sheet"] in data["sheets"] for row in data["rows"])
    # 시도별 소계·전국 합계 줄은 없다
    assert not any(row["sgg"] in ("소계", "합계") for row in data["rows"])


def test_name_key_drops_company_forms_and_former_names():
    same = {
        "(주)천우엔지니어링",
        "㈜천우엔지니어링",
        "주식회사 천우 엔지니어링",
        "천우엔지니어링(주)",
    }
    assert {treatment.name_key(n) for n in same} == {"천우엔지니어링"}
    assert treatment.name_key("㈜일진건설환경 (구 일성에너지㈜)") == "일진건설환경"
    assert (
        treatment.name_key("㈜에코비트 에너지 울산(울산2공장) [구: ㈜티에스케이이앤피 울산2공장]")
        == "에코비트에너지울산울산2공장"
    )


def test_treatment_matches_name_and_district_and_uses_the_same_permit_sheet():
    # 대호수지(부산 강서구)는 중간·종합 두 허가로 올라 있다. 명부 줄의 업종과 같은 시트만 쓰고 더하지 않는다
    middle = treatment.match(_vendor("pv_637c017f1a"))
    total = treatment.match(_vendor("pv_e0f482a7f6"))
    assert (middle["sheet"], middle["tonsPerYear"], middle["capacityTonsPerDay"]) == (
        "8-가. 재활용처리(중간)",
        851.3,
        38.4,
    )
    assert (total["sheet"], total["tonsPerYear"], total["capacityTonsPerDay"]) == (
        "8-다. 재활용처리(종합)",
        0.0,
        2.0,
    )
    # 성광스틸(김해)은 중간·종합에 같은 처리량(2,084톤)을 겹쳐 적었다. 더하면 두 배가 된다
    steel = treatment.match(_vendor("pv_43867a34a7"))
    assert (steel["sheet"], steel["tonsPerYear"]) == ("8-다. 재활용처리(종합)", 2084.0)
    # 업종을 2024년 뒤에 바꾼 곳은 그 업체의 다른 시트 줄을 그 시트 이름과 함께 쓴다
    changed = treatment.match(_vendor("pv_0ae8a0652d"))  # 명부는 종합, 2024년 결과표는 중간
    assert changed["sheet"] == "8-가. 재활용처리(중간)" and changed["tonsPerYear"] == 29091.1


def test_treatment_is_left_empty_when_the_match_is_not_certain():
    # 창원시 안의 다른 구(명부 성산구, 결과표 마산합포구)는 다른 사업장일 수 있다
    assert treatment.match(_vendor("pv_d866672b76")) is None
    # 도로명 번지가 다르다(용담로 50-1 ↔ 58-1)
    assert treatment.match(_vendor("pv_adeb874b55")) is None
    # 같은 상호라도 시군구가 다르거나, 상호가 비슷하기만 하면 잇지 않는다
    daeho = dict(_vendor("pv_637c017f1a"))
    assert treatment.match({**daeho, "address": "부산광역시 사하구 다대로 1"}) is None
    assert treatment.match({**daeho, "name": "대호수지산업"}) is None
    assert treatment.match({**daeho, "address": "경기도 시흥시 녹산산업중로376"}) is None
    assert treatment.match({**daeho, "address": "주소없음"}) is None


def test_treatment_match_rate_in_busan_ulsan_gyeongnam():
    """2026-11-22 판: 부산·울산·경남 명부 503곳 중 378곳(부산 52/59, 울산 24/31, 경남 302/413)."""
    regions = ("부산광역시", "울산광역시", "경상남도")
    vendors = [v for v in pv.registry()["vendors"] if v["address"].startswith(regions)]
    found = sum(1 for v in vendors if treatment.match(v))
    assert len(vendors) == 503 and found == 378


def test_search_candidates_carry_the_2024_treatment_for_display_only():
    pp = {"material": "PP", "origin": "PIR"}
    result = pv.search(pp, NOKSAN)
    candidates = result["candidates"]
    shown = [c for c in candidates if c["treatment2024"]]
    assert shown and all(
        c["treatment2024"] is None or c["treatment2024"]["tonsPerYear"] is not None
        for c in candidates
    )
    for c in shown:
        t = c["treatment2024"]
        row = treatment.match(pv.get(c["id"]))
        assert (t["tonsPerYear"], t["capacityTonsPerDay"], t["sheet"], t["row"]) == (
            row["tonsPerYear"],
            row["capacityTonsPerDay"],
            row["sheet"],
            row["row"],
        )
        assert t["asOf"] == "2024-12-31" and t["source"].startswith("기후에너지환경부·한국환경공단")
    # 순위는 처리 실적과 상관없다(허가 정보로 맞는 곳 먼저, 가까운 순)
    confirmed = [c["distanceKm"] for c in candidates if c["match"] == "confirmed"]
    assert confirmed == sorted(confirmed)
