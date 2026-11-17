"""담당자가 손으로 넣은 글(흐름 이름, 혼입물 이름, 회사 이름)을 다른 조직에 보일 때의 규칙.

이 글은 믿지 않는 자료다. 지시문, 링크, 연락처, 근거 없는 숫자·조항이 섞여 들어올 수 있고, 그대로
메시지에 옮기면 상대 에이전트(모델)와 상대 화면에 그대로 간다(평가 L6i의 G5·G6, 개인정보).

- 흐름 이름은 상대에게 보내지 않는다. 상대에게는 코드가 정해진 낱말로 만든 이름(stream_label)이
  간다. 재질·형태·품목 가운데 정해진 낱말만 쓰고, 구분은 흐름 ID로 한다(ID는 이미 상대에게 열린
  값이다). 배출 기업은 자기 화면에서 원래 이름과 이 이름을 함께 본다.
- 혼입물 이름은 정해진 낱말과 맞으면 그 낱말을, 아니면 정리한 글(safe_text, 숫자 낱말 없이 짧게)을,
  그것도 비면 "기타 혼입물"을 쓴다.
- 회사 이름은 정리한 글(safe_text)로 보인다. 괄호·대괄호 속 덧붙임, 링크, 이메일, 전화번호,
  주민·사업자 번호, 지시문 표지(---, SYSTEM: 등) 뒤의 글, 연락처를 알리는 낱말 뒤의 글을 빼고
  길이를 줄인다. 법인 표시 "(주)" 같은 것은 남긴다.

자동 거절 조건 대조처럼 플랫폼 코드가 판단에 쓰는 곳은 원래 글을 그대로 쓴다. 여기 규칙은 보이는
글(메시지, 문서, 상대 화면, 모델 입력, 상대와 나누는 근거)에만 쓴다.
"""

from __future__ import annotations

import re
from typing import Any

LABEL_LIMIT = 40
CONTAMINANT_LIMIT = 16
OTHER_CONTAMINANT = "기타 혼입물"

# 법인 표시. 괄호를 지울 때 남긴다
_LEGAL_FORMS = ("(주)", "(유)", "(합)", "(재)", "(사)", "㈜")
_PAIRS = {
    "(": ")",
    "[": "]",
    "<": ">",
    "{": "}",
    "【": "】",
    "「": "」",
    "『": "』",
    "〈": "〉",
    "《": "》",
}
_URL = re.compile(
    r"(?i)(?:https?://|www\.)\S+|\b[\w.-]+\.(?:invalid|com|net|org|kr|io|co|me|ly|app|dev|xyz)\b\S*"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?82[-.\s]?)?0\d{1,2}[-.\s)]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_ID_NUMBER = re.compile(r"(?<!\d)(?:\d{6}-?[1-4]\d{6}|\d{3}-\d{2}-\d{5})(?!\d)")
# 이 표지가 나오면 그 뒤는 이름이 아니다(지시문, 구분선, 역할 표시)
_CUT = re.compile(
    r"(?i)-{3,}|={3,}|#{2,}|\*{3,}|[\r\n:;|]|\b(?:system|assistant|user|ignore|new task)\b"
)
# 연락처를 알리는 낱말과 그 뒤(담당 박예시, 문의 010-…)
_CONTACT = re.compile(
    r"\s*(?:담당자?|문의처?|연락처?|전화|휴대폰|이메일|메일|e-?mail)(?![가-힣]).*$"
)
# 직함이 붙은 사람 이름(이시험 과장, 박예시 님)과 그 뒤
_TITLED = re.compile(
    r"\s*[가-힣]{2,4}\s*(?:과장|대리|부장|차장|팀장|주임|사원|실장|이사|대표|선생님|님|씨)(?![가-힣]).*$"
)
_SPACES = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")

# 상대에게 보일 수 있는 정해진 낱말
_MATERIAL_CODE = re.compile(r"[A-Za-z]{2,6}(?:\+[A-Za-z]{2,6}){0,2}")
_MATERIAL_WORDS = ("철", "알루미늄", "구리", "스테인리스", "목재", "종이", "고무", "유리")
_FORM_WORDS = {
    "가공 전": "가공 전 스크랩",
    "압축": "압축품",
    "분쇄": "분쇄품",
    "펠렛": "펠렛",
    "필름": "필름",
}
_WASTE_ITEMS = ("폐플라스틱", "포장재", "금속 스크랩", "폐목재")
# 혼입물로 흔히 적는 낱말(긴 것부터 맞춘다)
CONTAMINANT_WORDS = tuple(
    sorted(
        (
            "금속 인서트",
            "종이 라벨",
            "금속",
            "철",
            "알루미늄",
            "구리",
            "PVC",
            "ABS",
            "PET",
            "PS",
            "PE",
            "PP",
            "PC",
            "PA",
            "종이",
            "라벨",
            "테이프",
            "필름",
            "비닐",
            "고무",
            "실리콘",
            "섬유",
            "목재",
            "모래",
            "흙",
            "먼지",
            "유리",
            "기름",
            "오일",
            "접착제",
            "잉크",
            "도료",
            "이물질",
        ),
        key=len,
        reverse=True,
    )
)


