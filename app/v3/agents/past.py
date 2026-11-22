"""회사 기록(지난 반입 검수)을 대화에 싣는 문장. 항목과 문구는 코드가 정하고, 모델이 빠뜨려도 코드가 붙인다.

같은 두 회사 사이의 지난 검수가 합의보다 낮게 나온 적이 많은(status below) 폐기물에만 쓴다(memory.agent_history).
문장에 숫자를 쓰지 않는다(날짜·등급·단가는 회사 기록 화면이 근거와 함께 보인다).
"""

from __future__ import annotations

from typing import Any

from app.v3.textutil import josa

MARK = "지난 반입 검수"
VENDOR_CONDITION = "지난 반입 검수에서 합의보다 낮게 나온 적이 여러 번 있어, 반입 검수에서 매입 기준 등급을 다시 확인할 것"


def below_ids(past: Any, stream_ids: Any = None) -> list[str]:
    """합의보다 낮게 나온 기록이 많은 폐기물 ID(들어온 순서)."""
    wanted = set(stream_ids) if stream_ids is not None else None
    out: list[str] = []
    for item in past or []:
        if not isinstance(item, dict) or item.get("status") != "below":
            continue
        sid = item.get("streamId")
        if isinstance(sid, str) and (wanted is None or sid in wanted) and sid not in out:
            out.append(sid)
    return out


def company_line(names: list[str]) -> str:
    """회사 측 첫 문의에 붙이는 한 줄."""
    return (
        f"{MARK}에서 {', '.join(names)}의 등급이나 단가가 합의보다 낮게 나온 적이 여러 번 있었습니다. "
        "이번에는 반입 검수에서 등급과 단가를 어떻게 정하는지 조건에 함께 적어 주세요."
    )


def vendor_line(name: str) -> str:
    """업체 측 조건 답 본문에 붙이는 한 줄(조건 문장과 같은 뜻)."""
    return f"{josa(name, '은는')} {VENDOR_CONDITION.replace('확인할 것', '확인하는 조건입니다.')}"


def vendor_condition(name: str) -> str:
    """terms.conditions 항목("흐름 이름: 조건")."""
    return f"{name}: {VENDOR_CONDITION}"
