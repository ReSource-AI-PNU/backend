"""산업단지별 폐합성수지 발생·처리 집계를 공공 통계 결과표에서 뽑는다(2026-11-22).

    uv run --with openpyxl python -m scripts.v3.complex_partners.stats --xlsx <결과표 xlsx>

- 원본: 기후에너지환경부·한국환경공단 「2024 전국 폐기물 발생 및 처리현황」 결과표 zip 안의
  `02_09_2024_(부록)산업단지별 발생 및 처리현황(사업장배출시설계폐기물, 사업장지정폐기물).xlsx`,
  시트 `1-가. 사업장배출시설계 산업단지별 발생량`. 자원순환정보시스템 게시물(2025-12-31 등록)에서 받는다.
- 단지 이름은 통계표의 이름이 회사 단지 목록(complexes.v1.json)과 달라 아래 SHEET_NAMES로 잇는다.
- 회사 단위가 아니라 단지 전체 합계다. 다른 기업 물량을 화면 계산에 쓰지 않고 단지 현황으로만 보인다.
- 원본 파일은 저장소에 넣지 않고, 필요한 칸만 app/v3/complex_partners/stats.v1.json에 쓴다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[3] / "app" / "v3" / "complex_partners" / "stats.v1.json"
SHEET = "1-가. 사업장배출시설계 산업단지별 발생량"
WASTE = "폐합성수지류"
# complexes.v1.json의 id -> 통계표의 산업단지 이름
SHEET_NAMES = {
    "noksan": "(국가)부산 명지녹산국가산업단지",
    "sinho": "(일반)부산 신호일반산업단지",
    "hwajeon": "(일반)부산 화전지구일반산업단지",
    "mieum": "(일반)부산 미음일반산업단지",
    "jisa": "(일반)부산 부산과학일반산업단지",
    "sasang": "(일반)부산 사상재생사업지구",
    "changwon": "(국가)창원 창원국가산업단지",
    "yangsan": "(일반)양산 양산일반산업단지",
}
# 표의 열(0부터): 시도, 시군구, 산업단지, 대분류, 종류, 세부, 발생량, 재활용 소계, 물질재활용, 에너지회수, 소각, 매립, 기타
COLUMNS = {
    "generated": 6,
    "recycled": 7,
    "materialRecycled": 8,
    "energyRecovered": 9,
    "incinerated": 10,
    "landfilled": 11,
    "other": 12,
}


def build(xlsx: Path, url: str) -> dict:
    import openpyxl

    sheet = openpyxl.load_workbook(xlsx, read_only=True)[SHEET]
    wanted = {name: key for key, name in SHEET_NAMES.items()}
    found: dict[str, dict] = {}
    for row in sheet.iter_rows(min_row=6, values_only=True):
        if row[2] in wanted and row[4] == WASTE:
            values = {k: round(float(row[i] or 0), 2) for k, i in COLUMNS.items()}
            found[wanted[row[2]]] = {"sheetName": row[2], "district": row[1], **values}
    missing = sorted(set(SHEET_NAMES) - set(found))
    if missing:
        raise SystemExit(f"통계표에서 찾지 못한 단지: {missing}")
    return {
        "version": "v1",
        "source": "기후에너지환경부·한국환경공단 전국 폐기물 발생 및 처리현황 (부록) 산업단지별 발생 및 처리현황",
        "url": url,
        "sheet": SHEET,
        "waste": WASTE,
        "dataYear": "2024",
        "publishedOn": "2025-12-31",
        "unit": "톤/년",
        "note": "사업장배출시설계 폐기물의 단지 전체 합계다. 회사별 값이 아니며, 처리 방법은 재활용(물질재활용·에너지회수)·소각·매립·기타로 나뉜다.",
        "complexes": {key: found[key] for key in SHEET_NAMES},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xlsx", type=Path, required=True)
    # 게시물 주소. 주지 않으면 지금 stats.v1.json에 적힌 주소를 그대로 쓴다
    parser.add_argument("--url", default=None)
    args = parser.parse_args()
    url = args.url or json.loads(OUT.read_text(encoding="utf-8"))["url"]
    data = build(args.xlsx, url)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for key, item in data["complexes"].items():
        print(key, item["sheetName"], item["generated"], "톤/년")


if __name__ == "__main__":
    main()
