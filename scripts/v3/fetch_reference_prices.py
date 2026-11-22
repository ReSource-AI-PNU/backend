"""재활용가능자원 가격조사 참고 시세를 공공데이터포털 API에서 받아 시드 파일의 가격조사 행을 고쳐 쓴다.

    DATA_GO_KR_SERVICE_KEY=<인증키> python3 scripts/v3/fetch_reference_prices.py [--region 경남]
        [--month 2026-10] [--materials PE,PP] [--dry-run] [--save-raw 폴더]

- 출처: 한국환경공단_재활용가능자원 가격조사 정보 조회 서비스(공공데이터포털 15156640).
  GET https://apis.data.go.kr/B552584/reutilMrktPrcExmn/getlist
  (serviceKey, pageNo, numOfRows(최대 100), returnType=JSON, exmnYmd=YYYYMM, stdgNm=권역 이름)
  응답: {"header": {"resultCode": "200", "resultMsg": "NORMAL SERVICE."},
         "body": {"items": [{"exmnYmd", "stdgCd", "stdgNm", "itemNm", "mrktPrc", "telgmRcptnYmd"}], "totalCount"}}
  2026-11-18의 실제 호출에서 본 모양이다. 권역은 수도권, 강원, 충북, 충남, 전북, 전남, 경북, 경남 여덟이고
  부산은 따로 없다(경남 권역). 값은 원/kg이고 원 단위로 온다.
- 고쳐 쓰는 것은 app/v3/evidence/seeds/refs.json에서 ID가 `ref:가격조사:`로 시작하는 행뿐이다. 다른 행
  (KAU, 배출계수 등)과 그 순서, 파일의 들여쓰기는 그대로 둔다. 새 행은 옛 가격조사 행의 첫 자리에 넣는다.
- 근거 ID는 `ref:가격조사:<재질>-<형태>-<권역>@<YYYY-MM>` 그대로다. 달이 바뀌면 ID가 바뀌므로
  app/v3/judge/defaults.v1.json의 "refs"(reference.price.<재질>.<형태>)가 가리키는 ID도 고쳐야 한다.
  이 스크립트는 그 파일을 건드리지 않고, 고쳐야 할 키와 ID를 출력한다.
- --month를 주지 않으면 이번 달부터 거슬러 올라가며 자료가 있는 가장 최근 달을 찾는다.
- API가 오류 코드를 주거나 닿지 않으면 파일을 건드리지 않고 0이 아닌 코드로 끝난다.
- 인증키는 환경 변수로만 받고, 출력·예외 문구·파일에 싣지 않는다.
- 이 값은 참고 시세다. 업체 매입 단가가 아니므로 손익 계산에는 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.v3 import ids

ENDPOINT = "https://apis.data.go.kr/B552584/reutilMrktPrcExmn/getlist"
DATASET_URL = "https://www.data.go.kr/data/15156640/openapi.do"
SOURCE = "한국환경공단 재활용가능자원 가격조사 정보 조회 서비스 (공공데이터포털 15156640)"
SEEDS = ROOT / "app/v3/evidence/seeds/refs.json"
SERIES = "가격조사"
PREFIX = f"ref:{SERIES}:"
KEY_ENV = "DATA_GO_KR_SERVICE_KEY"
FORMS = ("압축", "플레이크", "펠렛", "잉고트")
OK_CODES = ("200", "00")
PAGE_SIZE = 100
LOOK_BACK_MONTHS = 6

# 조사 기준은 한국환경공단 월별 책자(재활용가능자원 가격조사, 2026년 10월호)의 문구다. API 응답에는 없다.
CONDITIONS = (
    "단위 원/kg, 부가가치세(VAT) 포함, 물류비 제외",
    "매월 10일 조사, 매월 26일 공표",
    "각 권역 내 품목별 조사업체 가격의 평균, 업체별 가격은 다를 수 있음",
    "조사 권역 경남: 부산광역시, 울산광역시, 경상남도",
)
STAGES = {
    "압축": "원재료를 선별･압축하여 압축품을 원료로 제품(플레이크, 펠렛 등)을 제조하는 업체에 판매하는 가격 (VAT 포함)",
    "플레이크": "파쇄시설을 갖춘 업체가 세척 파쇄품(플레이크)을 원료로 제품(펠렛 등)을 제조하는 업체에 판매하는 가격 (VAT 포함)",
    "펠렛": "용융･압출시설을 갖춘 업체가 펠렛을 원료로 제품을 제조하는 업체에 판매하는 가격 (VAT 포함)",
}
CAVEATS = (
    "asOf는 기준월의 말일로 적었다",
    "API는 권역 평균을 원 단위로 반올림해 준다. 월별 책자는 소수 한 자리까지 싣는다",
    "중간 가공품(압축·플레이크·펠렛)의 판매가격이라 가공 전 폐기물의 매입 단가와 다르다. 손익 계산에는 쓰지 않는다",
    "품질 등급은 잡색(생활계 혼합)이다. 공정 스크랩처럼 단일 재질·단색이면 실제 거래가는 이와 다를 수 있다",
)
# API는 발행 기관(한국환경공단)이 직접 내는 원자료라 받은 값 그대로를 원문 확인 값으로 둔다.
BOOKLET_URL = "https://www.recycling-info.or.kr/sds/marketIndex.do"
VERIFIED_META = {
    "evidenceClass": "official",
    "verificationStatus": "original_checked",
    "verifiedAgainst": "발행 기관 API 응답(원 단위)과 월별 책자의 조사 기준",
    "freightIncluded": False,
    "vatIncluded": True,
    "qualityGrade": "잡색(생활계 혼합)",
    "priceRole": "reference_index",
    "bookletPage": BOOKLET_URL,
}

# transport(url) -> (status, body). url에는 인증키가 들어 있으므로 기록하지 않는다
Transport = Callable[[str], tuple[int, bytes]]


class FetchError(RuntimeError):
    """API에서 자료를 받지 못했다. code는 API의 resultCode나 HTTP 상태다. 문구에 인증키를 싣지 않는다."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def urllib_transport(url: str) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise FetchError("https 주소만 호출한다")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise FetchError(f"공공데이터포털에 연결하지 못했다: {type(error).__name__}") from None


