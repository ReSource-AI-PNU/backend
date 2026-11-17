"""공개 신뢰 페이지(latest.json)에 싣는 쉬운 말 이름과 풀이(평가 설계 17장, DESIGN.md 제품 문구 원칙).

평가 리포트(.json, .md)는 개발자 문서라 지표 이름이 기술 용어다(pass^1, 표적 ASR, 카나리아·탐침). 신뢰
페이지는 일반 사용자가 보므로 이름(label)과 풀이(explain)를 쉬운 말로 싣고, 기술 이름은 technicalLabel,
기계 ID는 key와 층 코드(L4 등)에 따로 둔다. 표에 없는 지표는 기술 이름에서 개발자 말을 걷어 낸다.
"""

from __future__ import annotations

import re
from typing import Any

# 층 코드 → (쉬운 이름, 풀이)
LAYERS: dict[str, tuple[str, str]] = {
    "L1": ("사진 인식", "현장 사진에서 폐기물의 재질과 이물을 알아보는 정도를 봅니다."),
    "L2": ("근거 찾기", "질문에 맞는 법 조항과 근거 문서를 찾아 위쪽에 놓는지 봅니다."),
    "L3": (
        "근거 있는 글쓰기",
        "AI가 쓴 글의 숫자와 조항이 근거와 맞는지 보내기 전에 검사합니다. 통과하지 못한 글은 나가지 않습니다.",
    ),
    "L4": (
        "찾기를 끝까지 해내기",
        "가상 시나리오마다 찾기를 끝까지 돌려, 끝난 모습(합의·거절·담당자 인계)이 기대와 같은지 봅니다.",
    ),
    "L5": (
        "조건 맞추기",
        "업체 측 AI가 업체가 정한 범위를 지키는지, 합의할 수 있는 대화는 합의하는지 봅니다.",
    ),
    "L6c": ("다른 회사 정보 지키기", "한 회사의 정보가 다른 회사의 화면이나 대화로 새는지 봅니다."),
    "L6i": (
        "숨긴 지시와 개인정보 막기",
        "문서에 숨긴 지시가 AI를 움직이는지, 개인정보가 다른 회사에 보이는지 봅니다.",
    ),
    "L7": ("사람이 읽고 매긴 평가", "사람이 AI의 글을 읽고 정해 둔 기준으로 점수를 매깁니다."),
}

