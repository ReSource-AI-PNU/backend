"""흐름 출처(PIR·PCR) 추정 규칙 (설계안 v2 ①, originSource="ai")

PIR = 공정 부산물(스프루·러너, 불량품, 퍼지, 절단 스크랩), PCR = 공장이 쓰고 버린 포장재(스트레치 필름, 마대, 골판지 상자).
사진으로는 가를 수 없는 속성이라 공정명·흐름 이름·원자재 명세 텍스트와 탐지 결과(필름·골판지 비중)를 합쳐 추정하고,
결과는 항상 "AI 추정"이며 담당자 확인 질문을 함께 낸다.

한국어 키워드는 부분 일치 사고(예: "스크랩" 안의 "랩")를 막기 위해 토큰 단위로 맞춘다.
토큰은 공백·구두점·중점(·)으로 나누고, 토큰이 키워드와 같거나 키워드로 시작하되 그 뒤가 조사·접미(만, 은, 는, 이, 가, 을, 를, 류, 용)일 때만 인정한다.
"스크랩" 토큰은 "랩"으로 시작하지 않으므로 필름으로 잡히지 않는다.
"""

import re

PIR_TERMS = [
    "사출",
    "압출",
    "성형",
    "검사",
    "불량",
    "불량품",
    "스프루",
    "러너",
    "퍼지",
    "금형",
    "세척",
    "절단",
    "스크랩",
    "버",
    "게이트",
    "공정",
    "라인",
    "가공",
    "프레스",
    "트리밍",
    "컷팅",
    "리젝트",
]
PCR_TERMS = [
    "입고",
    "포장",
    "포장재",
    "포장필름",
    "필름",
    "스트레치",
    "랩",
    "마대",
    "팔레트랩",
    "골판지",
    "박스",
    "상자",
    "파렛트",
    "팔레트",
    "완충재",
    "에어캡",
    "뽁뽁이",
    "납품",
    "자재",
    "원료입고",
    "원자재",
    "구매",
]
SUFFIXES = (
    "만",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "류",
    "용",
    "에서",
    "의",
    "과",
    "와",
    "들",
    "도",
)
_SPLIT = re.compile(r"[\s·,./()\[\]{}:;!?\-_+&~]+")


def tokens(text: str) -> list[str]:
    return [t for t in _SPLIT.split(text or "") if t]


def _match(tok: str, term: str) -> bool:
    if tok == term:
        return True
    if tok.startswith(term):
        rest = tok[len(term) :]
        return rest in SUFFIXES
    return False


def hits(text: str, terms: list[str]) -> list[str]:
    out = []
    for tok in tokens(text):
        for term in terms:
            if _match(tok, term):
                out.append(term)
                break
    return out


def estimate_origin(
    name: str = "",
    process: str = "",
    material_doc_text: str = "",
    detections: list[dict] | None = None,
) -> dict:
    """returns {origin, confidence, evidence[], question?}"""
    det = detections or []
    text_pir = hits(name, PIR_TERMS) + hits(process, PIR_TERMS)
    text_pcr = hits(name, PCR_TERMS) + hits(process, PCR_TERMS)
    doc_pcr = hits(material_doc_text, ["포장", "포장재", "필름", "스트레치", "마대", "골판지"])
    # 탐지 힌트: 포장재 성격(film, paper)이 재질 면적의 과반이면 PCR 쪽으로
    pack_area = sum(
        d.get("ratio", 0)
        for d in det
        if d.get("kind") == "item" and d.get("label") in ("plastic_film", "paper")
    )
    item_area = sum(d.get("ratio", 0) for d in det if d.get("kind") == "item") or 1e-9
    pack_share = pack_area / item_area
    score = 0.0
    evidence = []
    if text_pir:
        score -= 0.35 * min(2, len(text_pir))
        evidence.append(f"공정·흐름 이름에 공정 부산물 용어: {', '.join(sorted(set(text_pir)))}")
    if text_pcr:
        score += 0.35 * min(2, len(text_pcr))
        evidence.append(f"공정·흐름 이름에 포장재 용어: {', '.join(sorted(set(text_pcr)))}")
    if doc_pcr:
        score += 0.2
        evidence.append("원자재 명세에 포장재 언급")
    if det:
        if pack_share >= 0.5:
            score += 0.25
            evidence.append(f"사진에서 필름·골판지 면적 비중 {pack_share:.0%}")
        elif pack_share <= 0.1:
            score -= 0.1
            evidence.append("사진에 포장재 성격 물체가 거의 없음")
    origin = "PCR" if score > 0 else "PIR"
    confidence = round(min(0.95, 0.5 + abs(score) / 2), 2)
    out = {"origin": origin, "originSource": "ai", "confidence": confidence, "evidence": evidence}
    # 출처는 AI 추정이므로 확신이 높아도 담당자 확인 질문을 항상 붙인다 (묶음 매각은 PCR 이 전제)
    out["question"] = {
        "step": "classify",
        "kind": "choice",
        "options": ["공정에서 나온 부산물 (PIR)", "입고 자재의 포장재 (PCR)"],
        "text": "이 흐름은 공정에서 나왔나요, 입고된 자재의 포장재인가요?",
        "why": "재생원료 사용 의무(국내 페트 고시, EU PPWR)는 PCR만 인정하므로 출처에 따라 갈 곳과 단가가 달라집니다.",
        "suggested": "입고 자재의 포장재 (PCR)"
        if origin == "PCR"
        else "공정에서 나온 부산물 (PIR)",
    }
    return out


if __name__ == "__main__":
    for n, p in [
        ("스프루·러너", "사출 1·2라인"),
        ("금속 스크랩", "프레스 라인"),
        ("원자재 포장 필름", "원료 입고"),
        ("불량 성형품", "검사 공정"),
        ("퍼지·색 혼합", "색 교체·금형 세척"),
        ("랩", ""),
    ]:
        print(n, "|", p, "->", estimate_origin(n, p))
