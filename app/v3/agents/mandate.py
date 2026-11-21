"""Decision mandate provenance. Governance references do not prove a market price."""

VERSION = "2026-11-21-explicit-evidence"
SOURCES = {
    "user-requirements": {
        "kind": "business_mandate",
        "date": "2026-11-21",
        "source": "프로젝트 담당자가 제공한 멘토링·운영 검증·근거 기반 계산 요구사항",
        "location": "docs/v3/agent-grounding-and-azure-2026-11-21.md",
    },
    "nist-ai-rmf": {
        "kind": "governance_guidance",
        "publisher": "NIST",
        "published": "2023-01",
        "title": "Artificial Intelligence Risk Management Framework (AI RMF 1.0)",
        "url": "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf",
        "location": "GOVERN 2.1, 3.2 (printed p.23); MAP 3.5 (p.27); MEASURE 2.5 (p.29)",
        "limitation": "역할·사람 감독·검증 한계 문서화 참고이며 가격 인상률의 근거나 제품 인증이 아님",
    },
    "ortools-cp-sat": {
        "kind": "algorithm_documentation",
        "publisher": "Google OR-Tools",
        "url": "https://developers.google.com/optimization/cp/cp_solver/",
        "location": "CP-SAT return values; integer constraints",
        "limitation": "입력한 목적함수·제약의 해석이며 실제 시장 최적 단가 보증이 아님",
    },
}

COMMON = (
    "사업 목적과 권한은 프로젝트 담당자의 명시 요구사항(2026-11-21)에 따른다. 과학 법칙이나 논문으로 입증된 시장 최적 전략이라고 설명하지 않는다.",
    "계산 계수는 원문 적용 조건을 확인한 도구 결과만 쓴다. 승인 기록 없는 인상률·시범기간·시세변동률을 만들거나 적용하지 않는다.",
    "NIST AI RMF의 역할 구분·사람 감독 원칙을 참고한다. 이 참고는 제품 인증이나 거래의 법적 유효성 보증이 아니다.",
)
ROLE = {
    "company": "배출기업이 제공한 조건과 비용으로, 필수 허가·품질·수거 조건을 만족하는 후보의 확인된 순손익을 비교한다. 미확인 비용은 잠정 금액으로 남기고 사람에게 확인한다.",
    "vendor": "업체가 실제로 등록한 매입 기준·품질·수거 조건·단가 한도 안에서만 답한다. 업체 원가가 없으면 이익 극대화나 시장 최적가격을 달성했다고 주장하지 않는다.",
}


def prompt_mandate(role):
    return [f"[지침 근거 버전 {VERSION}]", ROLE[role], *COMMON]
