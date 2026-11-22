"""사진 속 재질 표기 읽기. 탐지 모델은 색과 모양만 보므로 수지 종류(PP·PE·ABS)를 가르지 못한다.
포대 라벨, 분리배출 표시("플라스틱 PP"), 성형품의 재질 표시(ISO 11469 ">PP<", ">PE-HD<")는 글자로 적혀 있으니
Document Intelligence 글자 읽기(prebuilt-read)로 읽어 재질 후보를 낸다.

- 표기는 포장이나 부품에 적힌 것이고 안에 든 것과 다를 수 있다. 그래서 흐름의 재질을 바로 바꾸지 않는다.
  담당자가 화면에서 "이 재질로 등록"을 눌러야 재질이 되고(service.update_stream의 materialSource="label"),
  그때 재질의 출처(materialBasis)가 "label"이 된다.
- 읽은 글은 믿을 수 없는 입력이다. 정해진 재질 이름만 골라 쓰고, 근거에 남기는 원문은 글자를 걸러 짧게 자른다.
- PE·PS·PC·PA·OTHER처럼 다른 뜻으로도 쓰이는 짧은 글자는 재질 문맥(재질, 플라스틱, 비닐, 수지, >…<)이 있거나
  그 글자만 적힌 짧은 줄(분리배출 표시 안의 글자)일 때만 재질로 본다.
- 영상과 Document Intelligence가 받지 않는 사진 형식(webp)은 읽지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.intake.documents import API_VERSION, LOW_CONFIDENCE, DocumentIntelligenceClient

MODEL = "prebuilt-read"
SOURCE_NAME = "Document Intelligence 글자 읽기"
# Document Intelligence 글자 읽기가 받는 사진 형식(webp는 받지 않는다)
READ_TYPES = ("jpg", "jpeg", "png", "bmp", "tif", "tiff", "heif")
OTHER = "OTHER"
# 이름 → 재질. 긴 이름을 먼저 맞춘다(HDPE가 PE로 읽히지 않게)
_ALIASES = {
    "LLDPE": "LLDPE",
    "PE-LLD": "LLDPE",
    "HDPE": "HDPE",
    "PE-HD": "HDPE",
    "HD-PE": "HDPE",
    "LDPE": "LDPE",
    "PE-LD": "LDPE",
    "LD-PE": "LDPE",
    "PETE": "PET",
    "PET": "PET",
    "PVC": "PVC",
    "ABS": "ABS",
    "PP": "PP",
    "PE": "PE",
    "PS": "PS",
    "PC": "PC",
    "PA": "PA",
    "OTHER": OTHER,
}
# 다른 뜻으로도 쓰이는 짧은 글자. 재질 문맥이 있거나 짧은 줄일 때만 재질로 본다
_AMBIGUOUS = {"PE", "PS", "PC", "PA", OTHER}
_KOREAN = (
    (re.compile(r"고밀도\s*폴리\s*에틸렌"), "HDPE"),
    (re.compile(r"저밀도\s*폴리\s*에틸렌"), "LDPE"),
    (re.compile(r"폴리\s*에틸렌\s*테레프탈레이트"), "PET"),
    (re.compile(r"폴리\s*프로필렌"), "PP"),
    (re.compile(r"폴리\s*에틸렌"), "PE"),
    (re.compile(r"폴리\s*스티렌"), "PS"),
    (re.compile(r"폴리\s*염화\s*비닐"), "PVC"),
    (re.compile(r"폴리\s*카보네이트"), "PC"),
    (re.compile(r"폴리\s*아마이드|나일론"), "PA"),
)
_TOKEN = re.compile(
    r"(?<![A-Za-z])("
    + "|".join(re.escape(name) for name in sorted(_ALIASES, key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_CONTEXT = re.compile(
    r"재질|플라스틱|비닐|수지|합성|분리\s*배출|material|resin|[>＞].*[<＜]|♻", re.IGNORECASE
)
SHORT_LINE = 8
# 근거에 남기는 원문: 한글·영문·숫자와 몇 가지 기호만 두고 자른다
_SAFE = re.compile(r"[^0-9A-Za-z가-힣\s+/\-<>().,%]")
EXCERPT_CHARS = 40
MAX_LINES = 5


def readable(filename: str) -> bool:
    """글자 읽기에 보낼 수 있는 사진인가(확장자로 본다)."""
    name = str(filename or "")
    return "." in name and name.rsplit(".", 1)[-1].lower() in READ_TYPES


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", _SAFE.sub("", str(text or ""))).strip()[:EXCERPT_CHARS]


def _line_confidence(line: dict[str, Any], words: list[dict[str, Any]]) -> float | None:
    """줄에 든 낱말들의 가장 낮은 확신도. 낱말 위치(span)가 없으면 None."""
    spans = [
        (int(s.get("offset", 0)), int(s.get("offset", 0)) + int(s.get("length", 0)))
        for s in line.get("spans") or []
    ]
    found = []
    for word in words:
        span = word.get("span") or {}
        start = span.get("offset")
        if start is None or not isinstance(word.get("confidence"), (int, float)):
            continue
        if any(a <= int(start) < b for a, b in spans):
            found.append(float(word["confidence"]))
    return min(found) if found else None


def materials_in(text: str) -> list[str]:
    """한 줄에서 찾은 재질(나온 순서, 중복 없이)."""
    line = str(text or "")
    found: list[tuple[int, str]] = []
    rest = line
    for pattern, material in _KOREAN:
        for match in pattern.finditer(rest):
            found.append((match.start(), material))
        rest = pattern.sub(lambda m: " " * len(m.group(0)), rest)
    context = bool(_CONTEXT.search(line)) or len(line.strip()) <= SHORT_LINE
    for match in _TOKEN.finditer(rest):
        material = _ALIASES[match.group(1).upper()]
        if material in _AMBIGUOUS and not context:
            continue
        found.append((match.start(), material))
    out: list[str] = []
    for _, material in sorted(found):
        if material not in out:
            out.append(material)
    return out


def parse(result: dict[str, Any]) -> list[dict[str, Any]]:
    """글자 읽기 결과(analyzeResult)에서 재질 표기를 찾는다. 재질마다 가장 확신도가 높은 줄 하나를 남긴다.

    [{"material", "text"(걸러 자른 줄), "confidence"(줄 낱말의 가장 낮은 확신도 | None), "lowConfidence",
      "together"(같은 줄에 함께 적힌 다른 재질, 복합 재질 표기)}]
    """
    best: dict[str, dict[str, Any]] = {}
    for page in result.get("pages") or []:
        words = list(page.get("words") or [])
        for line in page.get("lines") or []:
            content = str(line.get("content") or "")
            materials = materials_in(content)
            if not materials:
                continue
            confidence = _line_confidence(line, words)
            for material in materials:
                item = {
                    "material": material,
                    "text": clean(content),
                    "confidence": confidence,
                    "lowConfidence": confidence is not None and confidence < LOW_CONFIDENCE,
                    "together": [m for m in materials if m != material],
                }
                old = best.get(material)
                if old is None or (confidence or 0) > (old["confidence"] or 0):
                    best[material] = item
    return list(best.values())


def suggestion(found: list[dict[str, Any]]) -> str | None:
    """재질 하나로 좁혀질 때만 등록 후보를 낸다. 여러 재질이 적혔거나 OTHER(복합 재질)이거나 확신도가 낮으면 None."""
    if len(found) != 1:
        return None
    item = found[0]
    if item["material"] == OTHER or item["lowConfidence"] or item["together"]:
        return None
    return item["material"]


def read(client: DocumentIntelligenceClient, data: bytes) -> list[dict[str, Any]]:
    """사진 한 장의 재질 표기. 연결이나 분석이 실패하면 DocumentError가 난다."""
    return parse(client.analyze(data, model=MODEL))


def register(
    registry: Any,
    org_id: str,
    stream: dict[str, Any],
    found: list[dict[str, Any]],
    filename: str,
    today: str,
) -> dict[str, Any]:
    """찾은 표기를 model 근거 하나로 등록하고 흐름의 photo.labelMaterials에 넣을 값을 돌려준다.

    찾은 것이 없으면 근거를 만들지 않는다.
    """
    picked = suggestion(found)
    out = {"found": found, "suggestion": picked, "evidence": [], "file": clean(filename)}
    if not found:
        return out
    lines = []
    for item in found[:MAX_LINES]:
        note = " (확신도 낮음)" if item["lowConfidence"] else ""
        lines.append(f"{item['material']}: 「{item['text']}」{note}")
    evidence = registry.put(
        Evidence(
            id=ids.model_id("document-intelligence-read", API_VERSION, f"{stream['id']}-표기재질"),
            kind="model",
            label="사진 속 재질 표기",
            value=picked,
            excerpt="사진에서 읽은 재질 표기. "
            + "; ".join(lines)
            + ". 포장·부품에 적힌 표기라 안에 든 것과 다를 수 있습니다.",
            source=f"{SOURCE_NAME} ({MODEL} {API_VERSION}) · {clean(filename)}",
            as_of=today,
            access=(org_id,),
            meta={
                "ownerOrgId": org_id,
                "streamId": stream["id"],
                "file": clean(filename),
                "found": found,
                "suggestion": picked,
            },
        )
    )
    out["evidence"] = [evidence.id]
    return out
