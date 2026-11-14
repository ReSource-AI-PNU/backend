"""Synthetic route and economic-effect calculations; no real-world determination."""

from dataclasses import dataclass
from math import fsum, isfinite
from typing import Literal

Route = Literal["reuse", "sell", "bundle", "pyrolysis", "incinerate"]
ROUTES = ("reuse", "sell", "bundle", "pyrolysis", "incinerate")


@dataclass(frozen=True)
class Detection:
    label: str
    ratio: float | None
    removed: bool = False


@dataclass(frozen=True)
class Flow:
    id: str
    item: str | None
    material: str | None
    origin: str | None
    monthly_kg: float | None
    single_material: bool | None
    detections: tuple[Detection, ...] | None


@dataclass(frozen=True)
class DemoConfig:
    supported_routes: dict[tuple[str, str], frozenset[Route]]
    minimum_kg: dict[str, float]
    current_per_kg: dict[str, float]
    sale_per_kg: dict[str, float]
    reuse_per_kg: float
    pyrolysis_per_kg: float
    bundle_transport_per_kg: float
    solo_sale_penalty_per_kg: float
    labor_per_hour: float
    reuse_labor_hours: float
    sell_labor_hours: float
    bundle_labor_hours: float
    reuse_max_contamination: float
    sell_max_contamination: float
    mix_threshold: float


@dataclass(frozen=True)
class FieldAction:
    title: str
    labor_hours: float
    labor_cost: float


@dataclass(frozen=True)
class LadderStep:
    label: str
    per_kg: float


@dataclass(frozen=True)
class Alternative:
    route: Route
    per_kg: float
    note: str


@dataclass(frozen=True)
class FlowResult:
    stream_id: str
    status: str
    route: Route | None = None
    ai_route: Route | None = None
    reason: str = ""
    current_per_kg: float | None = None
    after_per_kg: float | None = None
    monthly_now: float | None = None
    monthly_after: float | None = None
    monthly_difference: float | None = None
    cash_sale_revenue: float | None = None
    purchase_avoidance_net: float | None = None
    disposal_cost: float | None = None
    transport_cost: float | None = None
    labor_cost: float | None = None
    action: FieldAction | None = None
    bundle_status: str | None = None
    assumptions: tuple[str, ...] = ()
    reason_code: str | None = None
    ladder: tuple[LadderStep, ...] | None = None
    alternatives: tuple[Alternative, ...] | None = None


@dataclass(frozen=True)
class FactoryResult:
    decisions: tuple[FlowResult, ...]
    total_now: float | None
    total_after: float | None
    difference: float | None
    status: str


def _number(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite number")  # noqa: TRY004
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"invalid {name}")
    return value


def _config_value(mapping: dict[str, float], key: str, name: str) -> float | None:
    if key not in mapping:
        return None
    return _number(mapping[key], name)


def validate_config(config: DemoConfig) -> None:
    for name in ("reuse_per_kg", "bundle_transport_per_kg", "solo_sale_penalty_per_kg",
                 "labor_per_hour", "reuse_labor_hours", "sell_labor_hours", "bundle_labor_hours"):
        _number(getattr(config, name), name, minimum=0)
    if _number(config.pyrolysis_per_kg, "pyrolysis_per_kg") > 0:
        raise ValueError("pyrolysis_per_kg must be nonpositive")
    for name in ("reuse_max_contamination", "sell_max_contamination", "mix_threshold"):
        if _number(getattr(config, name), name, minimum=0) > 1:
            raise ValueError(f"{name} must be at most 1")
    if config.reuse_max_contamination > config.sell_max_contamination:
        raise ValueError("reuse threshold exceeds sell threshold")
    for mapping, sign in ((config.minimum_kg, 1), (config.sale_per_kg, 1),
                          (config.current_per_kg, -1)):
        for key, value in mapping.items():
            if _number(value, key) * sign < 0:
                raise ValueError(f"invalid configured value: {key}")


def _is_film(material: str) -> bool:
    return material.strip().upper() in {"LDPE", "LLDPE"}


def _small_pcr(flow: Flow, config: DemoConfig) -> bool:
    """PCR flow under the item's minimum pickup quantity, too small to sell alone."""
    minimum = _config_value(config.minimum_kg, flow.item, "minimum_kg")
    return flow.origin == "PCR" and minimum is not None and flow.monthly_kg < minimum


