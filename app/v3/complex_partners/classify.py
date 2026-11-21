"""등록공장의 생산품 문장에서 자원순환 역할을 가린다. 명부를 만들 때(scripts/v3/complex_partners/build.py)와
흐름의 재질을 볼 때 함께 쓴다.

- 생산품은 회사가 신고한 자유 기술형 문장이다("재생PE,PP칩", "플라스틱 사출품"). 쉼표 등으로 나눈 품목마다 본다.
- reprocess(받아서 재생원료로 만드는 곳): 재생·리사이클·분쇄 같은 말과 플라스틱 낱말이 한 품목에 함께 있다.
  "재생수지", "재생플라스틱 칩", "합성수지재생"이 여기에 든다.
- use(재생원료를 쓸 수 있는 곳): 플라스틱 제품(사출품, 필름, 용기 등)을 만든다. 폐기물을 바로 받는 곳이 아니라
  재생원료가 된 뒤의 수요처다.
- 고무·도료·금형·기계·섬유처럼 이름에 "수지", "PE" 같은 낱말이 들어가도 플라스틱 재활용과 무관한 품목은 뺀다.
  라틴 약어(PE, PP 등)는 앞뒤가 영문자가 아닐 때만 본다("PIPE"의 "PE"를 세지 않는다).
"""

from __future__ import annotations

import re

_LATIN = r"(?<![A-Za-z])(?:{})(?![A-Za-z])"
# 재질 낱말 -> 수지 계열
RESIN_WORDS: dict[str, re.Pattern[str]] = {
    "PP": re.compile(_LATIN.format("PP|CPP|OPP") + "|폴리프로필렌|피피", re.IGNORECASE),
    "PE": re.compile(_LATIN.format("PE|LDPE|HDPE|LLDPE") + "|폴리에틸렌", re.IGNORECASE),
    "PS": re.compile(_LATIN.format("PS|EPS|HIPS") + "|폴리스티렌|스티로폼", re.IGNORECASE),
    "ABS": re.compile(_LATIN.format("ABS"), re.IGNORECASE),
    "PET": re.compile(_LATIN.format("PET") + "|페트", re.IGNORECASE),
    "PVC": re.compile(_LATIN.format("PVC") + "|염화비닐", re.IGNORECASE),
}
_PLASTIC = re.compile(
    r"플라스틱|프라스틱|플래스틱|합성수지|비닐|필름|사출|펠렛|펠릿|펠레트|플레이크|칩|"
    + _LATIN.format("FILM|CHIP|PELLET"),
    re.IGNORECASE,
)
_RECYCLE = re.compile(r"재생|리사이클|재활용|분쇄|파쇄|recycl", re.IGNORECASE)
# 플라스틱 제품이라고 볼 말(재생원료 수요처). "합성수지"만 적은 화학 회사는 넣지 않는다
_PRODUCT = re.compile(
    r"사출|성형품|용기|필름|비닐|봉투|봉지|포장|시트|파이프|부품|제품|용품|케이스|상자|박스|팔레트|빠레트|"
    r"서랍|튜브|펠렛|펠릿|^플라스틱$|^프라스틱$|" + _LATIN.format("FILM|SHEET"),
    re.IGNORECASE,
)
# 플라스틱 재활용과 무관한 품목(고무·도료·기계·섬유·금속·연료 등)
_NOT = re.compile(
    r"고무|EPDM|우레탄|에폭시|타이어|드럼|아스콘|아스팔트|골재|유류|오일|폐유|윤활|금속|고철|비철|철강|강판|"
    r"알루미늄|구리|종이|골판지|펄프|섬유|원단|직물|화이버|도료|페인트|도금|금형|기계|성형기|사출기|발포기|설비|"
    r"장비|접착|코팅|FRP|강화플라스틱|계면|토너|잉크|목재|아크릴|페놀|멜라민|메라민|실리콘|불포화|에스테르|초산비닐|"
    r"첨가제|연료|SRF|에너지|선박|합성수지선|전선|케이블|커팅기|실링기|절단기|분쇄기|파쇄기|포장기|제조기|칩마운트|반도체",
    re.IGNORECASE,
)
# 필름류 품목("비닐포장지", "PE FILM"). 수지를 적지 않은 필름은 PE 계열 흐름에만 수요처로 둔다
_FILM = re.compile(r"비닐|필름|봉투|봉지|포장지|" + _LATIN.format("FILM"), re.IGNORECASE)
# 생산품 어디에든 있으면 재생원료 수요처로 두지 않는 회사(강화플라스틱·선박 제조)
_NOT_MAKER = re.compile(r"FRP|선박|조선", re.IGNORECASE)
_SPLIT = re.compile(r"[,，/;·、]|\s및\s")


def items(product: str) -> list[str]:
    """생산품 문장을 품목으로 나눈다. 빈 품목과 "-"는 버린다."""
    return [x.strip() for x in _SPLIT.split(product or "") if x.strip() and x.strip() != "-"]


def resins(text: str) -> list[str]:
    """글에 적힌 수지 계열(RESIN_WORDS 순서)."""
    return [name for name, pattern in RESIN_WORDS.items() if pattern.search(text or "")]


def _plastic(item: str) -> bool:
    """플라스틱 품목인가. "수지"만 있으면 화학 회사의 수지일 수 있어, 재생과 함께 적혔을 때만 센다("재생수지")."""
    recycled_resin = bool(_RECYCLE.search(item)) and "수지" in item
    return bool(_PLASTIC.search(item) or resins(item) or recycled_resin) and not _NOT.search(item)


def roles(product: str) -> dict[str, dict[str, list[str]]]:
    """생산품의 자원순환 역할. {"reprocess": {"items": [...], "resins": [...]}, "use": {...}}, 없는 역할은 빠진다.

    reprocess의 수지는 재생 품목만이 아니라 플라스틱 품목 전체에서 읽는다("재생PE, PP칩"이면 PE·PP).
    """
    parts = [item for item in items(product) if _plastic(item)]
    out: dict[str, dict[str, list[str]]] = {}
    recycled = [item for item in parts if _RECYCLE.search(item)]
    if recycled:
        out["reprocess"] = {"items": recycled, "resins": resins(" ".join(parts))}
    made = [item for item in parts if not _RECYCLE.search(item) and _PRODUCT.search(item)]
    # 배·FRP를 만드는 회사의 "합성수지제품"은 강화플라스틱이라 재생원료 수요처로 두지 않는다
    if made and not recycled and not _NOT_MAKER.search(product or ""):
        out["use"] = {"items": made, "resins": resins(" ".join(made))}
    return out


def film_only(found: list[str]) -> bool:
    """품목이 모두 필름류인가."""
    return bool(found) and all(_FILM.search(item) for item in found)


def families(material: str | None) -> list[str]:
    """흐름 재질의 수지 계열. "PP+ABS" -> ["PP", "ABS"], "LDPE" -> ["PE"]. 모르면 빈 목록."""
    return resins(re.sub(r"[+/,·]", " ", str(material or "")))
