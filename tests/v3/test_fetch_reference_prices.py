"""가격조사 수집 스크립트의 파서와 흐름. 실제 공공데이터포털 호출은 없다(가짜 transport).

응답 모양은 2026-11-18의 실제 호출에서 본 것과 같게 적었다. 인증키는 지어낸 글이다.
"""

import io
import json
import urllib.parse
from datetime import UTC, datetime

import pytest

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.evidence.seeds import SEED_DIR
from scripts.v3 import fetch_reference_prices as prices

KEY = "test+service/key=="
NOW = datetime(2026, 11, 18, 1, 0, 0, tzinfo=UTC)
ROWS_2026_10 = [
    ("폐지 (신문지)", 117),
    ("압축 (PE)", 483),
    ("압축 (PP)", 391),
    ("압축 (PET)", 600),
    ("플레이크 (PE)", 622),
    ("플레이크 (PP)", 563),
    ("펠렛 (PE)", 888),
    ("펠렛 (PP)", 724),
    ("EPS (잉고트)", 549),
    ("폐금속류 (철스크랩)", 354),
]


def page(rows, region="경남", month="202610", total=None, code="200"):
    items = [
        {
            "telgmRcptnYmd": "20261101",
            "exmnYmd": month,
            "stdgCd": "4800000000",
            "stdgNm": region,
            "itemNm": name,
            "mrktPrc": price,
        }
        for name, price in rows
    ]
    body = {"items": items, "numOfRows": 100, "pageNo": 1, "totalCount": total or len(items)}
    header = {"resultCode": code, "resultMsg": "NORMAL SERVICE." if code == "200" else "ERROR"}
    return json.dumps({"header": header, "body": body}, ensure_ascii=False).encode("utf-8")


def fake(data):
    """data: {(권역, YYYYMM): 행 목록}. 없는 달은 빈 목록으로 답한다."""
    sent = []

    def transport(url):
        query = {
            k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
        }
        sent.append(query)
        rows = data.get((query["stdgNm"], query["exmnYmd"]), [])
        return 200, page(rows, query["stdgNm"], query["exmnYmd"])

    return transport, sent


@pytest.fixture
def seeds(tmp_path):
    path = tmp_path / "refs.json"
    path.write_text((SEED_DIR / "refs.json").read_text("utf-8"), encoding="utf-8")
    return path


def run(argv, transport, env=None):
    out = io.StringIO()
    code = prices.main(
        argv,
        transport=transport,
        env={prices.KEY_ENV: KEY} if env is None else env,
        now=NOW,
        out=out,
    )
    return code, out.getvalue()


def test_item_names_and_dates():
    assert prices.parse_item_name("압축 (PE)") == ("PE", "압축")
    assert prices.parse_item_name("플레이크 (PVC)") == ("PVC", "플레이크")
    assert prices.parse_item_name("EPS (잉고트)") == ("EPS", "잉고트")
    for other in ("폐지 (신문지)", "폐금속류 (철스크랩)", "폐타이어 (고무분말)", "압축", "", None):
        assert prices.parse_item_name(other) is None
    assert prices.previous_month("2026-01") == "2025-12" and prices.previous_month("2026-11") == (
        "2026-10"
    )
    assert (
        prices.month_end("2026-10") == "2026-10-31" and prices.month_end("2028-02") == "2028-02-29"
    )


def test_request_carries_the_documented_parameters_and_encodes_the_key_once():
    url = prices.request_url(KEY, "경남", "2026-10", 1)
    query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
    assert url.startswith("https://apis.data.go.kr/B552584/reutilMrktPrcExmn/getlist?")
    assert query == {
        "serviceKey": KEY,
        "pageNo": "1",
        "numOfRows": "100",
        "returnType": "JSON",
        "exmnYmd": "202610",
        "stdgNm": "경남",
    }
    encoded = urllib.parse.quote(KEY, safe="")
    assert prices.request_url(encoded, "경남", "2026-10", 1) == url  # 인코딩된 키로 와도 같다


def test_latest_month_is_found_and_only_price_rows_are_rewritten(seeds):
    before = json.loads(seeds.read_text("utf-8"))
    transport, sent = fake({("경남", "202610"): ROWS_2026_10})
    code, text = run(["--seeds", str(seeds)], transport)
    assert code == 0, text
    assert [q["exmnYmd"] for q in sent] == ["202611", "202610"]  # 이번 달에 없어 한 달 거슬러 갔다
    after = json.loads(seeds.read_text("utf-8"))
    price_rows = [row for row in after if row["id"].startswith("ref:가격조사:")]
    others = [row for row in after if not row["id"].startswith("ref:가격조사:")]
    assert others == [row for row in before if not row["id"].startswith("ref:가격조사:")]
    assert [row["id"] for row in price_rows] == [
        f"ref:가격조사:{name}-경남@2026-10"
        for name in ("PE-압축", "PP-압축", "PE-플레이크", "PP-플레이크", "PE-펠렛", "PP-펠렛")
    ]
    assert [row["value"] for row in price_rows] == [483.0, 391.0, 622.0, 563.0, 888.0, 724.0]
    assert seeds.read_text("utf-8") == prices.dump(
        after
    )  # 파일 모양(들여쓰기, 끝 줄바꿈)이 그대로다
    for row in price_rows:
        meta = row["meta"]
        evidence = Evidence.from_dict(row)
        assert (
            evidence.kind == "ref" and evidence.as_of == "2026-10-31" and evidence.access == ("*",)
        )
        assert row["id"] == ids.ref_id(meta["series"], meta["item"], meta["period"])
        assert f"{row['value']:g}" in row["excerpt"] and row["unit"] == "원/kg"
        assert meta["url"] == prices.ENDPOINT and meta["retrievedAt"] == "2026-11-18T01:00:00Z"
        assert "data.go.kr" in meta["dataset"] and "15156640" in row["source"]
        assert KEY not in json.dumps(row, ensure_ascii=False)
    assert "ID는 그대로입니다" in text and KEY not in text
    assert urllib.parse.quote(KEY, safe="") not in text


