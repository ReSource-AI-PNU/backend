"""Adapt shared streams to the synthetic economics engine without creating an API."""

from collections.abc import Mapping, Sequence
from math import fsum

from app.agents.demo_routes import (
    ROUTES,
    DemoConfig,
    Detection,
    Flow,
    evaluate_flow,
    validate_config,
)
from app.schemas import (
    FeasibilityResult,
    FieldAction,
    LadderStep,
    RouteAlternative,
    Stream,
    StreamDecision,
    StreamRoute,
)

DEMO_ASSUMPTIONS = [
    "모든 단가와 규칙은 synthetic/demo",
    "재투입은 분쇄·관리비 포함 순효과; 매출이 아닌 구매비 회피",
    "묶음 운송비와 소량 PCR 단독 매각 출장비는 kg당 순효과에 포함; 인건비만 별도 차감",
    "기타 운송비와 작업 없는 경로의 인건비는 시연상 명시적으로 0",
    "소각에는 위탁비와 별도로 폐기물처분부담금 10원/kg(산정지수 1, 중소기업 감면 미반영)을 더함",
    "회수율·신재 대체율을 추정하지 않음; 실제 적합성·수용·거래 미확정",
]


def _pending(stream: Stream, code: str, reason: str, route: StreamRoute | None) -> StreamDecision:
    return StreamDecision(
        stream_id=stream.id, status="needs_information", reason_code=code, route=route,
        reason=reason, current_per_kg=None, after_per_kg=None, monthly_now=None,
        monthly_after=None, monthly_difference=None, cash_sale_revenue=None,
        purchase_avoidance_net=None, disposal_cost=None, transport_cost=None, labor_cost=None,
        carbon_now_t=None, carbon_after_t=None, action=None, ladder=None, alternatives=None,
        ai_route=None, data_mode="synthetic_demo", assumptions=DEMO_ASSUMPTIONS.copy(),
    )


def evaluate_streams(
    streams: Sequence[Stream], config: DemoConfig,
    overrides: Mapping[str, StreamRoute] | None = None,
) -> FeasibilityResult:
    """Calculate per stream; zero or unconfirmed monthly quantity is held pending."""
    if not streams:
        raise ValueError("at least one stream is required")
    validate_config(config)
    overrides = overrides or {}
    ids = [stream.id for stream in streams]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate stream id")
    if set(overrides) - set(ids) or any(route not in ROUTES for route in overrides.values()):
        raise ValueError("invalid stream override")
    decisions = []
    for stream in streams:
        override = overrides.get(stream.id)
        if not stream.monthly_kg_confirmed:
            decisions.append(_pending(stream, "unconfirmed_quantity", "월 발생량 확인 필요", override))
            continue
        if stream.monthly_kg == 0:
            decisions.append(_pending(stream, "zero_quantity", "월 발생량이 0이어서 계산 보류", override))
            continue
        flow = Flow(
            id=stream.id, item=stream.classify.item, material=stream.classify.material,
            origin=stream.origin, monthly_kg=stream.monthly_kg,
            single_material=stream.classify.single_material,
            detections=tuple(
                Detection(d.label, d.ratio, removed=d.removed is True)
                for d in stream.classify.detections if d.kind == "contaminant"
            ),
        )
        try:
            result = evaluate_flow(flow, config, override)
        except ValueError as exc:
            decisions.append(_pending(stream, "invalid_input", str(exc), override))
            continue
        action = result.action
        decisions.append(StreamDecision(
            stream_id=result.stream_id, status=result.status, reason_code=result.reason_code,
            route=result.route, reason=result.reason, ai_route=result.ai_route,
            current_per_kg=result.current_per_kg, after_per_kg=result.after_per_kg,
            monthly_now=result.monthly_now, monthly_after=result.monthly_after,
            monthly_difference=result.monthly_difference, cash_sale_revenue=result.cash_sale_revenue,
            purchase_avoidance_net=result.purchase_avoidance_net, disposal_cost=result.disposal_cost,
            transport_cost=result.transport_cost, labor_cost=result.labor_cost,
            carbon_now_t=None, carbon_after_t=None,
            # None은 미계산(보류), 목록은 계산됨. 코어가 계산한 경우에만 채운다.
            ladder=None if result.ladder is None else [
                LadderStep(label=step.label, per_kg=step.per_kg) for step in result.ladder
            ],
            alternatives=None if result.alternatives is None else [
                RouteAlternative(route=alt.route, per_kg=alt.per_kg, note=alt.note)
                for alt in result.alternatives
            ],
            action=FieldAction(title=action.title, labor_hours=action.labor_hours,
                               labor_cost=action.labor_cost) if action is not None else None,
            data_mode="synthetic_demo", assumptions=DEMO_ASSUMPTIONS.copy(),
        ))
    pending = any(d.status == "needs_information" for d in decisions)
    conditional = any(d.status == "conditional" for d in decisions)
    total_now = None if pending else fsum(d.monthly_now for d in decisions)
    total_after = None if pending else fsum(d.monthly_after for d in decisions)
    return FeasibilityResult(
        decisions=decisions,
        status="needs_information" if pending else "conditional" if conditional else "calculated",
        total_now=total_now, total_after=total_after,
        difference=None if pending else total_after - total_now,
        carbon_now_t=None, carbon_after_t=None, reasons=[],
        note="시연용 가상 계산이며 실제 거래나 적합성을 확정하지 않습니다.",
        data_mode="synthetic_demo", assumptions=DEMO_ASSUMPTIONS.copy(),
    )
