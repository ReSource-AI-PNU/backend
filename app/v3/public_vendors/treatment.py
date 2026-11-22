"""허가 명부 업체에 2024년 처리 실적(공표 결과표 한 줄)을 붙인다. 표시용이며 손익 계산과 배정에는 쓰지 않는다.

- 실적은 scripts/v3/public_vendors/treatment.py가 「2024 전국 폐기물 발생 및 처리현황」 결과표의 재활용업 시트
  셋(8-가 중간, 8-나 최종, 8-다 종합)에서 뽑은 treatment2024.v1.json이다.
- 두 자료에는 공통 번호가 없어 상호와 소재지로 잇는다. 틀린 실적을 붙이느니 비워 두는 쪽을 고른다.
  1. 시도와 시군구가 같아야 한다(명부 주소의 앞 두 낱말 ↔ 결과표의 시도·시군구 칸). 창원시처럼 구가 있는
     시는 두 주소에 모두 구가 적혀 있으면 구도 같아야 한다.
  2. 상호가 같아야 한다. ㈜·(주)·주식회사·유한회사·농업회사법인 같은 회사 형태 표기, 공백·괄호·점, 결과표의
     "(구 옛 상호)" 표기는 지우고 비교한다. 비슷한 상호(포함 관계 등)는 잇지 않는다.
  3. 같은 업체가 여러 시트에 있으면 명부 줄의 업종(중간·최종·종합재활용업)과 같은 시트의 줄만 쓴다. 시트끼리
     더하지 않는다: 같은 물량을 두 허가로 나눠 적었는지 겹쳐 적었는지 알 수 없고, 실제로 중간·종합에 같은
     처리량을 똑같이 적은 업체가 있다(김해 성광스틸, 우성메탈). 업종이 같은 시트에 줄이 없으면(2024년 뒤
     업종을 바꾼 경우) 다른 시트의 줄을 그 시트 이름과 함께 쓴다.
  4. 그래도 줄이 둘 이상이면 소재지(도로명+건물번호, 또는 동·리+번지)가 명부 주소와 맞는 줄 하나만 쓴다.
     하나로 줄지 않으면 붙이지 않는다.
  5. 남은 한 줄도 두 주소에 같은 종류의 번지(도로명 또는 지번)가 있는데 하나도 맞지 않으면 다른 사업장일 수
     있어 붙이지 않는다. 한쪽만 도로명이고 다른 쪽이 지번이면 비교할 수 없어 상호·시군구만으로 붙인다.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

TREATMENT_FILE = Path(__file__).resolve().parent / "treatment2024.v1.json"

# 명부 주소의 첫 낱말 -> 결과표의 시도 칸. 전남광주통합특별시는 2024년 결과표에서 전남이나 광주다
SIDO = {
    "서울특별시": ("서울",),
    "부산광역시": ("부산",),
    "대구광역시": ("대구",),
    "인천광역시": ("인천",),
    "광주광역시": ("광주",),
    "대전광역시": ("대전",),
    "울산광역시": ("울산",),
    "세종특별자치시": ("세종",),
    "경기도": ("경기",),
    "강원특별자치도": ("강원",),
    "강원도": ("강원",),
    "충청북도": ("충북",),
    "충청남도": ("충남",),
    "전북특별자치도": ("전북",),
    "전라북도": ("전북",),
    "전라남도": ("전남",),
    "전남광주통합특별시": ("전남", "광주"),
    "경상북도": ("경북",),
    "경상남도": ("경남",),
    "제주특별자치도": ("제주",),
}
# 명부 업종 -> 결과표 시트
SHEET_OF = {
    "중간재활용업": "8-가. 재활용처리(중간)",
    "최종재활용업": "8-나. 재활용처리(최종)",
    "종합재활용업": "8-다. 재활용처리(종합)",
}
_FORMS = re.compile(
    r"주식회사|유한회사|합자회사|합명회사|농업회사법인|영농조합법인|사단법인|재단법인|"
    r"\(주\)|\(유\)|\(합\)|（주）|㈜|㈐"
)
_FORMER = re.compile(
    r"[(\[]\s*구\s*[:：]?[^)\]]*[)\]]"
)  # "(구 일성에너지㈜)", "[구: ㈜티에스케이이앤피 울산2공장]"
_PUNCT = re.compile(r"[\s()（）\[\]{}·.,&'\"-]")
_ROAD = re.compile(r"([가-힣A-Za-z0-9])(로|길)(\d+번?[가-힣]?길)?(\d+(?:-\d+)?)")
_LOT = re.compile(r"([가-힣])(동|리)(\d+(?:-\d+)?)")


def name_key(name: str) -> str:
    """상호 비교 키. 회사 형태 표기, 옛 상호 표기, 공백·괄호·점을 지운다."""
    text = _FORMER.sub("", name or "")
    return _PUNCT.sub("", _FORMS.sub("", text)).upper()


def _keys(address: str) -> tuple[set[str], set[str]]:
    """주소의 도로명+건물번호 키와 동·리+번지 키. 공백을 지우고 앞 글자 하나만 붙여 시·구 표기 차이를 넘는다."""
    flat = re.sub(r"\s+", "", address or "")
    roads = {"".join(m.groups(default="")) for m in _ROAD.finditer(flat)}
    lots = {"".join(m.groups()) for m in _LOT.finditer(flat)}
    return roads, lots


def _site(a: str, b: str) -> bool | None:
    """두 주소가 같은 사업장인지. 같은 종류의 번지가 하나라도 맞으면 True, 같은 종류가 있는데 다 다르면
    False, 비교할 같은 종류가 없으면 None."""
    road_a, lot_a = _keys(a)
    road_b, lot_b = _keys(b)
    if road_a & road_b or lot_a & lot_b:
        return True
    if (road_a and road_b) or (lot_a and lot_b):
        return False
    return None


def _gu(tokens: list[str], city: str) -> str | None:
    """시 아래 구(창원시 성산구의 성산구). 시도·시 낱말을 건너뛴 다음 낱말이 구로 끝나면 그것이다."""
    if not city.endswith("시"):
        return None
    rest = [
        t for t in tokens if t not in SIDO and t != city and not t.endswith(("광역시", "특별시"))
    ]
    return rest[0] if rest and rest[0].endswith("구") else None


@lru_cache(maxsize=1)
def table() -> dict[str, Any]:
    return json.loads(TREATMENT_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def index() -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """(시도, 시군구 첫 낱말, 상호 키) -> 결과표 줄."""
    out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in table()["rows"]:
        city = (row["sgg"] or "").split(" ")[0]
        out.setdefault((row["sido"], city, name_key(row["name"])), []).append(row)
    return out


def match(vendor: dict[str, Any]) -> dict[str, Any] | None:
    """허가 명부 업체 하나에 맞는 결과표 한 줄. 확실하지 않으면 None(모듈 설명의 규칙 1~5)."""
    tokens = (vendor.get("address") or "").split()
    if len(tokens) < 2 or tokens[0] not in SIDO:
        return None
    city = tokens[1]
    key = name_key(vendor.get("name") or "")
    if not key:
        return None
    rows = [r for sido in SIDO[tokens[0]] for r in index().get((sido, city, key), [])]
    # 1. 시 아래 구가 두 주소에 모두 있으면 같아야 한다
    gu = _gu(tokens, city)
    if gu:
        rows = [r for r in rows if _gu(f"{r['sgg']} {r['address']}".split(), city) in (None, gu)]
    if not rows:
        return None
    # 3. 명부 업종과 같은 시트의 줄. 없으면 다른 시트의 줄
    sheets = {sheet for word, sheet in SHEET_OF.items() if word in (vendor.get("business") or "")}
    rows = [r for r in rows if r["sheet"] in sheets] or rows
    # 4. 둘 이상이면 소재지가 맞는 줄 하나
    if len(rows) > 1:
        rows = [r for r in rows if _site(vendor["address"], r["address"])]
        if len(rows) != 1:
            return None
    # 5. 같은 종류의 번지가 모두 다르면 다른 사업장일 수 있다
    row = rows[0]
    if _site(vendor["address"], row["address"]) is False:
        return None
    return row


def summary(vendor: dict[str, Any]) -> dict[str, Any] | None:
    """후보에 붙일 2024년 처리 실적. 없으면 None."""
    row = match(vendor)
    if row is None or row["tonsPerYear"] is None:
        return None
    data = table()
    return {
        "tonsPerYear": row["tonsPerYear"],
        "capacityTonsPerDay": row["capacityTonsPerDay"],
        "sheet": row["sheet"],
        "row": row["row"],
        "returnedOn": row.get("returnedOn"),
        "source": data["source"],
        "url": data["url"],
        "asOf": data["asOf"],
        "published": data["published"],
    }
