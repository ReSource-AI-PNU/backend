"""숫자 없이 조건을 약속하는 낱말(상수). 가드레일 검사(guardrails.check_term_words)가 쓴다.

근거 검증기는 숫자와 조항 인용을 본다. "무상으로 수거", "주말에도 수거", "전량 매입"처럼 숫자 없이
조건을 뜻하는 말은 검증기를 그대로 지난다. 그런 낱말이 메시지에 있으면 그에 해당하는 terms 항목이나
claim이 있어야 하고, 없으면 UNBACKED_TERM으로 거부한다.

낱말은 보수적으로 골랐다(오탐을 줄이려는 것). 조건을 뜻하는 것이 분명한 낱말만 넣고, 일상 표현과
겹치는 말("항상", "바로", "모두" 등)은 넣지 않았다. 조직·흐름 이름에 든 낱말은 보지 않는다.
부정문("주말에는 수거하지 않습니다")도 거부한다. 요일과 단가는 terms와 claim으로 적으면 된다.

needs(무엇이 있어야 하는가):
- zero_price: 값이 0인 금액 claim(원/kg, 원/t, 원/회, 원/월, 원/km, 원). 업체 측이면 terms의 단가 0도 된다
- weekend_days: terms.days에 토요일이나 일요일
- every_day: terms.days에 일곱 요일 모두
- quantity: 물량 claim(kg, t, kg/월, t/월, m3)
- none: 해당하는 terms 항목이 없다. 늘 거부한다(값은 claim으로 적는다)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CODE = "UNBACKED_TERM"

ZERO_PRICE = "zero_price"
WEEKEND_DAYS = "weekend_days"
EVERY_DAY = "every_day"
QUANTITY = "quantity"
NONE = "none"

PRICE_UNITS = ("원/kg", "원/t", "원/회", "원/월", "원/km", "원")
QUANTITY_UNITS = ("kg", "t", "kg/월", "t/월", "m3")
WEEKEND = ("토", "일")


@dataclass(frozen=True)
class TermWord:
    key: str
    pattern: re.Pattern[str]
    needs: str
    hint: str  # 다시 쓰는 모델에게 돌려주는 안내(한국어)


TERM_WORDS: tuple[TermWord, ...] = (
    TermWord(
        "free",
        re.compile(r"무상|무료|공짜"),
        ZERO_PRICE,
        "단가나 처리비가 0이면 그 값을 claim으로 적고, 아니면 이 낱말을 빼세요.",
    ),
    TermWord(
        "half_price",
        re.compile(r"반값|반액|절반\s*(?:가격|값|단가)"),
        NONE,
        "할인은 낱말로 적을 수 없습니다. 단가를 claim으로 적으세요.",
    ),
    TermWord(
        "weekend",
        re.compile(r"주말"),
        WEEKEND_DAYS,
        "수거 요일은 terms.days에 요일로 적으세요.",
    ),
    TermWord(
        "holiday",
        re.compile(r"공휴일|휴일"),
        NONE,
        "공휴일 수거는 조건 항목이 없습니다. 수거 요일은 terms.days에 요일로 적으세요.",
    ),
    TermWord(
        "every_day",
        # "매일 발생하는"처럼 흐름이 생기는 주기를 말하는 것은 조건이 아니다
        re.compile(r"매일(?!\s*(?:발생|나오|나온|배출|생기|생긴))|날마다"),
        EVERY_DAY,
        "수거 요일은 terms.days에 요일로 적으세요.",
    ),
    TermWord(
        "unlimited",
        re.compile(r"무제한|제한\s*없이|한도\s*없이"),
        NONE,
        "한도 없는 조건은 낼 수 없습니다. 물량은 claim으로 적으세요.",
    ),
    TermWord(
        "all_volume",
        re.compile(r"전량"),
        QUANTITY,
        "받는 물량을 claim으로 적으세요.",
    ),
)