def test_a_new_month_reports_which_default_refs_to_update(seeds):
    newer = [(name, price + 10) for name, price in ROWS_2026_10]
    transport, _ = fake({("경남", "202611"): newer})
    code, text = run(["--seeds", str(seeds)], transport)
    assert code == 0, text
    after = json.loads(seeds.read_text("utf-8"))
    assert {row["id"] for row in after if row["id"].startswith("ref:가격조사:")} == {
        f"ref:가격조사:{name}-경남@2026-11"
        for name in ("PE-압축", "PP-압축", "PE-플레이크", "PP-플레이크", "PE-펠렛", "PP-펠렛")
    }
    assert "defaults.v1.json" in text and "없어진 ID" in text
    assert '"reference.price.PP.압축": ["ref:가격조사:PP-압축-경남@2026-11"]' in text
    assert text.count('"reference.price.') == 6


def test_dry_run_month_materials_and_extra_regions(seeds):
    original = seeds.read_text("utf-8")
    data = {("경남", "202610"): ROWS_2026_10, ("경북", "202610"): ROWS_2026_10}
    transport, sent = fake(data)
    argv = ["--seeds", str(seeds), "--month", "2026-10", "--materials", "all"]
    code, text = run([*argv, "--region", "경남", "--region", "경북", "--dry-run"], transport)
    assert code == 0 and seeds.read_text("utf-8") == original and "--dry-run" in text
    assert [q["stdgNm"] for q in sent] == ["경남", "경북"]
    assert "ref:가격조사:EPS-잉고트-경북@2026-10" in text and "PET-압축-경남" in text
    assert "신문지" not in text and "철스크랩" not in text  # 플라스틱 품목만 싣는다
    code, text = run(["--seeds", str(seeds), "--region", "부산", "--month", "2026-10"], transport)
    assert code == 1 and "NO_DATA" in text and seeds.read_text("utf-8") == original


@pytest.mark.parametrize(
    ("status", "raw", "code"),
    [
        (200, page([], code="30"), "30"),  # 등록되지 않은 인증키 등
        (
            200,
            b"<OpenAPI_ServiceResponse>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</OpenAPI_ServiceResponse>",
            "NOT_JSON",
        ),
        (500, b"{}", "500"),
        (200, b"[]", "BAD_SHAPE"),
        (
            200,
            json.dumps({"header": {"resultCode": "200"}, "body": {"items": "x"}}).encode(),
            "BAD_SHAPE",
        ),
    ],
)
def test_errors_leave_the_seed_file_alone_and_say_the_code(seeds, tmp_path, status, raw, code):
    original = seeds.read_text("utf-8")
    raw_dir = tmp_path / "raw"
    exit_code, text = run(
        ["--seeds", str(seeds), "--save-raw", str(raw_dir)], lambda url: (status, raw)
    )
    assert exit_code == 1 and f"코드 {code}" in text and "그대로 두었습니다" in text
    assert seeds.read_text("utf-8") == original and KEY not in text
    assert [p.read_bytes() for p in raw_dir.iterdir()] == [raw]  # 받은 응답은 그대로 남긴다


def test_connection_failure_missing_key_and_no_recent_data(seeds):
    original = seeds.read_text("utf-8")

    def down(url):
        raise prices.FetchError(
            f"연결 실패 {url}"
        )  # 주소(인증키 포함)가 든 문구를 내도 싣지 않는다

    code, text = run(["--seeds", str(seeds)], down)
    assert code == 1 and KEY not in text and urllib.parse.quote(KEY, safe="") not in text
    code, text = run(["--seeds", str(seeds)], down, env={})
    assert code == 2 and prices.KEY_ENV in text
    code, text = run(["--seeds", str(seeds), "--month", "2026-13"], down)
    assert code == 2
    transport, sent = fake({})
    code, text = run(["--seeds", str(seeds)], transport)
    assert code == 1 and "NO_DATA" in text and len(sent) == prices.LOOK_BACK_MONTHS
    assert seeds.read_text("utf-8") == original


def test_pages_are_followed_until_the_total():
    calls = []

    def transport(url):
        number = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["pageNo"][0])
        calls.append(number)
        rows = [("압축 (PE)", 483)] if number == 1 else [("압축 (PP)", 391)]
        return 200, page(rows, total=2)

    rows = prices.fetch_month(KEY, "경남", "2026-10", transport)
    assert calls == [1, 2] and [row["itemNm"] for row in rows] == ["압축 (PE)", "압축 (PP)"]


def test_committed_seed_rows_came_from_this_script():
    """저장소의 가격조사 행은 이 스크립트가 쓴 모양이다(출처가 API이고 확인 일시가 있다)."""
    rows = json.loads((SEED_DIR / "refs.json").read_text("utf-8"))
    price_rows = [row for row in rows if row["id"].startswith("ref:가격조사:")]
    assert len(price_rows) >= 6
    for row in price_rows:
        assert row["meta"]["url"] == prices.ENDPOINT and row["source"] == prices.SOURCE
        assert row["value"] == int(row["value"])  # API는 원 단위로 준다
