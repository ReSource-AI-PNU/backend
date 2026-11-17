"""Platform integration; demo adapters are explicit, operational results are never fabricated."""

from app.core.config import settings
from app.platform.adapters import AdapterError, stage
from app.schemas import (
    CostCarbonResult,
    DocumentDraft,
    FeasibilityResult,
    ProposalResult,
    Question,
    RegulationResult,
    Stream,
)

RESULTS = {
    "feasibility": FeasibilityResult,
    "regulation": RegulationResult,
    "cost-carbon": CostCarbonResult,
    "proposal": ProposalResult,
}


def validated_questions(step, session, raw):
    if not isinstance(raw, list):
        raise AdapterError("단계 질문은 배열이어야 합니다")
    questions = [Question.model_validate(q) for q in raw]
    ids = {s.id for s in session.streams}
    if len({q.id for q in questions}) != len(questions) or any(
        q.step != step or (q.stream_id and q.stream_id not in ids) for q in questions
    ):
        raise AdapterError("질문의 ID·흐름·단계가 일치하지 않습니다")
    old = {q.id: q for q in session.questions}
    for q in questions:
        prior = old.get(q.id)
        q.answer = (
            prior.answer if prior and prior.text == q.text and prior.options == q.options else None
        )
    return questions


def validate_result(step, session, result):
    ids = {s.id for s in session.streams}
    if step == "feasibility":
        refs = [d.stream_id for d in result.decisions]
        if len(refs) != len(ids) or set(refs) != ids:
            raise AdapterError("판정 결과의 흐름이 누락·중복되었거나 다릅니다")
    if step == "regulation" and (
        not result.items or any(i.stream_id and i.stream_id not in ids for i in result.items)
    ):
        raise AdapterError("규정 결과의 검토 항목·흐름을 확인하세요")
    if step == "cost-carbon":
        if not result.routes or len({r.id for r in result.routes}) != len(result.routes):
            raise AdapterError("시나리오가 비었거나 중복되었습니다")
        for r in result.routes:
            refs = [line.stream_id for line in r.breakdown]
            if len(refs) != len(ids) or set(refs) != ids:
                raise AdapterError("시나리오에 모든 흐름이 한 번씩 필요합니다")
    if step == "proposal":
        routes = {r.id for r in session.cost_carbon.routes}
        if result.recommended not in routes or not set(result.alternatives) <= routes:
            raise AdapterError("이행안이 검토하지 않은 시나리오를 참조합니다")
        if len({d.id for d in result.documents}) != len(result.documents) or any(
            d.stream_id and d.stream_id not in ids for d in result.documents
        ):
            raise AdapterError("문서의 ID·흐름을 확인하세요")
    return result


def handler(step, state, demo=False):
    def run(session):
        state.session = session
        if settings.platform_stage_endpoints.get(step):
            response = stage(step, state)
            questions = validated_questions(step, session, response.get("questions", []))
            result = validate_result(
                step, session, RESULTS[step].model_validate(response["result"])
            )
            state.session.questions = [
                q for q in state.session.questions if q.step != step
            ] + questions
            return result
        if not demo:
            raise AdapterError(f"{step} 담당 서비스 주소 설정이 필요합니다")
        if step == "feasibility":
            from app.agents.economics_adapter import evaluate_streams
            from app.demo.synthetic import CONFIG

            return evaluate_streams(session.streams, CONFIG)
        if step == "regulation":
            return RegulationResult(
                items=[
                    {
                        "id": f"demo:{s.id}",
                        "streamId": s.id,
                        "label": "가상 규정 어댑터",
                        "status": "ok",
                        "result": "통합 동작 검사용 결과",
                        "citation": "법령 근거 아님 · 가상 응답",
                        "excerpt": "실제 적법성 판정에 사용할 수 없음",
                    }
                    for s in session.streams
                ],
                conclusion={"tone": "warn", "text": "가상 규정 응답 · 실법령 미검증"},
            )
        if step == "cost-carbon":
            # Use existing B economic result; do not invent carbon factors.
            decisions = session.feasibility.decisions
            return CostCarbonResult(
                baseline="신재 대비",
                routes=[
                    {
                        "id": "A",
                        "name": "가상 경로 비교",
                        "description": "B 트랙 가상 경제 결과 · 탄소 자료 없음",
                        "monthlyKrw": session.feasibility.total_after,
                        "carbonT": None,
                        "kauKrw": None,
                        "condition": "탄소 데이터 연결 필요",
                        "recommended": True,
                        "breakdown": [
                            {
                                "streamId": d.stream_id,
                                "route": d.route,
                                "monthlyKrw": d.monthly_after,
                                "carbonT": None,
                            }
                            for d in decisions
                            if d.route
                        ],
                    }
                ],
                assumptions=[
                    {"label": "가상 통합 검사", "value": "금액은 B의 시연 입력, 탄소는 계산 미연결"}
                ],
            )
        docs = [
            DocumentDraft(
                id=f"demo-{kind}-{i}",
                kind=kind,
                title=title,
                to="담당자 검토함",
                fields=[],
                body="가상 통합 검사 문서입니다. 실제 제출·발송할 수 없습니다.",
                external=False,
            )
            for i, (kind, title) in enumerate(
                [
                    ("plan", "이행 계획"),
                    ("grant", "지원 신청"),
                    ("pickup", "순회 요청"),
                    ("quote", "매각 견적"),
                    ("quote", "열분해 위탁"),
                    ("joint", "공동처리 검토"),
                ]
            )
        ]
        return ProposalResult(
            recommended="A", alternatives=[], summary="가상 이행안 · 외부 발송 없음", documents=docs
        )

    return run


def classify(state):
    response = stage("classify", state)
    if not isinstance(response.get("streams"), list):
        raise AdapterError("식별 결과의 흐름은 배열이어야 합니다")
    streams = [Stream.model_validate(s) for s in response["streams"]]
    if len(streams) != len(state.session.streams) or {s.id for s in streams} != {
        s.id for s in state.session.streams
    }:
        raise AdapterError("식별 서비스가 반환한 흐름 ID가 다릅니다")
    questions = validated_questions("classify", state.session, response.get("questions", []))
    old = {s.id: s for s in state.session.streams}
    for s in streams:
        prior = old[s.id]
        s.monthly_kg, s.monthly_kg_confirmed = prior.monthly_kg, prior.monthly_kg_confirmed
        if prior.origin_source == "human":
            s.origin, s.origin_source = prior.origin, prior.origin_source
    state.session.streams = streams
    state.session.questions = [q for q in state.session.questions if q.id.startswith("volume:")] + [
        q for q in questions if not q.id.startswith("volume:")
    ]
