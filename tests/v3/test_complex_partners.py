"""같은 산업단지의 자원순환 후보(결과 한 장의 complexPartners). 등록공장 생산품 분류, 회사의 단지 찾기,
시연 시나리오(녹산 한빛정밀)에서의 후보와 근거, 업체 뷰 차단, 견적 요청서."""

import pytest

from app.v3 import complex_partners, public_vendors, service
from app.v3.complex_partners import classify
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, answer_all, make_app, start

NOKSAN_HANBIT = {"name": "(주)한빛정밀", "location": [128.8260, 35.0871]}


@pytest.mark.parametrize(
    ("product", "role", "resins"),
    [
        (
            "재생PE,PP칩",
            "reprocess",
            ["PP", "PE"],
        ),  # 수지는 재생 품목 밖의 플라스틱 품목에서도 읽는다
        ("재생수지", "reprocess", []),
        ("합성수지재생", "reprocess", []),
        ("재생용 플라스틱 사출품,재생용 플라스틱 칩(CHIP)", "reprocess", []),
        ("플라스틱 사출품", "use", []),
        ("PE  FILM", "use", ["PE"]),
        ("포장용 프라스틱 CPP,PAD", "use", ["PP"]),
    ],
)
def test_roles_read_recycling_and_plastic_products(product, role, resins):
    found = classify.roles(product)
    assert list(found) == [role]
    assert found[role]["resins"] == resins


@pytest.mark.parametrize(
    "product",
    [
        "VACCUM PIPE",  # "PIPE"의 "PE"를 세지 않는다
        "플라스틱 도금",
        "EPDM칼라칩 등 합성고무칩",
        "합성수지",  # 화학 회사의 수지
        "에폭시 수지,폴리우레탄수지",
        "비닐필름용 커팅기, 실링기",
        "플라스틱사출금형",
        "음식물 단미사료",
        "FRP선박,건조및수리외, 합성수지제품, 합성수지선",  # 강화플라스틱 선박
        "",
    ],
)
def test_roles_skip_unrelated_products(product):
    assert classify.roles(product) == {}


def test_families_of_stream_material():
    assert classify.families("PP+ABS") == ["PP", "ABS"]
    assert classify.families("LDPE") == ["PE"]
    assert classify.families("알루미늄") == []
    assert classify.families(None) == []


def test_complex_of_reads_region_then_location_then_alias():
    assert complex_partners.complex_of({"region": "부산 녹산국가산업단지"})[0]["id"] == "noksan"
    found, basis = complex_partners.complex_of(NOKSAN_HANBIT)
    assert (found["id"], basis) == ("noksan", "location")
    assert complex_partners.complex_of({"region": "부산 강서구 미음산단"})[0]["id"] == "mieum"
    # 신호 단지 범위가 녹산과 겹치는 곳은 중심이 가까운 단지
    assert complex_partners.complex_of({"location": [128.87, 35.09]})[0]["id"] == "sinho"
    assert complex_partners.complex_of({"region": "경남 김해시"}) is None
    assert complex_partners.complex_of({}) is None


def test_search_finds_recyclers_in_the_same_complex_with_permits():
    found = complex_partners.search({"material": "PP", "origin": "PIR"}, NOKSAN_HANBIT)
    assert found["reason"] is None and found["complex"][0]["id"] == "noksan"
    takes = {(r["factory"] or r["vendor"])["name"]: r for r in found["takes"]}
    # 생산품이 재생원료이고 허가 명부(폐기물종합재활용업)에도 있는 녹산 공장
    assert takes["(주)성우케미칼"]["permit"] == "listed_check"
    assert takes["(주)성우케미칼"]["vendor"]["business"].startswith("폐기물")
    # 상호에 플라스틱 낱말이 없어 반경 검색(public_vendors)으로는 찾지 못하던 곳
    assert "극동산업" in takes and takes["극동산업"]["vendor"] is not None
    assert public_vendors._vendor_match(takes["극동산업"]["vendor"], found["codes"]) is None
    # 생산품에 같은 수지가 적힌 곳(재생PE·PP칩)이 수지를 적지 않은 곳보다 앞선다
    assert takes["동양피앤씨 유한회사"]["match"] == "named"
    # 허가 명부에 없는 곳은 허가 확인 필요이고 맨 뒤다
    assert found["takes"][-1]["permit"] == "unlisted"
    ranks = [complex_partners._PERMIT_RANK[r["permit"]] for r in found["takes"]]
    assert ranks == sorted(ranks)


def test_search_filters_uses_by_resin_and_film():
    pp = complex_partners.search({"material": "PP"}, NOKSAN_HANBIT)
    film = complex_partners.search({"material": "LDPE", "form": "필름"}, NOKSAN_HANBIT)
    pp_names = {r["factory"]["name"] for r in pp["uses"]}
    film_names = {r["factory"]["name"] for r in film["uses"]}
    assert "부산프라스틱공업(주)" in pp_names  # CPP
    assert "(주)아이에스테크" not in pp_names and "(주)아이에스테크" in film_names  # PE FILM
    assert "(주)다원테크" not in pp_names and "(주)다원테크" in film_names  # 수지 없는 비닐포장지
    assert pp["uses"][0]["match"] == "named"


