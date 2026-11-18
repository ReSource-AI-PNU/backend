"""담당자가 손으로 넣은 글(흐름 이름, 혼입물 이름, 회사 이름)을 다른 조직에 보일 때의 규칙.

이 글은 믿지 않는 자료다. 지시문, 링크, 연락처, 근거 없는 숫자·조항이 섞여 들어올 수 있고, 그대로
메시지에 옮기면 상대 에이전트(모델)와 상대 화면에 그대로 간다(평가 L6i의 G5·G6, 개인정보).

- 흐름 이름은 상대에게 보내지 않는다. 상대에게는 코드가 정해진 낱말로 만든 이름(stream_label)이
  간다. 재질(PP), 종류(불량품, 스프루·러너, 퍼지 덩어리, 포장 필름 …), 형태(분쇄품, 압축품 …)의
  정해진 낱말만 쓴다. 종류는 담당자가 넣은 이름·공정에서 정해진 낱말을 찾아 고르고(찾은 낱말만
  쓰고 이름 글은 옮기지 않는다), 없으면 형태·출처·품목에서 정한다. 같은 조직에 같은 이름이 둘
  이상이면 저장할 때 뒤에 차례 번호를 붙인다("PP 불량품 2", numbered_label). 무작위 ID는 붙이지
  않는다. 배출 기업은 자기 화면에서 원래 이름과 이 이름을 함께 본다.
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
# 형태(questions.FIELDS["form"]의 고르기)와 같은 뜻으로 적는 낱말 -> 정한 형태. 업체 매입 기준표의
# "분쇄품", 화면의 "펠릿"처럼 같은 형태를 다르게 적어도 같은 형태로 본다(form_key)
_FORM_ALIASES = {
    "가공 전": "가공 전",
    "가공전": "가공 전",
    "미가공": "가공 전",
    "압축": "압축",
    "압축품": "압축",
    "분쇄": "분쇄",
    "분쇄품": "분쇄",
    "분쇄물": "분쇄",
    "플레이크": "분쇄",
    "펠렛": "펠렛",
    "펠릿": "펠렛",
    "펠렛품": "펠렛",
    "필름": "필름",
}
# 형태의 보이는 이름. 가공 전은 형태 이름이 아니라 "아직 가공하지 않았다"는 뜻이라 이름에 넣지 않는다
_FORM_NOUNS = {"압축": "압축품", "분쇄": "분쇄품", "펠렛": "펠렛", "필름": "필름"}
_WASTE_ITEMS = ("폐플라스틱", "포장재", "금속 스크랩", "폐목재")
# 담당자가 넣은 이름·공정에서 찾는 종류 낱말(앞의 것이 먼저) -> 보이는 이름. 찾은 낱말만 쓴다
_KINDS = (
    (("불량",), "불량품"),
    (("스프루", "스푸루", "런너", "러너"), "스프루·러너"),
    (("퍼지",), "퍼지 덩어리"),
    (("스트레치",), "스트레치 필름"),
    (("필름", "비닐"), "필름"),
    (("마대", "톤백"), "마대"),
    (("팔레트", "파렛트", "빠레트"), "팔레트"),
    (("트레이",), "트레이"),
    (("자투리", "끝단", "트리밍"), "자투리"),
    (("분쇄", "플레이크"), "분쇄품"),
    (("펠렛", "펠릿"), "펠렛"),
    (("압축",), "압축품"),
    (("스크랩",), "스크랩"),
    (("포장재",), "포장재"),
)
# 불량품 앞에 붙이는 공정 낱말(이름·공정에서 찾는다)
_DEFECT_PROCESSES = ("사출", "압출", "블로우", "성형")
_PACKAGING = ("포장", "포장재")
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


def form_key(value: Any) -> str | None:
    """형태를 정한 낱말로. 모르는 글은 앞뒤 공백만 지운 그대로, 비면 None.

    "분쇄"와 "분쇄품", "펠렛"과 "펠릿"을 같은 형태로 본다(흐름의 형태와 매입 기준의 형태 대조).
    """
    text = _SPACES.sub(" ", str(value or "")).strip()
    if not text:
        return None
    return _FORM_ALIASES.get(text, _FORM_ALIASES.get(text.replace(" ", ""), text))


def _words(*values: Any) -> str:
    # 링크·연락처를 먼저 지운다(낱말이 링크 안에서 우연히 맞지 않게)
    return " ".join(_PHONE.sub(" ", _URL.sub(" ", _EMAIL.sub(" ", str(v or "")))) for v in values)


def _kind(stream: dict[str, Any]) -> str | None:
    """담당자가 넣은 이름·공정에서 찾은 종류의 보이는 이름. 정해진 낱말만 돌려준다."""
    text = _words(stream.get("name"), stream.get("process"))
    for needles, shown in _KINDS:
        if any(n in text for n in needles):
            break
    else:
        return None
    if shown == "불량품":
        process = next((p for p in _DEFECT_PROCESSES if p in text), None)
        return f"{process} 불량품" if process else shown
    if shown == "필름" and _packaging(stream):
        return "포장 필름"
    return shown


def _packaging(stream: dict[str, Any]) -> bool:
    return (
        stream.get("origin") == "PCR"
        or stream.get("wasteItem") == "포장재"
        or any(w in _words(stream.get("name"), stream.get("process")) for w in _PACKAGING)
    )


def base_label(stream: dict[str, Any]) -> str:
    """차례 번호 없는 보이는 이름. 재질 + 종류(+ 종류와 다른 가공 형태). 예: "PP 사출 불량품",
    "PP 불량품(분쇄)", "LDPE 포장 필름", "PP 분쇄품", "PP 공정 스크랩", "폐기물"."""
    form = form_key(stream.get("form"))
    form = form if form in _FORM_ALIASES.values() else None
    noun = _FORM_NOUNS.get(form or "")
    kind = _kind(stream)
    if kind and noun and noun not in kind and not (form == "필름" and "필름" in kind):
        kind = f"{kind}({form})"
    if not kind and noun:
        kind = "포장 필름" if form == "필름" and _packaging(stream) else noun
    if not kind:
        item = stream.get("wasteItem") if stream.get("wasteItem") in _WASTE_ITEMS else None
        if stream.get("origin") == "PIR" and item in (None, "폐플라스틱"):
            kind = "공정 스크랩"
        else:
            kind = item or "폐기물"
    material = material_word(stream.get("material"))
    return f"{material} {kind}" if material else kind


_ORDINAL = re.compile(r" (?:[2-9]|[1-9]\d+)")


def stream_label(stream: dict[str, Any]) -> str:
    """상대에게 보이는 흐름 이름. 예: "PP 사출 불량품", "LDPE 포장 필름", "PP 불량품 2".

    흐름에 저장된 label(numbered_label이 정한 것, 상대 쪽으로 넘어온 흐름의 것)은 지금 칸으로 만든
    이름이거나 그 뒤에 차례 번호만 붙은 것일 때만 쓴다. 다른 글이면 버리고 다시 만든다.
    """
    base = base_label(stream)
    given = stream.get("label")
    if isinstance(given, str) and given.startswith(base):
        rest = given[len(base) :]
        if not rest or _ORDINAL.fullmatch(rest):
            return given
    return base


def numbered_label(stream: dict[str, Any], others: list[dict[str, Any]]) -> str:
    """같은 조직의 다른 흐름(others)과 겹치지 않는 보이는 이름. 저장할 때 정해 흐름의 label에 둔다.

    처음 것은 번호 없이, 같은 이름이 이미 있으면 " 2", " 3"을 붙인다. 이미 정한 번호는 칸이 바뀌지
    않는 한 그대로 둔다(나중에 흐름이 늘어도 앞의 이름이 바뀌지 않는다).
    """
    taken = {stream_label(o) for o in others if o.get("id") != stream.get("id")}
    current = stream_label(stream)
    if current not in taken:
        return current
    base, number = base_label(stream), 2
    while f"{base} {number}" in taken:
        number += 1
    return f"{base} {number}"


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
