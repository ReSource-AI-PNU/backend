"""한국어 조사. 코드가 만드는 문장에서 이름 뒤 조사를 받침에 맞게 붙인다.

    josa("(주)미소환경", "와과")  -> "(주)미소환경과"
    josa("수분", "이가")          -> "수분이"
    josa("PP", "은는")            -> "PP는"
    josa("출처(PIR·PCR)", "이가") -> "출처(PIR·PCR)가"
    particle("서울", "으로")      -> "로"

"은(는)", "이(가)" 같은 표기를 화면에 내지 않으려고 쓴다. 표준 라이브러리만 쓴다.

마지막 글자를 읽는 규칙:
- 한글: 받침이 있는지와 그 받침이 ㄹ인지(으로/로)를 글자 코드에서 읽는다.
- 닫는 괄호로 끝나면 괄호 앞의 말을 본다("출처(PIR·PCR)" → "출처"). 말 전체가 괄호나 따옴표에
  싸여 있으면 안쪽을 본다. 따옴표, 마침표 같은 기호는 건너뛴다.
- 숫자: 우리말로 읽은 끝소리(0 영·십·백·천·만, 1 일, 3 삼, 6 육, 7 칠, 8 팔은 받침이 있다).
- 단위: 계약 3장의 단위는 읽는 소리로 본다(kg 킬로그램, % 퍼센트, t 톤 등). "kg/월"은 "월"을 본다.
- 영문: 대문자로 끝나면 글자 이름으로 읽는다(L 엘, M 엠, N 엔, R 알은 받침이 있다).
  소문자로 끝나는 낱말은 l, m, n, ng로 끝날 때만 받침이 있다고 본다(어림 규칙이다).
- 읽을 수 없으면 받침이 없는 것으로 본다.
"""

from __future__ import annotations

import re

# 조사 쌍: (받침 있을 때, 받침 없을 때). 으로/로는 ㄹ 받침일 때도 "로"다
PAIRS: dict[str, tuple[str, str]] = {
    "은는": ("은", "는"),
    "이가": ("이", "가"),
    "을를": ("을", "를"),
    "와과": ("과", "와"),
    "으로": ("으로", "로"),
    "이라": ("이라", "라"),
    "이나": ("이나", "나"),
    "아야": ("아", "야"),
}
_ALIASES = {
    "는": "은는",
    "은": "은는",
    "가": "이가",
    "이": "이가",
    "를": "을를",
    "을": "을를",
    "과": "와과",
    "와": "와과",
    "과와": "와과",
    "로": "으로",
    "으로로": "으로",
    "는은": "은는",
    "가이": "이가",
    "를을": "을를",
}

_OPEN = "([{<〈《「『【"
_CLOSE = ")]}>〉》」』】"
_SKIP = "'\"`‘’“”·.,!?:; \t\n*_~"
_HANGUL_FIRST, _HANGUL_LAST = 0xAC00, 0xD7A3
_RIEUL = 8  # 종성 ㄹ의 번호

# 숫자 한 자리를 읽은 끝소리: (받침 있음, ㄹ 받침)
_DIGITS = {
    "0": (True, False),  # 영, 십, 백, 천, 만
    "1": (True, True),  # 일
    "2": (False, False),  # 이
    "3": (True, False),  # 삼
    "4": (False, False),  # 사
    "5": (False, False),  # 오
    "6": (True, False),  # 육
    "7": (True, True),  # 칠
    "8": (True, True),  # 팔
    "9": (False, False),  # 구
}
# 대문자 글자 이름의 끝소리
_LETTER_FINAL = {"L": (True, True), "M": (True, False), "N": (True, False), "R": (True, True)}
# 단위를 읽은 끝 글자(계약 3장의 단위). "/" 뒤의 마지막 단위만 본다
_UNIT_READING = {
    "kg": "램",
    "t": "톤",
    "%": "트",
    "km": "터",
    "m": "터",
    "m3": "터",
    "tco2e": "톤",
    "kgco2e": "램",
}


def _strip(word: str) -> str:
    """끝의 기호를 걷어 내고, 끝의 괄호 덩어리는 그 앞의 말로 바꾼다."""
    text = str(word or "")
    for _ in range(8):
        text = text.rstrip(_SKIP)
        if not text or text[-1] not in _CLOSE:
            break
        depth, start = 0, -1
        for index in range(len(text) - 1, -1, -1):
            if text[index] in _CLOSE:
                depth += 1
            elif text[index] in _OPEN:
                depth -= 1
                if depth == 0:
                    start = index
                    break
        if start < 0:
            text = text[:-1]  # 짝이 없는 닫는 괄호
            continue
        before = text[:start].rstrip(_SKIP)
        # 괄호 앞에 말이 있으면 그 말을, 없으면(말 전체가 괄호 안이면) 안쪽을 본다
        text = before if before else text[start + 1 : -1]
    return text.rstrip(_SKIP)


def _final(word: str) -> tuple[bool, bool]:
    """(받침이 있는가, 그 받침이 ㄹ인가)."""
    text = _strip(word)
    if not text:
        return False, False
    last = text[-1]
    code = ord(last)
    if _HANGUL_FIRST <= code <= _HANGUL_LAST:
        jong = (code - _HANGUL_FIRST) % 28
        return jong != 0, jong == _RIEUL
    tail = text.rsplit("/", 1)[-1].rsplit(" ", 1)[-1].lstrip("0123456789.,+-−").lower()
    if tail in _UNIT_READING:
        return _final(_UNIT_READING[tail])
    if last.isdigit():
        return _DIGITS.get(last, (False, False))
    if last.isascii() and last.isalpha():
        if last.isupper():
            return _LETTER_FINAL.get(last, (False, False))
        lowered = text.lower()
        if lowered.endswith("ng") or last in "mn":
            return True, False
        if last == "l":
            return True, True
    return False, False


def particle(word: str, pair: str) -> str:
    """word 뒤에 올 조사만 돌려준다. pair는 "은는", "이가", "을를", "와과", "으로"(또는 "로")."""
    key = "".join(c for c in str(pair) if c not in "/() ")
    key = _ALIASES.get(key, key)
    if key not in PAIRS:
        raise ValueError(f"모르는 조사: {pair!r}")
    closed, opened = PAIRS[key]
    has_final, rieul = _final(word)
    if key == "으로":
        return closed if has_final and not rieul else opened
    return closed if has_final else opened


def josa(word: str, pair: str) -> str:
    """word에 조사를 붙인 글. josa("수분", "이가") -> "수분이"."""
    return f"{word}{particle(word, pair)}"


# 같은 낱말(한글이 들어간 두 글자 이상)이 빈칸을 두고 잇따라 나온 것. "처리비는 처리비는 {{x}}"
_REPEATED = re.compile(
    r"(?<![가-힣A-Za-z0-9])([가-힣][가-힣A-Za-z0-9]+)(?:[ \t]+\1)+(?=[\s.,!?)]|$)"
)


def drop_repeats(text: str) -> str:
    """잇따라 두 번 쓴 낱말을 한 번으로 줄인다(모델 글의 "처리비는 처리비는"). 다른 글은 그대로다."""
    return _REPEATED.sub(r"\1", str(text))
