"""같은 단지 자원순환 후보 명부(등록공장)를 공공데이터에서 만든다.

    uv run python -m scripts.v3.complex_partners.build            # 내려받아 app/v3/complex_partners/factories.v1.json을 다시 쓴다
    uv run python -m scripts.v3.complex_partners.build --from F   # 이미 받은 CSV나 ZIP(F)으로만 만든다

- 원본: 공공데이터포털 "한국산업단지공단_전국등록공장현황_등록공장현황자료"(https://www.data.go.kr/data/15105482/fileData.do).
  공장설립온라인지원시스템(팩토리온)에 등록된 공장의 회사명, 단지명, 생산품, 공장주소다. 이용허락범위 제한 없음.
  업종 코드·원자재·좌표·연락처는 없다. 단지 밖 개별입지 공장은 단지명이 비어 있다.
- 내려받기는 포털의 "다운로드" 버튼이 부르는 주소(/tcs/dss/selectFileDataDownload.do로 파일 ID를 받고
  /cmm/cmm/fileDownload.do로 ZIP을 받음)를 그대로 쓴다. 인증키가 필요 없다. CSV는 CP949다.
- complexes.v1.json의 단지(datasetNames)에 든 공장 가운데 생산품으로 자원순환 역할이 있는 곳만 남긴다(classify.roles).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from app.v3.complex_partners import classify

PK = "15105482"
PAGE = f"https://www.data.go.kr/data/{PK}/fileData.do"
DIR = Path(__file__).resolve().parents[3] / "app" / "v3" / "complex_partners"
OUT = DIR / "factories.v1.json"
COMPLEXES = DIR / "complexes.v1.json"
SOURCE = "한국산업단지공단 전국등록공장현황(공공데이터포털)"
NOTE = (
    "공장설립온라인지원시스템에 등록된 공장의 회사명, 단지명, 생산품, 공장주소입니다. 생산품은 회사가 신고한 "
    "문장이라 지금도 그 품목을 만드는지, 폐기물을 받을 수 있는지는 회사에 물어 확인해야 합니다."
)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": PAGE})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def download() -> tuple[bytes, str]:
    """(ZIP 바이트, 기준일 YYYY-MM-DD)."""
    meta = json.loads(
        _get(
            "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do?"
            + urllib.parse.urlencode({"publicDataPk": PK, "fileDetailSn": 1})
        )
    )
    file_id = meta["atchFileId"]
    as_of = _as_of(meta["dataSetFileDetailInfo"]["dataNm"])
    url = "https://www.data.go.kr/cmm/cmm/fileDownload.do?" + urllib.parse.urlencode(
        {"atchFileId": file_id, "fileDetailSn": 1}
    )
    return _get(url), as_of


def _as_of(name: str) -> str:
    found = re.search(r"(\d{4})(\d{2})(\d{2})", name)
    if not found:
        raise SystemExit(f"파일 이름에서 기준일을 찾지 못했습니다: {name}")
    return "-".join(found.groups())


def read_rows(raw: bytes) -> tuple[list[dict[str, str]], str | None]:
    """ZIP이면 안의 자료 CSV를 읽는다. (행, 파일 이름의 기준일)."""
    name = None
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if re.search(r"_\d{8}\.csv$", n)]
            if len(names) != 1:
                raise SystemExit(
                    f"ZIP에서 자료 CSV를 하나로 고르지 못했습니다: {archive.namelist()}"
                )
            name = names[0]
            raw = archive.read(name)
    text = raw.decode("cp949", errors="replace")
    return list(csv.DictReader(io.StringIO(text))), (_as_of(name) if name else None)


def build(rows: list[dict[str, str]], as_of: str) -> dict:
    complexes = json.loads(COMPLEXES.read_text(encoding="utf-8"))["complexes"]
    by_name = {n: c for c in complexes for n in c["datasetNames"]}
    factories = []
    for row in rows:
        complex_name = (row.get("단지명") or "").strip()
        found = by_name.get(complex_name)
        name = (row.get("회사명") or "").strip()
        product = (row.get("생산품") or "").strip()
        address = (row.get("공장주소") or "").strip()
        if found is None or not name or not product:
            continue
        roles = classify.roles(product)
        if not roles:
            continue
        key = f"{name}|{address}|{product}".encode()
        factories.append(
            {
                "id": "cf_" + hashlib.sha1(key).hexdigest()[:10],
                "name": name,
                "complexId": found["id"],
                "complexName": complex_name,
                "product": product,
                "address": address,
                "roles": roles,
            }
        )
    factories.sort(key=lambda f: f["id"])
    seen: set[str] = set()
    unique = [f for f in factories if not (f["id"] in seen or seen.add(f["id"]))]
    return {
        "version": "v1",
        "source": SOURCE,
        "url": PAGE,
        "dataset": PK,
        "asOf": as_of,
        "note": NOTE,
        "count": len(unique),
        "factories": unique,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", type=Path, help="이미 받은 CSV나 ZIP")
    parser.add_argument("--as-of", help="기준일(YYYY-MM-DD). CSV만 줄 때 필요하다")
    args = parser.parse_args()
    if args.source:
        rows, as_of = read_rows(args.source.read_bytes())
        as_of = args.as_of or as_of or _as_of(args.source.name)
    else:
        raw, as_of = download()
        rows, _ = read_rows(raw)
    data = build(rows, as_of)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for factory in data["factories"]:
        for role in factory["roles"]:
            counts[f"{factory['complexId']}:{role}"] = (
                counts.get(f"{factory['complexId']}:{role}", 0) + 1
            )
    print(f"{OUT} · 공장 {data['count']}곳 · 기준일 {as_of} · {dict(sorted(counts.items()))}")


if __name__ == "__main__":
    main()
