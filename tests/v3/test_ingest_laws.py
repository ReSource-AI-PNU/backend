"""법령 수집 스크립트의 파서와 흐름. 실제 law.go.kr 호출은 없다(가짜 transport).

예시 XML은 공식 가이드(lsNwListGuide, lsNwInfoGuide)의 출력 필드 이름으로 만든 것이다. 가이드에
응답 본문 예시가 없어 실제 응답을 옮긴 것이 아니다. 조문 글은 저장소의 laws.json(국가법령정보센터
원문)에서 가져왔다. 파서는 요소 이름과 문서 순서만 보므로, 겹친 모양과 납작한 모양을 모두 넣었다.
"""

import json
import urllib.parse
from datetime import UTC, datetime

import pytest

from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import SEED_DIR, load_seed_file
from scripts.v3 import ingest_laws

OC = "test-oc"
NOW = datetime(2026, 11, 17, 3, 0, 0, tzinfo=UTC)
SEEDS = {item["id"]: item for item in json.loads((SEED_DIR / "laws.json").read_text("utf-8"))}
ARTICLE_18 = SEEDS["law:폐기물관리법:제18조@2026-11-12"]["excerpt"].split("\n")
ARTICLE_4_2 = SEEDS["law:폐기물관리법-시행규칙:제4조의2@2026-11-12"]["excerpt"].split("\n")

SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<LawSearch>
  <target>law</target><키워드>폐기물관리법</키워드><section>lawNm</section>
  <totalCnt>3</totalCnt><page>1</page>
  <law id="1">
    <법령일련번호>279797</법령일련번호><현행연혁코드>현행</현행연혁코드>
    <법령명한글><![CDATA[폐기물관리법]]></법령명한글><법령ID>001823</법령ID>
    <공포일자>20251111</공포일자><공포번호>21129</공포번호><시행일자>20261112</시행일자>
  </law>
  <law id="2">
    <법령일련번호>291947</법령일련번호><현행연혁코드>현행</현행연혁코드>
    <법령명한글><![CDATA[폐기물관리법 시행규칙]]></법령명한글><법령ID>006911</법령ID>
    <시행일자>20261112</시행일자>
  </law>
  <law id="3">
    <법령일련번호>270001</법령일련번호><현행연혁코드>연혁</현행연혁코드>
    <법령명한글><![CDATA[폐기물관리법]]></법령명한글><법령ID>001823</법령ID>
    <시행일자>20250101</시행일자>
  </law>
</LawSearch>"""


def cdata(text):
    return f"<![CDATA[{text}]]>"


def nested_law_xml():
    """조문 아래에 항이 겹쳐 있는 모양."""
    paragraphs = "".join(
        f"<항><항번호>{cdata(line[:1])}</항번호><항내용>{cdata(line)}</항내용></항>"
        for line in ARTICLE_18[1:]
    )
    heading, _, first = ARTICLE_18[0].partition(") ")
    heading += ")"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<법령 법령키="0018232025111121129">
  <기본정보>
    <법령ID>001823</법령ID><공포일자>20251111</공포일자><공포번호>21129</공포번호>
    <언어>한글</언어><법종구분 법종구분코드="A0002">법률</법종구분>
    <법령명_한글>{cdata("폐기물관리법")}</법령명_한글><시행일자>20261112</시행일자>
  </기본정보>
  <조문>
    <조문단위 조문키="0000000">
      <조문번호>1</조문번호><조문여부>전문</조문여부>
      <조문내용>{cdata("        제1장 총칙  ")}</조문내용>
    </조문단위>
    <조문단위 조문키="0018001">
      <조문번호>18</조문번호><조문여부>조문</조문여부>
      <조문제목>{cdata("사업장폐기물의 처리")}</조문제목><조문시행일자>20261112</조문시행일자>
      <조문내용>{cdata(heading)}</조문내용>
      <항><항번호>{cdata("①")}</항번호><항내용>{cdata(first)}</항내용></항>
      {paragraphs}
    </조문단위>
    <조문단위 조문키="0013021">
      <조문번호>13</조문번호><조문가지번호>2</조문가지번호><조문여부>조문</조문여부>
      <조문제목>{cdata("폐기물의 재활용 원칙 및 준수사항")}</조문제목>
      <조문시행일자>20270101</조문시행일자>
      <조문내용>{cdata("제13조의2(폐기물의 재활용 원칙 및 준수사항) 시행 전인 조문")}</조문내용>
    </조문단위>
    <조문단위 조문키="0099001">
      <조문번호>99</조문번호><조문여부>조문</조문여부>
      <조문제목>{cdata("원문이 없는 조")}</조문제목><조문내용></조문내용>
    </조문단위>
  </조문>
  <부칙><부칙단위><부칙공포일자>20251111</부칙공포일자><부칙내용>{cdata("부칙")}</부칙내용></부칙단위></부칙>
</법령>"""


