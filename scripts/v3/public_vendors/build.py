"""가입하지 않은 처리 업체 명부를 공공데이터에서 만든다.

    uv run python -m scripts.v3.public_vendors.build            # 내려받아 app/v3/public_vendors/registry.v1.json을 다시 쓴다
    uv run python -m scripts.v3.public_vendors.build --from F   # 이미 받은 JSON(F)으로만 만든다

- 원본: 공공데이터포털 "전국폐기물처리업소표준데이터"(https://www.data.go.kr/data/15114147/standard.do).
  지자체(2026-11 기준 116곳)가 올린 폐기물처리업 허가 정보를 표준 항목으로 모은 자료다. 모든 지자체가 올리지는
  않으므로 명부에 없는 업체도 있다.
- 내려받기는 포털의 "다운로드" 버튼이 부르는 JSON 주소(/download/columList.json, /download/standard.json)를 그대로 쓴다.
  인증키가 필요 없다.
- 재활용업(중간·최종·종합)만 남긴다. 수집·운반업과 처분업은 받아서 재활용하는 곳이 아니라 뺀다.
- 처리폐기물정보는 폐기물관리법 시행규칙 별표 4의 분류번호(예: 51-03-01)로 온다. 이름은 waste_codes.v1.json에서 찾는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import urllib.parse
import urllib.request
from pathlib import Path

PK = "15114147"
PAGE = f"https://www.data.go.kr/data/{PK}/standard.do"
BASE = "https://www.data.go.kr/download"
OUT = Path(__file__).resolve().parents[3] / "app" / "v3" / "public_vendors" / "registry.v1.json"
KEEP = ("재활용업",)
CODE = re.compile(r"\d{2}-\d{2}-\d{2}")


def _get(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": PAGE,
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def download() -> list[dict]:
    header = _get(f"{BASE}/columList.json?pk={PK}&ext=JSON")
    table = header["tableVO"]
    total = int(header["totalCount"])
    rows: list[dict] = []
    for page in range(1, math.ceil(total / 10000) + 1):
        params = [("colNmList", c) for c in table["colNmList"]] + [
            ("totalCount", total),
            ("svcTableNm", table["svcTableNm"]),
            ("perPage", 10000),
            ("page", page),
        ]
        rows += _get(f"{BASE}/standard.json?publicDataPk={PK}&" + urllib.parse.urlencode(params))
    if len(rows) != total:
        raise SystemExit(f"내려받은 행 수({len(rows)})가 전체 건수({total})와 다릅니다.")
    return rows


def _num(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def build(rows: list[dict]) -> dict:
    vendors = []
    for row in rows:
        business = (row.get("BUS_NM") or "").strip()
        if not any(k in business for k in KEEP):
            continue
        lat, lon = _num(row.get("LAT")), _num(row.get("LOT"))
        if lat is None or lon is None or not (33 <= lat <= 39 and 124 <= lon <= 132):
            continue
        name = (row.get("FLCT_NM") or "").strip()
        address = (row.get("LCTN_ROAD_NM_ADDR") or row.get("LCTN_LOTNO_ADDR") or "").strip()
        if not name or not address:
            continue
        key = f"{name}|{address}".encode()
        vendors.append(
            {
                "id": "pv_" + hashlib.sha1(key).hexdigest()[:10],
                "name": name,
                "address": address,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "business": business,
                "codes": sorted(set(CODE.findall(row.get("TRET_WAS_INF") or ""))),
                "permitDate": (row.get("PERMI_YMD") or "").strip() or None,
                "phone": (row.get("TELNO") or "").strip() or None,
                "agency": (row.get("MNG_INST_NM") or row.get("INSTT_NM") or "").strip() or None,
                "asOf": (row.get("CRTR_YMD") or "").strip() or None,
            }
        )
    vendors.sort(key=lambda v: v["id"])
    seen: set[str] = set()
    unique = [v for v in vendors if not (v["id"] in seen or seen.add(v["id"]))]
    _mark_reliability(unique)
    dates = sorted(v["asOf"] for v in unique if v["asOf"])
    return {
        "version": "v1",
        "source": "공공데이터포털 전국폐기물처리업소표준데이터",
        "url": PAGE,
        "note": "지자체가 올린 폐기물처리업 허가 정보입니다. 모든 지자체가 올리지는 않아 명부에 없는 업체도 있습니다.",
        "asOfRange": [dates[0], dates[-1]] if dates else None,
        "count": len(unique),
        "vendors": unique,
    }


GENERIC = {"00-00-00", "51-00-00", "51-99-00"}
BULK_SHARE, BULK_MIN = 0.9, 10


def _mark_reliability(vendors: list[dict]) -> None:
    """처리폐기물정보를 믿을 수 있는가(codesReliable). 2026-11 원본에서 확인한 문제 두 가지를 거른다.

    - 지자체가 모든 업체에 같은 번호를 적은 경우(김해시 300곳의 99%가 51-03-01뿐, 부산 강서구 19곳 모두 00-00-00):
      업체 n곳 이상인 지자체에서 한 가지 번호 묶음이 90% 이상이면 그 묶음을 가진 업체는 믿지 않는다.
    - 번호가 없거나 상위 분류만 적은 경우(00-00-00, 51-00-00, 51-99-00).
    """
    by_agency: dict[str, list[dict]] = {}
    for vendor in vendors:
        by_agency.setdefault(vendor.get("agency") or "", []).append(vendor)
    bulk: set[tuple[str, tuple[str, ...]]] = set()
    for agency, group in by_agency.items():
        counts: dict[tuple[str, ...], int] = {}
        for vendor in group:
            key = tuple(vendor["codes"])
            counts[key] = counts.get(key, 0) + 1
        top, n = max(counts.items(), key=lambda item: item[1])
        if len(group) >= BULK_MIN and n / len(group) >= BULK_SHARE:
            bulk.add((agency, top))
    for vendor in vendors:
        codes = tuple(vendor["codes"])
        vendor["codesReliable"] = (
            bool(codes)
            and not set(codes) <= GENERIC
            and ((vendor.get("agency") or "", codes) not in bulk)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--from", dest="source", help="이미 받은 행 목록 JSON(list 또는 {'rows': [...]})"
    )
    args = parser.parse_args()
    if args.source:
        data = json.loads(Path(args.source).read_text(encoding="utf-8"))
        rows = data["rows"] if isinstance(data, dict) else data
    else:
        rows = download()
    result = build(rows)
    OUT.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(f"{OUT.name}: {result['count']}곳 (원본 {len(rows)}행)")


if __name__ == "__main__":
    main()