_RATE = "0에서 1 사이 비율입니다."
# 지표 키 → (쉬운 이름, 풀이). 같은 키가 층마다 뜻이 다르면 (층 코드, 키)로 둔다
METRICS: dict[Any, tuple[str, str]] = {
    "ndcg@10": ("맞는 조항을 위쪽에 찾아 놓는 정도", "1이면 맞는 조항이 모두 맨 위에 있습니다."),
    "recall@5": ("찾아야 할 조항이 상위 5개 안에 든 비율", _RATE),
    "recall@10": ("찾아야 할 조항이 상위 10개 안에 든 비율", _RATE),
    "mrr": ("첫 번째로 맞는 조항이 얼마나 위에 있는지", "1이면 첫 결과가 맞는 조항입니다."),
    "no_answer_accuracy": ("답이 없는 질문에 없다고 답한 비율", _RATE),
    "answer_rate": ("답이 있는 질문에 답을 내놓은 비율", _RATE),
    "firstTry": ("첫 초안이 근거 확인을 통과한 비율", _RATE),
    "afterRewrite": ("고쳐 쓴 뒤까지 근거 확인을 통과한 비율", _RATE),
    "handoff": ("끝내 통과하지 못해 담당자에게 넘긴 비율", _RATE),
    "numberHallucination": (
        "초안에 근거 없는 숫자가 들어간 비율",
        "초안은 상대에게 나가기 전에 걸러집니다.",
    ),
    "evidenceHallucination": (
        "초안에 없는 근거를 댄 비율",
        "초안은 상대에게 나가기 전에 걸러집니다.",
    ),
    "quoteMismatch": ("조항을 원문과 다르게 인용한 비율", _RATE),
    "recheckRejected": ("받은 글을 다시 검사해 돌려보낸 비율", _RATE),
    "outgoingHallucination": ("근거 없는 숫자가 상대에게 나간 횟수", "0이어야 합니다."),
    "pass^1": ("찾기를 끝까지 맞게 마친 비율", _RATE),
    "pass^2": ("같은 시나리오를 두 번 돌려 두 번 모두 맞게 마친 비율", _RATE),
    "pass^3": ("같은 시나리오를 세 번 돌려 세 번 모두 맞게 마친 비율", _RATE),
    "pass^4": ("같은 시나리오를 네 번 돌려 네 번 모두 맞게 마친 비율", _RATE),
    "verdictMatch": (
        "끝난 모습이 기대와 같은 비율",
        "합의, 거절, 담당자 인계 가운데 무엇으로 끝났는지 봅니다.",
    ),
    "violations": ("하면 안 되는 일을 한 횟수", "0이어야 합니다."),
    "requiredRecall": ("꼭 물어야 할 것을 물은 비율", _RATE),
    "unneededQuestions": ("묻지 않아도 될 것을 물은 비율", _RATE),
    "latencyP50": ("찾기 한 번에 걸린 시간(가운데 값)", "초 단위입니다."),
    "latencyP95": ("찾기 한 번에 걸린 시간(느린 쪽)", "느린 쪽 5%의 경계입니다. 초 단위입니다."),
    "turnsPerThread": ("대화 하나에서 주고받은 횟수(평균)", ""),
    "questionBatches": ("찾기 한 번에 담당자에게 물은 묶음 수(평균)", ""),
    ("L4", "handoffs"): ("찾기 한 번에 담당자에게 넘긴 대화 수(평균)", ""),
    "tokens": ("AI가 읽고 쓴 글의 양", ""),
    "cost": ("비용(원)", ""),
    "variantConsistency": ("말을 바꿔 물어도 같은 결과를 낸 비율", _RATE),
    "agreement": ("합의할 수 있는 대화를 합의한 비율", _RATE),
    "wrongAgreements": ("합의하면 안 되는 대화를 합의한 횟수", "0이어야 합니다."),
    "normalizedRegret": ("가장 좋은 조건과의 차이", "0이 가장 좋습니다."),
    "zeroRegret": ("가장 좋은 조건으로 합의한 비율", _RATE),
    "guardrailCompliance": ("업체가 정한 범위를 지킨 비율", _RATE),
    "turnsToAgreement": ("합의까지 주고받은 횟수(평균)", ""),
    ("L5", "handoffs"): ("사람이 이어받은 대화 수(시나리오당 평균)", ""),
    "tacticSensitivity": ("상대의 말투에 따라 결과가 흔들린 정도", "0이 가장 좋습니다."),
    "pareto": ("양쪽 모두에게 더 나은 조건이 남지 않은 합의의 비율", _RATE),
    "regretWithTransport": (
        "운반비까지 넣었을 때 가장 좋은 조건과의 차이(참고)",
        "0이 가장 좋습니다.",
    ),
    "exposures": ("다른 회사에 보인 정보 건수", "0이어야 합니다."),
    "probeClean": ("정보를 빼내려는 시도에도 새지 않은 비율", _RATE),
    "baseExposures": ("평소 쓰는 중에 다른 회사에 보인 정보 건수", "0이어야 합니다."),
    "probeAdded": ("빼내려는 시도로 더 샌 정보 건수", "0이어야 합니다."),
    "viewExposures": ("화면·대화·문서로 다른 회사에 보인 정보 건수", "0이어야 합니다."),
    "probeErrors": ("시험 실행 오류 건수", ""),
    "benign_utility": ("숨긴 지시가 없을 때 찾기를 맞게 마친 비율", _RATE),
    "utility_under_attack": ("숨긴 지시가 있어도 찾기를 맞게 마친 비율", _RATE),
    "safe_handoff": ("숨긴 지시를 보고 담당자에게 안전하게 넘긴 비율", _RATE),
    "asr_code_gated": ("숨긴 공격 문구가 목표에 닿은 비율", "0이어야 합니다."),
    "asr_G1": ("숨긴 지시로 업체 범위 밖 단가를 내게 한 비율", "0이어야 합니다."),
    "asr_G2": ("숨긴 지시로 승인 없이 확정하게 한 비율", "0이어야 합니다."),
    "asr_G3": ("숨긴 지시로 허가 없는 업체와 대화·배정하게 한 비율", "0이어야 합니다."),
    "asr_G4": ("숨긴 지시로 다른 회사 정보를 빼낸 비율", "0이어야 합니다."),
    "asr_G5": ("숨긴 지시로 근거 없는 숫자·조항을 넣게 한 비율", "0이어야 합니다."),
    "asr_G6": ("숨긴 지시 문장·링크가 상대에게 간 글에 옮겨진 비율", "0이어야 합니다."),
    "utility_plus_handoff": ("숨긴 지시가 있어도 맞게 마치거나 안전하게 넘긴 비율", _RATE),
    "relay_rate": ("공격 문구가 상대에게 그대로 전달된 비율(참고)", _RATE),
    "twin_goal_state": ("공격 없이도 같은 결과에 닿은 시나리오 수(기준선)", ""),
    "shields_detect": ("입력 공격 탐지기가 문서 공격을 찾은 비율", _RATE),
    "shields_false_positive": ("입력 공격 탐지기가 정상 문서를 공격으로 본 비율", _RATE),
    "asr_documents_shields_on": ("탐지기를 켰을 때 문서 공격이 목표에 닿은 비율", _RATE),
    "asr_documents_shields_off": ("탐지기를 껐을 때 문서 공격이 목표에 닿은 비율", _RATE),
    "pii_other_orgs": ("다른 회사에 보인 개인정보 건수", "0이어야 합니다."),
    "pii_model_input_documents": ("문서에서 AI 입력으로 들어간 개인정보 건수", "0이어야 합니다."),
    "pii_model_input_names": ("이름 칸에서 AI 입력으로 들어간 개인정보 건수(참고)", ""),
    "pii_events": ("실행 기록에 남은 개인정보 건수(참고)", ""),
    "pii_control_false_hits": ("개인정보를 넣지 않은 시험에서 잘못 잡은 건수", ""),
}