def _is_plastic(flow: Flow) -> bool:
    """Plastic flows: waste plastic, or packaging whose resin is LDPE·LLDPE film."""
    return flow.item == "폐플라스틱" or (flow.item == "포장재" and _is_film(flow.material))


def _ladder(route: Route, current: float, sale: float | None, has_removal: bool, solo: bool,
            config: DemoConfig) -> tuple[LadderStep, ...]:
    """Per-kg steps from the current route up to the chosen one (frontend mock ladderFor).

    Unlike the mock, the "partial removal" step appears only when there is something to remove,
    and a small PCR flow sold alone ends at the sale price minus the solo trip penalty.
    """
    steps = [LadderStep("현재(혼합 소각)", current)]
    if route == "reuse":
        if sale is not None:
            steps.append(LadderStep("분리해 매각", sale))
        steps.append(LadderStep("분쇄 재투입", config.reuse_per_kg))
    elif route == "sell" and sale is not None and solo:
        steps.append(LadderStep("분리해 단독 매각", sale - config.solo_sale_penalty_per_kg))
    elif route == "sell" and sale is not None and has_removal:
        steps += [LadderStep("이물 일부 제거", sale / 2), LadderStep("이물 제거 후 매각", sale)]
    elif route == "sell" and sale is not None:
        steps.append(LadderStep("분리해 매각", sale))
    elif route == "bundle" and sale is not None:
        steps += [LadderStep("분리해 단독 매각", sale - config.solo_sale_penalty_per_kg),
                  LadderStep("단지 묶음 매각", sale - config.bundle_transport_per_kg)]
    elif route == "pyrolysis":
        steps.append(LadderStep("열분해 위탁", config.pyrolysis_per_kg))
    return tuple(steps)


def _alternatives(route: Route, flow: Flow, current: float, sale: float | None, mixed: bool,
                  config: DemoConfig) -> tuple[Alternative, ...]:
    """Routes not chosen, with a nominal per-kg value and why (frontend mock decide).

    The per-kg value is what the route would be worth if it applied; the note says why it
    was not chosen. It is not a claim that the route is available for this flow.
    """
    small_pcr = _small_pcr(flow, config)
    out = []
    for other in ROUTES:
        if other == route:
            continue
        if other == "reuse":
            if flow.item != "폐플라스틱" or flow.origin == "PCR":
                continue
            per_kg, note = config.reuse_per_kg, "같은 공정에 다시 넣으려면 단일 재질·무오염이어야 합니다."
        elif other == "sell":
            if sale is None:
                continue
            per_kg = sale - (config.solo_sale_penalty_per_kg if small_pcr else 0)
            note = ("재질이 섞여 매입처가 받지 않습니다." if mixed
                    else "혼자 팔면 출장비 때문에 단가가 깎입니다." if small_pcr
                    else "이물을 떼야 매입 기준을 맞춥니다.")
        elif other == "bundle":
            if flow.origin != "PCR" or sale is None:
                continue
            per_kg, note = sale - config.bundle_transport_per_kg, "같은 단지 기업과 순회 수거로 묶습니다."
        elif other == "pyrolysis":
            # 열분해는 플라스틱 흐름의 경로다. 금속·목재 등에는 대안으로 내지 않는다.
            if not _is_plastic(flow):
                continue
            per_kg, note = config.pyrolysis_per_kg, "재질이 섞인 흐름에 맞는 경로입니다."
        else:
            per_kg, note = current, "지금처럼 혼합 폐기물로 위탁합니다."
        out.append(Alternative(other, per_kg, note))
    return tuple(out)