def flat_law_xml():
    """요소가 겹치지 않고 차례로 놓인 모양(가이드의 필드 순서 그대로)."""
    lines = "".join(f"<항내용>{cdata(line)}</항내용>" for line in ARTICLE_4_2[1:])
    return f"""<Law>
  <법령ID>006911</법령ID><공포일자>20261112</공포일자><공포번호>70</공포번호>
  <법령명_한글>{cdata("폐기물관리법 시행규칙")}</법령명_한글><시행일자>20261112</시행일자>
  <조문번호>4</조문번호><조문가지번호>2</조문가지번호><조문여부>조문</조문여부>
  <조문제목>{cdata("폐기물의 종류 및 재활용 유형")}</조문제목>
  <조문내용>{cdata(ARTICLE_4_2[0])}</조문내용>{lines}
</Law>"""


class FakeLawApi:
    def __init__(self, laws, search=SEARCH_XML, status=200):
        self.laws, self.search, self.status, self.urls = laws, search, status, []

    def __call__(self, url):
        self.urls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert query["OC"] == [OC] and query["target"] == ["law"] and query["type"] == ["XML"]
        if "lawSearch.do" in url:
            return self.status, self.search.encode("utf-8")
        return self.status, self.laws[query["MST"][0]].encode("utf-8")


def test_urls_follow_the_guide():
    assert ingest_laws.search_url(OC, "폐기물관리법") == (
        "https://www.law.go.kr/DRF/lawSearch.do?OC=test-oc&target=law&type=XML"
        "&query=%ED%8F%90%EA%B8%B0%EB%AC%BC%EA%B4%80%EB%A6%AC%EB%B2%95&display=100"
    )
    assert ingest_laws.service_url(OC, "279797") == (
        "https://www.law.go.kr/DRF/lawService.do?OC=test-oc&target=law&type=XML&MST=279797"
    )
    assert OC not in ingest_laws.public_url("폐기물관리법")
    assert (
        ingest_laws.public_url("폐기물관리법")
        == SEEDS["law:폐기물관리법:제18조@2026-11-12"]["meta"]["url"]
    )
    with pytest.raises(ingest_laws.IngestError, match="law.go.kr"):
        ingest_laws.urllib_transport("https://example.invalid/DRF/lawService.do")


def test_search_picks_the_current_law_with_the_exact_name():
    items = ingest_laws.parse_search(SEARCH_XML)
    assert [(i["mst"], i["name"], i["status"]) for i in items] == [
        ("279797", "폐기물관리법", "현행"),
        ("291947", "폐기물관리법 시행규칙", "현행"),
        ("270001", "폐기물관리법", "연혁"),
    ]
    assert ingest_laws.pick_current(items, "폐기물관리법")["mst"] == "279797"
    assert ingest_laws.pick_current(items, "폐기물관리법 시행규칙")["mst"] == "291947"
    with pytest.raises(ingest_laws.IngestError, match="하나로 정하지 못했다"):
        ingest_laws.pick_current(items, "폐기물")
    with pytest.raises(ingest_laws.IngestError, match="XML이 아니다"):
        ingest_laws.parse_search("<html>점검 중")


def test_articles_become_evidence_with_verbatim_text():
    law = ingest_laws.parse_law(nested_law_xml())
    assert (law["name"], law["lawId"], law["effective"]) == ("폐기물관리법", "001823", "20261112")
    items, skipped = ingest_laws.to_evidence(
        law, "279797", today="2026-11-17", retrieved_at=NOW.isoformat()
    )
    assert [item["id"] for item in items] == ["law:폐기물관리법:제18조@2026-11-12"]
    made, seeded = items[0], SEEDS["law:폐기물관리법:제18조@2026-11-12"]
    # 원문은 글자 그대로다. 줄을 나누는 자리만 다르다(API는 조 제목과 첫 항을 따로 준다)
    assert made["excerpt"].replace("\n", " ") == seeded["excerpt"].replace("\n", " ")
    assert made["excerpt"].split("\n")[0] == "제18조(사업장폐기물의 처리)"
    for key in ("kind", "label", "source", "asOf", "validUntil", "access", "value", "unit"):
        assert made[key] == seeded[key], key
    assert set(made) == set(seeded)  # Evidence.to_dict()와 같은 키
    assert made["meta"]["lsiSeq"] == "279797" and made["meta"]["url"] == seeded["meta"]["url"]
    assert made["meta"]["promulgated"] == "2025-11-11" and OC not in json.dumps(made)
    assert skipped == [
        {"article": "제13조의2", "reason": "시행 전(2027-01-01)"},
        {"article": "제99조", "reason": "원문 없음"},
    ]


def test_flat_layout_gives_the_same_kind_of_record():
    law = ingest_laws.parse_law(flat_law_xml())
    items, skipped = ingest_laws.to_evidence(
        law, "291947", today="2026-11-17", retrieved_at=NOW.isoformat()
    )
    seeded = SEEDS["law:폐기물관리법-시행규칙:제4조의2@2026-11-12"]
    assert skipped == [] and [item["id"] for item in items] == [seeded["id"]]
    assert items[0]["excerpt"] == seeded["excerpt"] and items[0]["label"] == seeded["label"]