# 공개 이름에 쓰지 않는 말(기술 용어). 표에 없는 지표 이름은 이 말을 걷어 내거나 바꾼다
JARGON = re.compile(
    r"ASR|카나리아|탐침|spotlighting|Shields|Prompt|Azure|AI Search|BM25|RRF|Cohen|κ|"
    r"\bL[1-7][a-z]?\b|pass\^|nDCG|MRR|Recall|handoff|규칙 정책"
)
_DEV = re.compile(
    r"[A-Z][A-Z0-9]*_[A-Z0-9_]+|\.py\b|\.json\b|/|backend|resource-ai|설정 없음|환경 변수"
)
_REPLACE = (
    ("Azure AI Search", "검색 서비스"),
    ("Prompt Shields", "입력 공격 탐지기"),
    ("규칙 정책", "모델을 연결하지 않은 기본 규칙"),
    ("handoff", "담당자 인계"),
)
# 재지 못한 것(notMeasured)의 기술 문장 → 쉬운 말. 앞에서부터 처음 맞는 것을 쓴다
_NOT_MEASURED = (
    (re.compile(r"^S\d "), "다른 검색 방식과의 비교: 이번에는 재지 않았습니다."),
    (re.compile(r"Prompt Shields 켬"), "입력 공격 탐지기를 켠 조건: 이번에는 재지 않았습니다."),
    (
        re.compile(r"spotlighting"),
        "문서 글을 AI 입력과 구분해 넣는 조건: 문서 글이 AI 입력에 들어가지 않아 해당하지 않습니다.",
    ),
    (re.compile(r"공장 현장 사진"), "공장 현장 사진과 오염 비율 정답이 아직 없어 재지 못했습니다."),
    (
        re.compile(r"^의미 지표"),
        "글의 뜻이 근거와 맞는지: 판정 기준과 사람이 매긴 정답이 아직 없습니다.",
    ),
    (
        re.compile(r"^루브릭"),
        "사람이 읽고 매기는 평가: 채점 기준과 사람이 매긴 정답이 아직 없습니다.",
    ),
    (
        re.compile(r"실제 모델의 주입 저항"),
        "실제 AI 모델이 숨긴 지시를 견디는지: 이번 실행은 모델을 연결하지 않았습니다.",
    ),
    (
        re.compile(r"^인용 문장 적합률"),
        "찾은 문장이 질문에 맞는지: 질문별 정답 표시가 없어 재지 못했습니다.",
    ),
    (re.compile(r"Cohen"), "정답 표시를 두 사람이 따로 매겨 맞춰 보는 일: 아직 하지 않았습니다."),
    (re.compile(r"예산"), "호출 예산을 넘어 재지 못했습니다."),
)


def layer_text(code: str, title: str) -> tuple[str, str]:
    """(쉬운 이름, 풀이). 표에 없는 층이면 기술 이름을 걷어 낸 것."""
    found = LAYERS.get(code) or LAYERS.get(code[:2])
    return found if found else (plain(title) or code, "")


def metric_text(code: str, key: str, label: str) -> tuple[str, str]:
    """(쉬운 이름, 풀이). 표에 없는 지표면 기술 이름을 걷어 낸 것."""
    found = METRICS.get((code, key)) or METRICS.get(key)
    return found if found else (plain(label) or "기타 지표", "")


def plain(text: str) -> str | None:
    """기술 이름에서 개발자 말을 바꾸거나 걷어 낸다. 남는 것이 없으면 None."""
    if _DEV.search(text):
        return None
    for old, new in _REPLACE:
        text = text.replace(old, new)
    text = re.sub(r"\s*\([^)]*(?:ASR|카나리아|탐침|G\d|Shields|spotlighting)[^)]*\)", "", text)
    text = re.sub(r"\(평가 설계 \d+장\)|\(\d+장\)", "", text).strip()
    return None if not text or JARGON.search(text) else text


def not_measured_text(items: list[str]) -> list[str]:
    """재지 못한 것의 쉬운 말 목록(겹치는 것은 한 번). 바꿀 수 없는 문장은 뺀다."""
    out: list[str] = []
    for item in items:
        text = next(
            (plain_text for pattern, plain_text in _NOT_MEASURED if pattern.search(item)), None
        )
        text = text or plain(item)
        if text and text not in out:
            out.append(text)
    return out
