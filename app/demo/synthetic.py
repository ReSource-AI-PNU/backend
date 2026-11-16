"""Synthetic values transcribed from frontend #17/#18 demo, never real quotes."""

from app.agents.cost_carbon import CarbonConfig
from app.agents.demo_routes import DemoConfig, Detection, Flow

# 폐기물처분부담금(순환경제사회 전환 촉진법 제36조) = 처분량 × 부과요율 × 산정지수.
# 사업장 가연성 폐기물 요율: 소각 10원/kg, 매립 25원/kg (한국환경공단 폐기물처분부담금제도 안내).
# 산정지수는 2018년 1에서 매년 물가로 고시되나 현재 값을 확인하지 못해 1로 둔다(가정).
# 중소기업은 연매출 구간별로 50~100% 감면되지만 시연 공장의 매출을 몰라 감면하지 않는다(상한).
LEVY_RATE_PER_KG = {"incinerate": 10, "landfill": 25}
LEVY_INDEX = 1.0
LEVY_REDUCTION = 0.0


def levy_per_kg(method: str) -> float:
    return LEVY_RATE_PER_KG[method] * LEVY_INDEX * (1 - LEVY_REDUCTION)

# ⑤ 비용·탄소 시연값 (frontend mock pipeline.ts CARBON·LANDFILL_PER_KG, lib/carbon.ts KAU_PER_T)
CARBON_CONFIG = CarbonConfig(
    carbon_per_t={
        "incinerate": 0.78, "landfill": 0.51, "sell": -1.5, "bundle": -1.45,
        "reuse": -1.6, "pyrolysis": -0.3,
    },
    landfill_per_kg={"폐플라스틱": -230, "포장재": -230, "금속 스크랩": -180, "폐목재": -120},
    # 한국거래소 배출권(KAU) 2026-11 초 시세 약 3만 원/t (언론 보도 기준, 가정)
    kau_per_t=30000,
    kau_label="2026-11 초 시세",
    landfill_levy_per_kg=levy_per_kg("landfill"),
)

CONFIG = DemoConfig(
    # Explicit demo calculation coverage, not real-world eligibility.
    supported_routes={
        ("폐플라스틱", "PP"): frozenset({"reuse", "sell", "pyrolysis", "incinerate"}),
        ("폐플라스틱", "PP+ABS"): frozenset({"pyrolysis", "incinerate"}),
        ("포장재", "LDPE"): frozenset({"sell", "bundle", "incinerate"}),
        ("금속 스크랩", "FE"): frozenset({"sell", "incinerate"}),
        ("폐목재", "목재"): frozenset({"sell", "bundle", "incinerate"}),
    },
    minimum_kg={"폐플라스틱": 1000, "포장재": 1000, "금속 스크랩": 300, "폐목재": 500},
    # 혼합 소각 위탁비(처분부담금 제외). 폐합성수지(폐플라스틱·LDPE 포장재)는 한국폐기물거래소(KOWAX)
    # 폐합성수지류 처리 가격 전국 188,800원/톤(2025-12, 공개 화면 기준, 단위는 원/톤으로 판단)이다.
    # 금속 스크랩·폐목재는 프런트 mock 가정값을 그대로 둔다.
    current_per_kg={"폐플라스틱": -188.8, "포장재": -188.8, "금속 스크랩": -250, "폐목재": -120},
    sale_per_kg={"폐플라스틱": 300, "포장재": 150, "금속 스크랩": 260, "폐목재": 40},
    reuse_per_kg=900,
    pyrolysis_per_kg=-80,
    bundle_transport_per_kg=50,
    # 소량을 혼자 팔 때 출장비로 깎이는 단가 (frontend mock SOLO_PENALTY_PER_KG)
    solo_sale_penalty_per_kg=190,
    incinerate_levy_per_kg=levy_per_kg("incinerate"),
    labor_per_hour=15000,
    reuse_labor_hours=2,
    sell_labor_hours=2,
    bundle_labor_hours=1,
    reuse_max_contamination=0.02,
    sell_max_contamination=0.12,
    mix_threshold=0.05,
)

FLOWS = (
    Flow("sprue", "폐플라스틱", "PP", "PIR", 600, True, ()),
    Flow("defect", "폐플라스틱", "PP", "PIR", 400, True,
         (Detection("종이 라벨", 0.04), Detection("금속 인서트", 0.02))),
    Flow("purge", "폐플라스틱", "PP+ABS", "PIR", 250, False,
         (Detection("ABS 혼입", 0.35), Detection("탄화물", 0.05))),
    Flow("film", "포장재", "LDPE", "PCR", 300, True,
         (Detection("종이 태그", 0.02), Detection("테이프", 0.01))),
)