def test_records_without_text_or_date_are_left_out():
    law = {
        "name": "가상법",
        "effective": "",
        "articles": [
            {"number": "1", "type": "조문", "lines": ["제1조(목적) 가상 조문"]},
            {"number": "2", "type": "조문", "effective": "20260101", "lines": ["제2조 가상"]},
            {"number": "2", "type": "조문", "effective": "20260101", "lines": ["제2조 가상"]},
            {"number": "x", "type": "조문", "lines": ["번호 없음"]},
            {"number": "3", "type": "전문", "lines": ["제2장"]},
        ],
    }
    items, skipped = ingest_laws.to_evidence(law, "1", today="2026-11-17", retrieved_at="t")
    assert [item["id"] for item in items] == ["law:가상법:제2조@2026-01-01"]
    assert [s["reason"] for s in skipped] == [
        "시행일을 알 수 없음",
        "같은 조가 두 번 나옴",
        "조 번호를 알 수 없음",
    ]
    only, _ = ingest_laws.to_evidence(
        law, "1", today="2026-11-17", retrieved_at="t", only={"제9조"}
    )
    assert only == []
    assert ingest_laws.iso_date("2026. 11. 12.") == "2026-11-12"
    assert ingest_laws.iso_date("20261340") is None and ingest_laws.iso_date(None) is None
    with pytest.raises(ingest_laws.IngestError, match="법령 이름"):
        ingest_laws.to_evidence({"articles": []}, "1", today="2026-11-17", retrieved_at="t")


def test_without_the_key_nothing_is_called(tmp_path, capsys):
    api = FakeLawApi({})
    out = tmp_path / "laws.json"
    assert ingest_laws.main(["--out", str(out)], {}, transport=api, now=lambda: NOW) == 2
    assert api.urls == [] and not out.exists()
    assert "LAW_OC" in capsys.readouterr().err


def test_run_writes_a_seed_file_the_loader_accepts(tmp_path, capsys):
    api = FakeLawApi({"279797": nested_law_xml(), "291947": flat_law_xml()})
    out = tmp_path / "nested" / "laws.ingested.json"
    argv = ["--law", "폐기물관리법", "--law", "폐기물관리법 시행규칙", "--out", str(out)]
    assert ingest_laws.main(argv, {"LAW_OC": OC}, transport=api, now=lambda: NOW) == 0
    assert len(api.urls) == 4
    written = json.loads(out.read_text("utf-8"))
    assert [item["id"] for item in written] == [
        "law:폐기물관리법-시행규칙:제4조의2@2026-11-12",
        "law:폐기물관리법:제18조@2026-11-12",
    ]
    assert OC not in out.read_text("utf-8") and OC not in capsys.readouterr().out
    assert written[0]["meta"]["retrievedAt"] == "2026-11-17T03:00:00+00:00"
    registry = InMemoryRegistry()
    assert load_seed_file(registry, out) == 2  # laws.json과 같은 형식이라 그대로 읽힌다
    assert registry.get(written[1]["id"], "org_hanbit").excerpt == written[1]["excerpt"]


def test_failures_are_reported_and_not_written(tmp_path, capsys):
    out = tmp_path / "laws.json"
    env = {"LAW_OC": OC}
    down = FakeLawApi({}, status=500)
    assert ingest_laws.main(["--out", str(out)], env, transport=down, now=lambda: NOW) == 1
    assert not out.exists()
    assert "HTTP 500" in capsys.readouterr().err
    # 본문이 다른 법령이거나 조문이 없으면 그 법령은 넣지 않는다
    wrong = FakeLawApi({"279797": flat_law_xml()})
    argv = ["--law", "폐기물관리법", "--out", str(out)]
    assert ingest_laws.main(argv, env, transport=wrong, now=lambda: NOW) == 1
    empty = FakeLawApi({"279797": "<Law><result>사용자 정보 검증에 실패하였습니다.</result></Law>"})
    assert ingest_laws.main(argv, env, transport=empty, now=lambda: NOW) == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "이름이 다르다" in err and OC not in err
    assert ingest_laws.main(["--only", "제17조"], env, transport=wrong, now=lambda: NOW) == 2


def test_dry_run_and_article_filter(tmp_path, capsys):
    api = FakeLawApi({"279797": nested_law_xml()})
    out = tmp_path / "laws.json"
    argv = ["--law", "폐기물관리법", "--only", "폐기물관리법=제18조,제99조", "--dry-run"]
    assert ingest_laws.main([*argv, "--out", str(out)], {"LAW_OC": OC}, transport=api) == 0
    printed = capsys.readouterr().out
    assert "폐기물관리법: 1개 조" in printed and "제18조(사업장폐기물의 처리)" in printed
    assert "저장하지 않았다" in printed and not out.exists()