def test_search_explains_when_it_does_not_look():
    assert complex_partners.search({"material": "PP"}, {"name": "x"})["reason"] == (
        complex_partners.NO_COMPLEX_REASON
    )
    metal = complex_partners.search({"material": "알루미늄"}, NOKSAN_HANBIT)
    assert metal["takes"] == [] and metal["reason"] == public_vendors.NON_PLASTIC_REASON
    mine = complex_partners.search(
        {"material": "PP"}, {**NOKSAN_HANBIT, "name": "성우케미칼"}, exclude_names=("극동산업",)
    )
    names = {(r["factory"] or r["vendor"])["name"] for r in mine["takes"]}
    assert "(주)성우케미칼" not in names and "극동산업" not in names


@pytest.fixture(scope="module")
def waiting():
    app = make_app()
    view = answer_all(app, start(app))
    if view["session"]["status"] == "awaiting_answers":
        view = answer_all(app, view)
    return app, view


def test_result_lists_complex_partners_with_evidence(waiting):
    app, view = waiting
    partners = view["result"]["complexPartners"]
    assert partners["complex"] == {"id": "noksan", "name": "녹산국가산업단지", "basis": "location"}
    assert partners["source"]["factories"]["url"].startswith("https://www.data.go.kr/")
    assert {s["streamId"] for s in partners["streams"]} == {s["id"] for s in view["streams"]}
    registered = {app.store.get_org(p["orgId"])["name"] for p in app.store.list_vendor_profiles()}
    takes = [c for s in partners["streams"] for c in s["takes"]]
    uses = [c for s in partners["streams"] for c in s["uses"]]
    assert takes and uses
    for entry in partners["streams"]:
        assert len(entry["takes"]) == min(complex_partners.TAKES_LIMIT, entry["takesTotal"])
        assert len(entry["uses"]) == min(complex_partners.USES_LIMIT, entry["usesTotal"])
    for candidate in takes + uses:
        assert candidate["name"] not in registered
        assert candidate["evidence"]
        for evidence_id in candidate["evidence"]:  # 근거는 배출 기업이 열 수 있다
            ref = app.registry.get(evidence_id, HANBIT)
            assert ref.kind == "ref"
        if candidate["product"]:
            assert (
                candidate["product"] in app.registry.get(candidate["evidence"][0], HANBIT).excerpt
            )
    for candidate in takes:
        assert candidate["permit"]["status"] in ("listed", "listed_check", "unlisted")
        assert (candidate["permit"]["vendorId"] is None) == (
            candidate["permit"]["status"] == "unlisted"
        )
    assert all("permit" not in c for c in uses)


def test_outside_candidates_mark_same_complex(waiting):
    _, view = waiting
    candidates = [c for s in view["result"]["outsideVendors"]["streams"] for c in s["candidates"]]
    assert all(isinstance(c["sameComplex"], bool) for c in candidates)
    same = complex_partners.same_complex_vendor_ids(NOKSAN_HANBIT)
    assert all(c["sameComplex"] == (c["id"] in same) for c in candidates)


def test_vendor_view_does_not_carry_complex_partners(waiting):
    app, view = waiting
    vendor_view = service.get_view(app, view["session"]["id"], SAEGIL)
    assert "complexPartners" not in (vendor_view["result"] or {})


def test_quote_request_for_a_complex_partner_found_by_product(waiting):
    app, view = waiting
    sid = view["session"]["id"]
    entry, candidate = next(
        (s, c)
        for s in view["result"]["complexPartners"]["streams"]
        for c in s["takes"]
        if c["name"] == "극동산업"
    )
    vendor_id = candidate["permit"]["vendorId"]
    listed = {c["id"] for s in view["result"]["outsideVendors"]["streams"] for c in s["candidates"]}
    assert vendor_id not in listed  # 반경 후보가 아니어도 같은 단지 후보라 만들 수 있다
    letter = service.quote_request(app, sid, HANBIT, entry["streamId"], vendor_id)
    assert letter["validation"]["ok"], letter["validation"]
    assert letter["recipient"]["name"] == "극동산업"
    with pytest.raises(service.ServiceError) as err:
        service.quote_request(app, sid, HANBIT, entry["streamId"], "pv_not_a_candidate")
    assert err.value.code == "NOT_FOUND"


def test_complex_recheck_follows_current_stream():
    vendor = next(
        v for v in public_vendors.registry()["vendors"] if v["name"] == "극동산업" and v["lon"]
    )
    ok = complex_partners.recheck({"material": "PP"}, vendor["id"], NOKSAN_HANBIT)
    assert ok == {"ok": True, "reason": None}
    metal = complex_partners.recheck({"material": "알루미늄"}, vendor["id"], NOKSAN_HANBIT)
    assert metal["reason"] == "non_plastic"
    outside = complex_partners.recheck({"material": "PP"}, vendor["id"], {"name": "x"})
    assert outside["reason"] == "not_in_complex"
    registered = complex_partners.recheck(
        {"material": "PP"}, vendor["id"], NOKSAN_HANBIT, exclude_names=("극동산업",)
    )
    assert registered["reason"] == "registered"