def request_url(key: str, region: str, month: str, page: int) -> str:
    """month는 YYYY-MM. 키가 URL 인코딩된 글로 와도(%가 있으면) 한 번만 인코딩되게 푼 뒤 싣는다."""
    query = urllib.parse.urlencode(
        {
            "serviceKey": urllib.parse.unquote(key),
            "pageNo": str(page),
            "numOfRows": str(PAGE_SIZE),
            "returnType": "JSON",
            "exmnYmd": month.replace("-", ""),
            "stdgNm": region,
        }
    )
    return f"{ENDPOINT}?{query}"


def parse_page(raw: bytes) -> tuple[list[dict[str, Any]], int]:
    """(행 목록, 전체 수). 결과 코드가 정상이 아니거나 모양이 다르면 FetchError."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FetchError(
            "응답이 JSON이 아니다(인증키 오류나 점검 안내일 수 있다)", "NOT_JSON"
        ) from None
    if not isinstance(payload, dict):
        raise FetchError("응답의 형식이 다르다", "BAD_SHAPE")
    payload = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    code = str(header.get("resultCode") or "")
    if code not in OK_CODES:
        message = str(header.get("resultMsg") or "")[:80]
        raise FetchError(
            f"API가 오류 코드를 줬다: {code or '없음'} {message}".strip(), code or "NO_CODE"
        )
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    items = body.get("items")
    if isinstance(items, dict):  # {"item": [...]} 꼴로 오는 API도 있다
        items = items.get("item")
    if items in (None, ""):
        items = []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise FetchError("응답의 형식이 다르다", "BAD_SHAPE")
    rows = [row.get("item", row) if isinstance(row, dict) else row for row in items]
    if any(not isinstance(row, dict) for row in rows):
        raise FetchError("응답의 형식이 다르다", "BAD_SHAPE")
    try:
        total = int(body.get("totalCount") or len(rows))
    except (TypeError, ValueError):
        total = len(rows)
    return rows, total


def fetch_month(
    key: str, region: str, month: str, transport: Transport, raw_sink: list | None = None
) -> list[dict[str, Any]]:
    """한 권역, 한 달의 모든 행. 자료가 없으면 빈 목록."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        status, raw = transport(request_url(key, region, month, page))
        if raw_sink is not None:
            raw_sink.append((f"{region}-{month}-p{page}", raw))
        if status != 200:
            raise FetchError(f"API 호출 실패 (HTTP {status})", str(status))
        found, total = parse_page(raw)
        rows.extend(found)
        if not found or len(rows) >= total or page >= 50:
            return rows
        page += 1


