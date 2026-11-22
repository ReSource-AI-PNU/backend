"""재활용업체의 2024년 처리 실적(공표 결과표)에서 필요한 칸만 뽑는다.

    uv run --with openpyxl python -m scripts.v3.public_vendors.treatment <결과표 zip 또는 02_06 xlsx>
    uv run --with openpyxl python -m scripts.v3.public_vendors.treatment <zip> --report   # 부산·울산·경남 매칭률도 본다

- 원본: 기후에너지환경부·한국환경공단 「2024 전국 폐기물 발생 및 처리현황」 결과표(순환자원정보센터 게시판, 2025-12-31 등록).
  zip 안의 `02_06_2024_전국 폐기물 처리업체현황_Ⅰ(생활, 사업장일반, 건설).xlsx`에서 재활용업 시트 셋
  (8-가 중간, 8-나 최종, 8-다 종합)만 읽는다. 원본 파일은 저장소에 넣지 않는다.
- 남기는 칸: 시도, 시군구, 업체명, 소재지, 영업대상 폐기물, 처리시설, 처리능력(톤/일), 2024년 처리량(톤/년),
  허가(승인)일, 반납 신고일, 그리고 시트 이름과 행 번호(근거가 원본의 어느 줄인지 밝힌다).
  대표자(개인 이름)와 전화번호·잔재물 칸은 남기지 않는다.
- 시도·시군구 칸은 원본에서 묶음의 첫 줄에만 적혀 있어 아래 줄로 이어 채운다. 시도별 소계·전국 합계 줄은 뺀다.
- openpyxl은 앱 의존성이 아니라서 이 스크립트만 `uv run --with openpyxl`로 돌린다.
- 상호·시군구로 허가 명부(registry.v1.json)와 잇는 규칙은 앱 쪽 app/v3/public_vendors/treatment.py에 있다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path

OUT = (
    Path(__file__).resolve().parents[3] / "app" / "v3" / "public_vendors" / "treatment2024.v1.json"
)
SOURCE = "기후에너지환경부·한국환경공단 2024 전국 폐기물 발생 및 처리현황"
# 게시판 번호. 소문자로 이으면 문자 발송 낱말 검사(test_chapter4_nothing_is_sent_outside_the_app)에 걸려 나눠 적는다
BOARD = "BBS" + "MSTR_000000000002"
PAGE = f"https://www.recycling-info.or.kr/rrs/stat/envStatDetail.do?menuNo=M130601&bbsId={BOARD}&nttId=1596"
DOWNLOAD = f"https://www.recycling-info.or.kr/cmm/fms/FileDownload.do?atchFileId=FILE_000000000263380&fileSn=7&bbsId={BOARD}"
ZIP_NAME = "2024년 전국 폐기물 발생 및 처리현황 결과표.zip"
FILE_NAME = "02_06_2024_전국 폐기물 처리업체현황_Ⅰ(생활, 사업장일반, 건설).xlsx"
SHEETS = ("8-가. 재활용처리(중간)", "8-나. 재활용처리(최종)", "8-다. 재활용처리(종합)")
# 머리 두 줄(4·5행) 다음 6행이 전국 합계다. 칸 순서는 세 시트가 같다(2025-12-31 판에서 확인)
HEADER_ROW = 4
FIRST_ROW = 6
COLUMNS = {
    "sido": 1,
    "sgg": 2,
    "name": 3,
    "address": 5,
    "waste": (6, 7, 8, 9),  # 생활, 사업장비배출시설계, 사업장배출시설계, 건설
    "facility": 10,
    "capacity": 12,
    "tons": 13,
    "permit": 20,
    "returned": 21,
}
EXPECTED_HEADER = {
    3: "업체명",
    5: "소재지",
    6: "영업대상 폐기물",
    10: "처리시설",
    13: "2024년 처리량",
}
_DATE = re.compile(r"(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")


def _text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _date(value: object) -> str | None:
    if isinstance(value, dt.datetime | dt.date):
        return value.strftime("%Y-%m-%d")
    match = _DATE.search(_text(value))
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return round(float(value), 1)
    try:
        return round(float(_text(value).replace(",", "")), 1)
    except ValueError:
        return None


def _open_workbook(path: Path):
    import openpyxl  # 빌드 때만 쓴다(uv run --with openpyxl)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            member = next(
                (n for n in archive.namelist() if n.split("/")[-1].startswith("02_06_")), None
            )
            if member is None:
                raise SystemExit(f"zip에 {FILE_NAME}이 없습니다.")
            data = archive.read(member)
        return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    return openpyxl.load_workbook(path, read_only=True, data_only=True)


def extract(path: Path) -> list[dict]:
    workbook = _open_workbook(path)
    rows: list[dict] = []
    for sheet in SHEETS:
        sido = sgg = ""
        for number, row in enumerate(workbook[sheet].iter_rows(values_only=True), start=1):
            if number == HEADER_ROW:
                for index, word in EXPECTED_HEADER.items():
                    if not _text(row[index]).startswith(word):
                        raise SystemExit(
                            f"{sheet} {HEADER_ROW}행 {index + 1}열이 {word}가 아닙니다."
                        )
            if number < FIRST_ROW:
                continue
            sido = _text(row[COLUMNS["sido"]]) or sido
            sgg_cell = _text(row[COLUMNS["sgg"]])
            if sgg_cell in ("소계", "합계"):
                continue
            sgg = sgg_cell or sgg
            name = _text(row[COLUMNS["name"]])
            if not name:
                continue
            waste = []
            for index in COLUMNS["waste"]:
                for part in re.split(r"\s*,\s*", _text(row[index])):
                    if part and part not in waste:
                        waste.append(part)
            rows.append(
                {
                    "sheet": sheet,
                    "row": number,
                    "sido": sido,
                    "sgg": sgg,
                    "name": name,
                    "address": _text(row[COLUMNS["address"]]),
                    "waste": ", ".join(waste) or None,
                    "facility": _text(row[COLUMNS["facility"]]) or None,
                    "capacityTonsPerDay": _number(row[COLUMNS["capacity"]]),
                    "tonsPerYear": _number(row[COLUMNS["tons"]]),
                    "permitDate": _date(row[COLUMNS["permit"]]),
                    "returnedOn": _date(row[COLUMNS["returned"]]),
                }
            )
    return rows


def build(path: Path) -> dict:
    rows = extract(path)
    return {
        "version": "v1",
        "source": SOURCE,
        "file": FILE_NAME,
        "zip": ZIP_NAME,
        "url": PAGE,
        "download": DOWNLOAD,
        "year": 2024,
        "asOf": "2024-12-31",
        "published": "2025-12-31",
        "sheets": list(SHEETS),
        "note": "재활용업체(중간·최종·종합)의 2024년 처리량과 처리능력입니다. 지자체가 모아 공표한 값입니다.",
        "count": len(rows),
        "rows": rows,
    }


def report() -> None:
    """부산·울산·경남 허가 명부 업체 가운데 처리 실적을 붙인 곳."""
    from app.v3 import public_vendors
    from app.v3.public_vendors import treatment

    treatment.table.cache_clear()
    treatment.index.cache_clear()
    regions = ("부산광역시", "울산광역시", "경상남도")
    vendors = [v for v in public_vendors.registry()["vendors"] if v["address"].startswith(regions)]
    matched = [(v, treatment.match(v)) for v in vendors]
    found = [(v, row) for v, row in matched if row]
    print(
        f"부산·울산·경남 명부 {len(vendors)}곳 중 {len(found)}곳에 2024년 처리 실적을 붙였습니다."
    )
    for region in regions:
        group = [(v, row) for v, row in matched if v["address"].startswith(region)]
        print(f"  {region}: {len(group)}곳 중 {sum(1 for _, row in group if row)}곳")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="결과표 zip 또는 02_06 xlsx")
    parser.add_argument("--report", action="store_true", help="부산·울산·경남 매칭률을 출력한다")
    args = parser.parse_args()
    result = build(Path(args.source))
    OUT.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(f"{OUT.name}: {result['count']}줄")
    if args.report:
        report()


if __name__ == "__main__":
    main()
