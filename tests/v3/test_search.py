import pytest

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, InMemoryRegistry
from app.v3.evidence.search import bigrams, evidence_chain, get_evidence, search_evidence, tokenize

PRICE = "doc:org_saegil:매입기준표:PP-A-단가"
SECRET = "doc:org_other:위탁계약서:단가"
SURVEY = "ref:가격조사:PP-펠렛-경남@2026-08"
LAW = "law:시험용법:제1조@2026-01-01"
REVENUE = "calc:cost_carbon:aaaaaaaaaaaa:매각액"
MONTHLY = "calc:cost_carbon:bbbbbbbbbbbb:월손익"
LOOP = "calc:loop:cccccccccccc:x"


@pytest.fixture
def registry():
    def ev(eid, **kw):
        base = {"excerpt": "", "source": "시험 자료", "as_of": "2026-11-01"}
        return Evidence(id=eid, kind=eid.split(":")[0], **{**base, **kw})

    return InMemoryRegistry(
        [
            ev(PRICE, label="PP A등급 매입 단가", value=320, unit="원/kg",
               excerpt="PP 분쇄품 A등급은 kg당 320원에 매입한다.", access=("org_saegil", "org_hanbit")),
            ev(SECRET, label="위탁 처리 단가", value=250, unit="원/kg",
               excerpt="혼합 폐플라스틱 소각 위탁 단가", access=("org_other",)),
            ev(SURVEY, label="PP 펠렛 가격조사 (경남)", value=850000, unit="원/t",
               source="한국환경공단 재활용가능자원 가격조사"),
            ev(LAW, label="허가 품목 확인", excerpt="수탁자의 허가 품목을 확인하여야 한다."),
            ev(REVENUE, label="월 매각액", value=192000, unit="원/월",
               meta={"inputEvidence": [PRICE, SECRET, "measure:org_hanbit:st_sprue:월발생량@없음"]}),
            ev(MONTHLY, label="월 손익", value=162000, unit="원/월",
               meta={"inputEvidence": [REVENUE, PRICE, LOOP]}),
            ev(LOOP, label="순환", meta={"inputEvidence": [MONTHLY, LOOP]}),
        ]
    )  # fmt: skip


def test_tokenize_and_bigrams():
    assert tokenize("PP 펠렛, 가격조사!") == ["pp", "펠렛", "가격조사"]
    assert bigrams("가격조사 PP") == {"가격", "격조", "조사"}


def test_search_ranks_label_matches_first(registry):
    results = search_evidence(registry, "PP 매입 단가", org_id="org_hanbit")
    assert results[0]["evidence"]["id"] == PRICE
    assert results[0]["score"] > results[1]["score"]
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)
    assert results[0]["evidence"]["kindLabel"] == "문서"


def test_search_finds_korean_with_particles(registry):
    results = search_evidence(registry, "허가품목을", org_id="org_hanbit")
    assert [r["evidence"]["id"] for r in results] == [LAW]


def test_search_never_returns_other_orgs_evidence(registry):
    for query in ("위탁 처리 단가", "소각", SECRET, "", "단가"):
        ids = [
            r["evidence"]["id"]
            for r in search_evidence(registry, query, org_id="org_hanbit", limit=50)
        ]
        assert SECRET not in ids
    assert (
        search_evidence(registry, "위탁 처리 단가", org_id="org_other")[0]["evidence"]["id"]
        == SECRET
    )
    # 대화하지 않은 조직에는 공개 근거만 나온다("단가"는 동의어로 "가격"까지 넓혀 가격조사를 찾는다)
    assert [
        r["evidence"]["id"] for r in search_evidence(registry, "단가", org_id="org_namhae")
    ] == [SURVEY]


def test_search_filters_kinds_and_limit(registry):
    results = search_evidence(registry, "PP", kinds=["ref"], org_id="org_hanbit")
    assert [r["evidence"]["id"] for r in results] == [SURVEY]
    assert len(search_evidence(registry, "PP 단가 월", org_id="org_hanbit", limit=2)) == 2
    assert search_evidence(registry, "PP", org_id="org_hanbit", limit=0) == []


def test_search_is_deterministic_and_handles_empty_or_no_match(registry):
    first = search_evidence(registry, "월 손익", org_id="org_hanbit")
    assert first == search_evidence(registry, "월 손익", org_id="org_hanbit")
    assert search_evidence(registry, "전혀없는낱말xyz", org_id="org_hanbit") == []
    listed = search_evidence(registry, "  ", org_id="org_hanbit", limit=3)
    assert [r["score"] for r in listed] == [0.0, 0.0, 0.0]
    assert [r["evidence"]["id"] for r in listed] == sorted(r["evidence"]["id"] for r in listed)


def test_get_evidence(registry):
    assert get_evidence(registry, PRICE, "org_hanbit")["value"] == 320
    with pytest.raises(EvidenceForbidden):
        get_evidence(registry, SECRET, "org_hanbit")
    with pytest.raises(EvidenceNotFound):
        get_evidence(registry, "doc:없음:x:y", "org_hanbit")


def test_chain_follows_inputs_hides_forbidden_and_stops_cycles(registry):
    chain = evidence_chain(registry, MONTHLY, "org_hanbit")
    assert [(c["id"], c["depth"]) for c in chain] == [
        (REVENUE, 1),
        (PRICE, 2),
        (SECRET, 2),
        ("measure:org_hanbit:st_sprue:월발생량@없음", 2),
        (LOOP, 1),
    ]
    assert chain[2] == {"id": SECRET, "hidden": True, "depth": 2}
    assert chain[3] == {
        "id": "measure:org_hanbit:st_sprue:월발생량@없음",
        "missing": True,
        "depth": 2,
    }
    assert chain[1]["value"] == 320
    assert evidence_chain(registry, PRICE, "org_hanbit") == []


def test_chain_root_must_be_visible(registry):
    with pytest.raises(EvidenceForbidden):
        evidence_chain(registry, SECRET, "org_hanbit")
    with pytest.raises(EvidenceNotFound):
        evidence_chain(registry, "calc:없음:x:y", "org_hanbit")