def previous_month(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"


def latest_month(
    key: str,
    region: str,
    start: str,
    transport: Transport,
    raw_sink: list | None = None,
    look_back: int = LOOK_BACK_MONTHS,
) -> tuple[str, list[dict[str, Any]]]:
    """start(YYYY-MM)부터 거슬러 올라가며 자료가 있는 첫 달과 그 행. 없으면 FetchError."""
    month = start
    for _ in range(look_back):
        rows = fetch_month(key, region, month, transport, raw_sink)
        if rows:
            return month, rows
        month = previous_month(month)
    raise FetchError(f"{region}: {start}부터 {look_back}달 안에 자료가 없다", "NO_DATA")


def parse_item_name(name: str) -> tuple[str, str] | None:
    """(재질, 형태). "압축 (PE)" -> ("PE", "압축"), "EPS (잉고트)" -> ("EPS", "잉고트"). 플라스틱이 아니면 None."""
    match = re.fullmatch(r"\s*([^()]+?)\s*\(\s*([^()]+?)\s*\)\s*", str(name or ""))
    if not match:
        return None
    left, inner = match.group(1), match.group(2)
    if left in FORMS and re.fullmatch(r"[A-Z]{2,5}", inner):
        return inner, left
    if inner in FORMS and re.fullmatch(r"[A-Z]{2,5}", left):
        return left, inner
    return None


def month_end(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def number_text(value: float) -> str:
    return f"{value:g}"


def to_seed_row(
    row: dict[str, Any], month: str, retrieved_at: str, previous: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """API의 한 행을 시드 행으로 바꾼다. 플라스틱 품목이 아니거나 값이 없으면 None."""
    parsed = parse_item_name(row.get("itemNm"))
    price = row.get("mrktPrc")
    region = str(row.get("stdgNm") or "").strip()
    if (
        parsed is None
        or not region
        or isinstance(price, bool)
        or not isinstance(price, (int, float))
        or price <= 0
    ):
        return None
    material, form = parsed
    item = f"{material}-{form}-{region}"
    year, number = month.split("-")
    value = float(price)
    caveats = list(CAVEATS)
    if previous is not None and previous.get("value") != value:
        caveats.append(
            f"이전 값 {number_text(float(previous['value']))} {previous.get('unit') or ''}"
            f"({(previous.get('meta') or {}).get('url') or previous.get('source') or '출처 미상'})을 "
            f"API 값으로 바꿨다"
        )
    return {
        "id": ids.ref_id(SERIES, item, month),
        "kind": "ref",
        "label": f"재활용가능자원 가격조사 {material} {form} · {region} · {year}년 {int(number)}월",
        "value": value,
        "unit": "원/kg",
        "excerpt": (
            f"조사월 {year}.{number} | 권역 {region} | 품목 {str(row.get('itemNm')).strip()} | "
            f"시장 가격 {number_text(value)} 원/kg"
        ),
        "source": SOURCE,
        "asOf": month_end(month),
        "validUntil": None,
        "access": ["*"],
        "version": 1,
        "meta": {
            "series": SERIES,
            "item": item,
            "period": month,
            "region": region,
            "material": material,
            "form": form,
            "url": ENDPOINT,
            "dataset": DATASET_URL,
            "retrievedAt": retrieved_at,
            "regionCode": str(row.get("stdgCd") or ""),
            "receivedOn": str(row.get("telgmRcptnYmd") or ""),
            "conditions": [
                *CONDITIONS,
                *([f"가격조사 단계: {STAGES[form]}"] if form in STAGES else []),
            ],
            "caveats": caveats,
            **VERIFIED_META,
            "verifiedOn": retrieved_at[:10],
        },
    }


def rewrite(existing: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """가격조사 행만 new_rows로 바꾼다. 다른 행과 순서는 그대로, 새 행은 옛 가격조사 행의 첫 자리에."""
    out: list[dict[str, Any]] = []
    placed = False
    for row in existing:
        if str(row.get("id") or "").startswith(PREFIX):
            if not placed:
                out.extend(new_rows)
                placed = True
            continue
        out.append(row)
    if not placed:
        out = [*new_rows, *out]
    return out


def dump(rows: list[dict[str, Any]]) -> str:
    """시드 파일의 모양 그대로(들여쓰기 한 칸, 한글 그대로, 끝에 줄바꿈)."""
    return json.dumps(rows, ensure_ascii=False, indent=1) + "\n"


def collect(
    key: str,
    regions: list[str],
    materials: set[str] | None,
    month: str | None,
    start_month: str,
    retrieved_at: str,
    existing: list[dict[str, Any]],
    transport: Transport,
    raw_sink: list | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """(새 시드 행, {권역: 쓴 달}). 권역마다 가장 최근 달을 쓴다. 행은 재질·형태 순서를 API가 준 대로 둔다."""
    old = {row["id"]: row for row in existing if str(row.get("id") or "").startswith(PREFIX)}
    rows: list[dict[str, Any]] = []
    months: dict[str, str] = {}
    for region in regions:
        if month:
            used, found = month, fetch_month(key, region, month, transport, raw_sink)
            if not found:
                raise FetchError(f"{region}: {month}에 자료가 없다", "NO_DATA")
        else:
            used, found = latest_month(key, region, start_month, transport, raw_sink)
        months[region] = used
        for raw_row in found:
            parsed = parse_item_name(raw_row.get("itemNm"))
            if parsed is None or (materials is not None and parsed[0] not in materials):
                continue
            item = f"{parsed[0]}-{parsed[1]}-{str(raw_row.get('stdgNm') or '').strip()}"
            seed_row = to_seed_row(
                raw_row, used, retrieved_at, old.get(ids.ref_id(SERIES, item, used))
            )
            if seed_row is not None:
                rows.append(seed_row)
    if not rows:
        raise FetchError("고른 재질의 가격조사 행이 하나도 없다", "NO_ROWS")
    if len({row["id"] for row in rows}) != len(rows):
        raise FetchError("같은 품목이 두 번 왔다", "DUPLICATE")
    return rows, months


def defaults_refs(rows: list[dict[str, Any]], region: str) -> dict[str, str]:
    """defaults.v1.json의 "refs"에 들어갈 키와 ID(한 권역 기준): reference.price.<재질>.<형태>."""
    return {
        f"reference.price.{row['meta']['material']}.{row['meta']['form']}": row["id"]
        for row in rows
        if row["meta"]["region"] == region
    }


def main(
    argv: list[str] | None = None,
    *,
    transport: Transport | None = None,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
    out=None,
) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        description="가격조사 참고 시세를 받아 시드의 가격조사 행을 고쳐 쓴다"
    )
    parser.add_argument(
        "--region", action="append", default=None, help="권역 이름(여러 번 줄 수 있다). 기본 경남"
    )
    parser.add_argument("--month", default=None, help="YYYY-MM. 없으면 자료가 있는 가장 최근 달")
    parser.add_argument(
        "--materials",
        default="PE,PP",
        help="쉼표로 나눈 재질. all이면 플라스틱 품목 모두. 기본 PE,PP",
    )
    parser.add_argument("--seeds", default=str(SEEDS), help="고쳐 쓸 시드 파일")
    parser.add_argument(
        "--dry-run", action="store_true", help="파일을 쓰지 않고 바뀔 내용만 보여 준다"
    )
    parser.add_argument(
        "--save-raw", default=None, help="받은 응답을 그대로 저장할 폴더(인증키는 없다)"
    )
    args = parser.parse_args(argv)
    settings = os.environ if env is None else env
    key = str(settings.get(KEY_ENV) or "").strip()
    if not key:
        print(f"환경 변수 {KEY_ENV}가 필요합니다.", file=out)
        return 2
    if args.month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        print("--month는 YYYY-MM 꼴이어야 합니다.", file=out)
        return 2
    moment = now or datetime.now(UTC)
    retrieved_at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    regions = args.region or ["경남"]
    materials = (
        None
        if args.materials.strip().lower() == "all"
        else {part.strip().upper() for part in args.materials.split(",") if part.strip()}
    )
    path = Path(args.seeds)
    existing = json.loads(path.read_text(encoding="utf-8"))
    raw_sink: list[tuple[str, bytes]] = []
    try:
        rows, months = collect(
            key,
            regions,
            materials,
            args.month,
            moment.strftime("%Y-%m"),
            retrieved_at,
            existing,
            transport or urllib_transport,
            raw_sink,
        )
        failure = None
    except FetchError as error:
        rows, months, failure = [], {}, error
    if args.save_raw:
        folder = Path(args.save_raw)
        folder.mkdir(parents=True, exist_ok=True)
        for name, raw in raw_sink:
            (folder / f"price-{name}.json").write_bytes(raw)
    if failure is not None:
        text = (
            str(failure)
            .replace(key, "<인증키>")
            .replace(urllib.parse.quote(key, safe=""), "<인증키>")
        )
        print(f"받지 못했습니다(코드 {failure.code or '없음'}): {text}", file=out)
        print("시드 파일은 그대로 두었습니다.", file=out)
        return 1
    old = {row["id"]: row for row in existing if str(row.get("id") or "").startswith(PREFIX)}
    new_ids = {row["id"] for row in rows}
    for region, month in months.items():
        print(f"{region}: {month} 자료를 썼습니다.", file=out)
    for row in rows:
        before = old.get(row["id"])
        note = (
            "새 행"
            if before is None
            else "값 같음"
            if before.get("value") == row["value"]
            else f"값 {number_text(float(before['value']))} → {number_text(row['value'])}"
        )
        print(f"  {row['id']}  {number_text(row['value'])} 원/kg  ({note})", file=out)
    removed = sorted(set(old) - new_ids)
    added = sorted(new_ids - set(old))
    if removed:
        print("없어진 ID: " + ", ".join(removed), file=out)
    if removed or added:
        print(
            'ID가 바뀌었습니다. app/v3/judge/defaults.v1.json의 "refs"를 이렇게 고쳐야 합니다:',
            file=out,
        )
        for name, evidence_id in defaults_refs(rows, regions[0]).items():
            print(f'  "{name}": ["{evidence_id}"]', file=out)
    else:
        print('ID는 그대로입니다. defaults.v1.json의 "refs"는 고칠 것이 없습니다.', file=out)
    if args.dry_run:
        print("--dry-run: 파일을 쓰지 않았습니다.", file=out)
        return 0
    path.write_text(dump(rewrite(existing, rows)), encoding="utf-8")
    print(f"{path}의 가격조사 행 {len(rows)}개를 고쳐 썼습니다. 다른 행은 그대로입니다.", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