def evaluate_flow(flow: Flow, config: DemoConfig, override: Route | None = None) -> FlowResult:
    """Evaluate one demo flow. Missing information returns needs_information."""
    validate_config(config)
    if not isinstance(flow.id, str) or not flow.id.strip():
        raise ValueError("flow id is required")
    if override is not None and override not in ROUTES:
        raise ValueError("invalid route override")
    if flow.monthly_kg is not None and _number(flow.monthly_kg, "monthly_kg") <= 0:
        raise ValueError("monthly_kg must be greater than zero")
    if flow.origin is not None and flow.origin not in ("PIR", "PCR"):
        raise ValueError("invalid origin")
    if flow.single_material is not None and not isinstance(flow.single_material, bool):
        raise ValueError("single_material must be boolean")
    if flow.detections is not None:
        for detection in flow.detections:
            if not isinstance(detection.label, str) or not detection.label.strip():
                raise ValueError("detection label is required")
            if not isinstance(detection.removed, bool):
                raise ValueError("removed must be boolean")  # noqa: TRY004
            if detection.ratio is not None and _number(detection.ratio, "ratio", minimum=0) > 1:
                raise ValueError("ratio must be at most 1")
    if not flow.item or not flow.material or flow.origin is None or flow.monthly_kg is None:
        return FlowResult(flow.id, "needs_information", reason="품목, 재질, 출처와 월 발생량 확인 필요", reason_code="missing_information")
    if not isinstance(flow.item, str) or not isinstance(flow.material, str):
        raise ValueError("item and material must be strings")  # noqa: TRY004
    if flow.single_material is None or flow.detections is None:
        return FlowResult(flow.id, "needs_information", reason="단일 재질과 이물 정보 확인 필요", reason_code="missing_information")
    active = [d for d in flow.detections if not d.removed]
    if any(d.ratio is None for d in active):
        return FlowResult(flow.id, "needs_information", reason="이물 비율 확인 필요", reason_code="missing_information")
    current = _config_value(config.current_per_kg, flow.item, "current_per_kg")
    if current is None:
        return FlowResult(flow.id, "needs_information", reason="시연 현재 처리 단가 확인 필요", reason_code="missing_information")
    contamination = fsum(d.ratio for d in active if d.ratio is not None)
    if contamination > 1:
        raise ValueError("sum of contaminant ratios exceeds 1")
    mix_detections = [d for d in flow.detections if "혼입" in d.label or "혼합" in d.label]
    mixed = any(not d.removed and d.ratio > config.mix_threshold for d in mix_detections) or (
        not flow.single_material and not mix_detections
    )
    if flow.item == "금속 스크랩":
        ai, reason = "sell", "시연 규칙: 금속 스크랩 매각"
    elif mixed:
        ai, reason = "pyrolysis", "시연 규칙: 혼합 재질 열분해"
    elif flow.origin == "PCR":
        # #10 specifies film resins by whole name. Other packaging has no supplied film rule.
        if flow.item == "포장재" and not _is_film(flow.material):
            return FlowResult(flow.id, "needs_information", reason="LDPE·LLDPE 외 포장재의 시연 규칙 확인 필요", reason_code="unsupported_combination")
        minimum = _config_value(config.minimum_kg, flow.item, "minimum_kg")
        if minimum is None:
            return FlowResult(flow.id, "needs_information", reason="시연 최소 물량 확인 필요", reason_code="missing_information")
        ai = "bundle" if flow.monthly_kg < minimum else "sell"
        reason = "시연 규칙: PCR 최소 물량 " + ("미만 묶음 후보" if ai == "bundle" else "이상 매각")
    elif flow.item == "폐플라스틱" and contamination < config.reuse_max_contamination:
        ai, reason = "reuse", "시연 규칙: 이물이 적은 단일 PIR 플라스틱 재투입"
    elif contamination < config.sell_max_contamination:
        ai, reason = "sell", "시연 규칙: 이물 기준 미만 매각"
    else:
        ai, reason = "incinerate", "시연 규칙: 이물 기준 이상 소각"
    route = override or ai
    if route != ai:
        reason = f"사용자 경로 변경: {route}; 원래 시연 경로: {ai}"

    supported = config.supported_routes.get((flow.item, flow.material.strip().upper()), frozenset())
    if route not in supported:
        return FlowResult(
            flow.id, "needs_information", route=route, ai_route=ai if route != ai else None,
            reason="해당 품목·재질·경로 조합의 시연 계산값이 없음 (실제 적합성 판단 아님)",
            reason_code="unsupported_combination",
        )

    sale = _config_value(config.sale_per_kg, flow.item, "sale_per_kg") if route in ("sell", "bundle") else None
    if route in ("sell", "bundle") and sale is None:
        return FlowResult(flow.id, "needs_information", route=route, ai_route=ai if route != ai else None,
                          reason="시연 매각 단가 확인 필요", reason_code="missing_information")
    # Zero action/transport is the explicit frontend demo assumption for other routes.
    removable = [d.label for d in active if "혼입" not in d.label and "혼합" not in d.label]
    if route == "reuse":
        title, hours = "색별로 분리해 분쇄기에 넣기", config.reuse_labor_hours
    elif route == "bundle":
        title, hours = "흐름을 마대에 따로 모으기", config.bundle_labor_hours
    elif route == "sell" and removable:
        title, hours = f"{'·'.join(removable)} 제거 후 따로 모으기", config.sell_labor_hours
    else:
        title, hours = None, 0
    labor = hours * config.labor_per_hour
    action = FieldAction(title, hours, labor) if title is not None else None
    # 최소 물량 미만 PCR 흐름을 혼자 팔면 출장비가 붙는다. 대안 표시(_alternatives)와 같은 규칙이다.
    solo = route == "sell" and _small_pcr(flow, config)
    solo_penalty = config.solo_sale_penalty_per_kg if solo else 0
    if route == "bundle":
        transport = flow.monthly_kg * config.bundle_transport_per_kg
    else:
        transport = flow.monthly_kg * solo_penalty
    after = {"reuse": config.reuse_per_kg, "pyrolysis": config.pyrolysis_per_kg,
             "incinerate": current, "sell": sale - solo_penalty if sale is not None else None,
             "bundle": sale - config.bundle_transport_per_kg if sale is not None else None}[route]
    quantity = flow.monthly_kg
    now = current * quantity
    monthly_after = after * quantity - labor
    cash = sale * quantity if route in ("sell", "bundle") else 0
    avoidance = config.reuse_per_kg * quantity if route == "reuse" else 0
    disposal = -after * quantity if route in ("pyrolysis", "incinerate") else 0
    # The chosen route's own sale price was checked above; other routes may lack one.
    item_sale = _config_value(config.sale_per_kg, flow.item, "sale_per_kg")
    ladder = _ladder(route, current, item_sale, bool(removable), solo, config)
    alternatives = _alternatives(route, flow, current, item_sale, mixed, config)
    if not all(isfinite(x) for x in (
        now, monthly_after, monthly_after - now, cash, avoidance, disposal, transport, labor,
        *(step.per_kg for step in ladder), *(alt.per_kg for alt in alternatives),
    )):
        raise ValueError("calculation exceeds finite floating-point range")
    return FlowResult(
        flow.id, "conditional" if route == "bundle" else "calculated", route,
        ai if route != ai else None, reason, current, after, now, monthly_after,
        monthly_after - now, cash, avoidance, disposal, transport, labor,
        action, "conditional" if route == "bundle" else None,
        ("모든 단가와 규칙은 synthetic/demo", "재투입은 분쇄·관리비 포함 순효과; 매출이 아닌 구매비 회피",
         "묶음 운송비와 소량 PCR 단독 매각 출장비는 kg당 순효과에 포함; 인건비만 별도 차감",
         "기타 운송비와 작업 없는 경로의 인건비는 시연상 명시적으로 0",
         "회수율·신재 대체율을 추정하지 않음; 실제 적합성·수용·거래 미확정"),
        ladder=ladder, alternatives=alternatives,
    )


def evaluate_factory(flows: tuple[Flow, ...] | list[Flow], config: DemoConfig,
                     overrides: dict[str, Route] | None = None) -> FactoryResult:
    if not flows:
        raise ValueError("at least one flow is required")
    ids = [flow.id for flow in flows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate flow id")
    overrides = overrides or {}
    if set(overrides) - set(ids):
        raise ValueError("unknown override stream id")
    decisions = tuple(evaluate_flow(flow, config, overrides.get(flow.id)) for flow in flows)
    if any(d.monthly_now is None or d.monthly_after is None for d in decisions):
        return FactoryResult(decisions, None, None, None, "needs_information")
    now = sum(d.monthly_now for d in decisions)
    after = sum(d.monthly_after for d in decisions)
    if not all(isfinite(x) for x in (now, after, after - now)):
        raise ValueError("factory totals exceed finite floating-point range")
    return FactoryResult(decisions, now, after, after - now,
                         "conditional" if any(d.bundle_status for d in decisions) else "calculated")
