"""⑤ 비용·탄소: 경로 판정 결과를 공장 시나리오 A·B·C·D로 비교한다 (synthetic/demo).

프런트 `mock/pipeline.ts`의 `buildCostCarbon`과 `lib/carbon.ts`의 `kauOf`·`rebaseCarbon`을 옮겼다.
프런트와 달리 계산 중에는 반올림하지 않는다. 반올림은 화면에서만 한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from math import fsum, isfinite

from app.schemas import (
    Assumption,
    CarbonBaseline,
    CostCarbonResult,
    FeasibilityResult,
    Route,
    ScenarioLine,
    Stream,
    StreamDecision,
    StreamRoute,
)

ROUTE_LABEL: dict[str, str] = {
    "reuse": "공정 재투입",
    "sell": "매각",
    "bundle": "단지 묶음 매각",
    "pyrolysis": "열분해",
    "incinerate": "소각",
}
CARBON_KEYS = ("reuse", "sell", "bundle", "pyrolysis", "incinerate", "landfill")
BASELINES = ("신재 대비", "매립 대비")


@dataclass(frozen=True)
class CarbonConfig:
    carbon_per_t: dict[str, float]
    """경로(또는 landfill)별 tCO₂e/t, 신재 대비. +는 배출, −는 회피"""
    landfill_per_kg: dict[str, float]
    """품목별 매립 위탁 kg당 손익(처리비라 0 이하), 시나리오 D 전용"""
    kau_per_t: float
    """배출권(KAU) 가격 원/tCO₂e. 줄인 탄소를 금액으로 바꾸는 참고값"""
    kau_label: str
    """KAU 시세의 기준 시점·출처 표기 (가정값 안내용)"""
    landfill_levy_per_kg: float
    """매립 처분 시 배출자가 내는 폐기물처분부담금(원/kg, 0 이상). 시나리오 D 전용"""


def validate_carbon_config(config: CarbonConfig) -> None:
    missing = set(CARBON_KEYS) - set(config.carbon_per_t)
    if missing:
        raise ValueError(f"missing carbon factors: {sorted(missing)}")
    for key, value in config.carbon_per_t.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"invalid carbon factor: {key}")
    for item, value in config.landfill_per_kg.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"invalid landfill price: {item}")
        if value > 0:
            raise ValueError(f"landfill price must be nonpositive: {item}")
    levy = config.landfill_levy_per_kg
    if isinstance(levy, bool) or not isinstance(levy, (int, float)) or not isfinite(levy) or levy < 0:
        raise ValueError("landfill_levy_per_kg must be a finite nonnegative number")
    kau = config.kau_per_t
    if isinstance(kau, bool) or not isinstance(kau, (int, float)) or not isfinite(kau) or kau < 0:
        raise ValueError("kau_per_t must be a finite nonnegative number")


def _sum(values: list[float | None]) -> float | None:
    if any(v is None for v in values):
        return None
    total = fsum(v for v in values if v is not None)
    if not isfinite(total):
        raise ValueError("scenario total exceeds finite floating-point range")
    return total


def _kau(carbon_t: float | None, config: CarbonConfig) -> float | None:
    # 줄인 탄소(음수)가 클수록 큰 금액. 프런트 kauOf와 같지만 10원 단위 반올림은 화면에서 한다.
    # + 0.0은 탄소가 0일 때 −0.0 대신 0.0을 내기 위함이다.
    return None if carbon_t is None else -carbon_t * config.kau_per_t + 0.0


def _computed(decision: StreamDecision) -> bool:
    return (
        decision.status != "needs_information"
        and decision.route is not None
        and decision.monthly_now is not None
        and decision.monthly_after is not None
    )


def _line(stream_id: str, route: StreamRoute | None, monthly: float | None,
          carbon: float | None) -> ScenarioLine:
    return ScenarioLine(stream_id=stream_id, route=route, monthly_krw=monthly, carbon_t=carbon)


def _condition(decisions: Sequence[StreamDecision]) -> str:
    routes = {d.route for d in decisions if _computed(d)}
    parts = []
    if routes - {"incinerate"}:
        parts.append("분리 보관 구역")
    if "reuse" in routes:
        parts.append("분쇄기 사용")
    if "bundle" in routes:
        parts.append("단지 순회 수거 참여사 동의")
    return ", ".join(parts) or "없음"


def evaluate_cost_carbon(
    streams: Sequence[Stream],
    feasibility: FeasibilityResult,
    config: CarbonConfig,
    baseline: CarbonBaseline = "신재 대비",
) -> CostCarbonResult:
    """흐름별 경로 판정 결과를 시나리오 A·B·C·D로 묶어 월 손익·순 탄소·KAU를 낸다.

    - A 흐름별 최적안: 경로 판정의 "바꾸면" 값
    - B 묶음 없이: 묶음 흐름만 지금처럼 소각, 나머지는 A
    - C 현행 유지: 모든 흐름 소각("지금" 값)
    - D 전부 매립: 매립 위탁비와 매립 계수
    보류 흐름이 하나라도 있으면 그 흐름의 내역과 시나리오 합계는 None이다.
    매립 대비 기준은 같은 흐름의 D 탄소를 빼므로 D가 0이 되고, 시나리오끼리의 차이는 같다.
    """
    validate_carbon_config(config)
    if baseline not in BASELINES:
        raise ValueError("baseline must be 신재 대비 or 매립 대비")
    by_id = {s.id: s for s in streams}
    if len(by_id) != len(streams):
        raise ValueError("duplicate stream id")
    if not feasibility.decisions:
        raise ValueError("at least one decision is required")
    unknown = {d.stream_id for d in feasibility.decisions} - set(by_id)
    if unknown:
        raise ValueError(f"decisions refer to unknown streams: {sorted(unknown)}")

    factor = config.carbon_per_t
    scenarios: dict[str, list[ScenarioLine]] = {"A": [], "B": [], "C": [], "D": []}
    for d in feasibility.decisions:
        stream = by_id[d.stream_id]
        t = stream.monthly_kg / 1000
        if not _computed(d):
            for lines in scenarios.values():
                lines.append(_line(d.stream_id, d.route if lines is scenarios["A"] else None,
                                   None, None))
            continue
        incinerate = _line(d.stream_id, "incinerate", d.monthly_now, factor["incinerate"] * t)
        chosen = _line(d.stream_id, d.route, d.monthly_after, factor[d.route] * t)
        landfill_price = config.landfill_per_kg.get(stream.classify.item)
        landfill = _line(
            d.stream_id, "incinerate",
            None if landfill_price is None
            else (landfill_price - config.landfill_levy_per_kg) * stream.monthly_kg,
            None if landfill_price is None else factor["landfill"] * t,
        )
        scenarios["A"].append(chosen)
        # 묶음이 없으면 소량 포장재는 업체가 오지 않아 지금처럼 소각한다.
        scenarios["B"].append(incinerate if d.route == "bundle" else chosen)
        scenarios["C"].append(incinerate)
        scenarios["D"].append(landfill)

    if baseline == "매립 대비":
        # B는 A·C의 내역을 함께 쓰므로 제자리에서 고치지 않고 새 내역을 만든다.
        landfill_carbon = {line.stream_id: line.carbon_t for line in scenarios["D"]}

        def rebase(line: ScenarioLine) -> ScenarioLine:
            base = landfill_carbon[line.stream_id]
            carbon = None if line.carbon_t is None or base is None else line.carbon_t - base
            return line.model_copy(update={"carbon_t": carbon})

        scenarios = {sid: [rebase(line) for line in lines] for sid, lines in scenarios.items()}

    names = {s.id: s.name for s in streams}
    meta = {
        "A": ("흐름별 최적안",
              " · ".join(f"{names[d.stream_id]} {ROUTE_LABEL[d.route]}"
                         for d in feasibility.decisions if _computed(d)) or "계산 보류",
              _condition(feasibility.decisions)),
        "B": ("묶음 없이 개별 처리", "단지 묶음이 없으면 소량 포장재는 지금처럼 소각",
              "업체가 소량 포장재를 받지 않음"),
        "C": ("현행 유지 (전부 혼합 소각)", "모든 흐름을 혼합 사업장폐기물로 위탁", "없음"),
        "D": ("전부 매립", "매립 가능 지역까지 운송",
              "매립장 잔여 용량 부족(부산 약 5년 뒤 포화 예상)"),
    }
    has_bundle = any(_computed(d) and d.route == "bundle" for d in feasibility.decisions)
    routes = []
    for sid, lines in scenarios.items():
        monthly = _sum([line.monthly_krw for line in lines])
        carbon = _sum([line.carbon_t for line in lines])
        pending = monthly is None or carbon is None
        status = ("needs_information" if pending
                  else "conditional" if sid == "A" and has_bundle else "calculated")
        name, description, condition = meta[sid]
        routes.append(Route(
            id=sid, name=name, description=description, monthly_krw=monthly, carbon_t=carbon,
            kau_krw=_kau(carbon, config), condition=condition, recommended=sid == "A",
            status=status, breakdown=lines,
        ))

    def factor_text(key: str) -> str:
        return f"{factor[key]:+g}"

    assumptions = [
        Assumption(label="경로 판정", value=text) for text in (feasibility.assumptions or [])
    ] + [
        Assumption(label="매립 위탁비", value=", ".join(
            f"{item} {-price:g}원/kg" for item, price in config.landfill_per_kg.items()
        ) + f" + 폐기물처분부담금 {config.landfill_levy_per_kg:g}원/kg (시나리오 D, 가정)"),
        Assumption(label="탄소 계수", value=(
            f"신재 대비 tCO₂e/t: 재투입 {factor_text('reuse')}, 매각 {factor_text('sell')}, "
            f"묶음 매각 {factor_text('bundle')}, 열분해 {factor_text('pyrolysis')}, "
            f"소각 {factor_text('incinerate')}, 매립 {factor_text('landfill')} (가정)"
        )),
        Assumption(label="KAU", value=f"{config.kau_label} {config.kau_per_t:,.0f}원/t (가정)"),
        Assumption(label="탄소 기준", value=(
            "신재 대비: 재활용으로 피한 신재 생산까지 셈"
            if baseline == "신재 대비"
            else "매립 대비: 같은 흐름을 매립했을 때(시나리오 D)와의 차이. D가 0"
        )),
        Assumption(label="반올림", value="계산값은 반올림하지 않음. 화면에서만 반올림"),
    ]
    return CostCarbonResult(routes=routes, assumptions=assumptions, baseline=baseline,
                            data_mode="synthetic_demo")