def _drop_brackets(text: str) -> str:
    """괄호로 싼 덧붙임을 지운다. 닫히지 않은 괄호는 그 뒤를 모두 지운다. 법인 표시는 남긴다."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if any(text.startswith(form, i) for form in _LEGAL_FORMS):
            form = next(f for f in _LEGAL_FORMS if text.startswith(f, i))
            out.append(form)
            i += len(form)
            continue
        char = text[i]
        if char in _PAIRS:
            end = text.find(_PAIRS[char], i + 1)
            if end < 0:
                break
            out.append(" ")
            i = end + 1
            continue
        if char in _PAIRS.values():
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def safe_text(value: Any, limit: int = LABEL_LIMIT) -> str:
    """사람이 넣은 이름을 다른 조직에 보일 수 있게 정리한 글. 남는 것이 없으면 빈 글."""
    text = str(value or "")
    text = _EMAIL.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _ID_NUMBER.sub(" ", text)
    text = _PHONE.sub(" ", text)
    cut = _CUT.search(text)
    if cut:
        text = text[: cut.start()]
    text = _drop_brackets(text)
    text = _CONTACT.sub("", text)
    text = _TITLED.sub("", text)
    text = _SPACES.sub(" ", text).strip(" ,.·-_/")
    if len(text) > limit:
        text = text[:limit].rstrip(" ,.·-_/")
    return text


def org_label(name: Any, fallback: str = "") -> str:
    """회사 이름을 보일 때. 정리하고 남는 것이 없으면 fallback."""
    return safe_text(name) or fallback


def material_word(material: Any) -> str | None:
    text = str(material or "").strip()
    if _MATERIAL_CODE.fullmatch(text):
        return text.upper()
    return text if text in _MATERIAL_WORDS else None


def stream_code(stream_id: Any) -> str:
    """흐름 ID에서 구분용 짧은 표시(영문·숫자만, 12자까지)를 만든다."""
    raw = str(stream_id or "").removeprefix("st_")
    return re.sub(r"[^0-9A-Za-z]+", "", raw)[:12]


def stream_label(stream: dict[str, Any]) -> str:
    """상대에게 보이는 흐름 이름. 예: "PP 압축품(sprue)", "LDPE 필름(film)", "폐기물(a)".

    흐름의 label이 이미 있으면(상대 쪽으로 넘어온 흐름) 그대로 쓴다.
    """
    given = stream.get("label")
    if isinstance(given, str) and given:
        return given
    parts = []
    material = material_word(stream.get("material"))
    if material:
        parts.append(material)
    form = _FORM_WORDS.get(str(stream.get("form") or ""))
    item = stream.get("wasteItem") if stream.get("wasteItem") in _WASTE_ITEMS else None
    parts.append(form or item or "폐기물")
    code = stream_code(stream.get("id"))
    base = " ".join(parts)
    return f"{base}({code})" if code else base


def contaminant_label(value: Any) -> str:
    """혼입물 하나를 보일 때. 정해진 낱말이 들어 있으면 그 낱말, 아니면 정리한 글."""
    if isinstance(value, dict):
        value = value.get("label") or value.get("name")
    # 링크·연락처를 먼저 지운다("https"의 PS 같은 우연한 맞음을 피한다)
    text = _PHONE.sub(" ", _URL.sub(" ", _EMAIL.sub(" ", str(value or ""))))
    for word in CONTAMINANT_WORDS:
        if word.isascii():
            if re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text, re.IGNORECASE):
                return word
        elif word in text:
            return word
    cleaned = " ".join(t for t in safe_text(text, 64).split() if not _DIGIT.search(t))
    return cleaned[:CONTAMINANT_LIMIT].strip() or OTHER_CONTAMINANT


def contaminant_labels(stream: dict[str, Any]) -> list[str]:
    """흐름의 (지우지 않은) 혼입물을 보일 이름으로. 순서를 지키고 겹치면 한 번만."""
    out: list[str] = []
    for item in stream.get("contaminants") or []:
        if isinstance(item, dict) and item.get("removed"):
            continue
        label = contaminant_label(item)
        if label not in out:
            out.append(label)
    return out


def counterparty_stream(stream: dict[str, Any]) -> dict[str, Any]:
    """상대 조직(과 그 쪽 모델)에 보일 흐름: 이름은 stream_label, 혼입물은 contaminant_label.

    다른 항목은 그대로다. 배출 기업만의 항목을 빼는 것은 judge.common.without_emitter_only가 한다.
    """
    out = dict(stream)
    out["name"] = stream_label(stream)
    out["label"] = out["name"]
    if stream.get("contaminants"):
        out["contaminants"] = [_shown_contaminant(c) for c in stream["contaminants"]]
    return out


def _shown_contaminant(item: Any) -> Any:
    label = contaminant_label(item)
    if not isinstance(item, dict):
        return label
    shown = {**item, "name": label}
    if "label" in item:
        shown["label"] = label
    return shown
