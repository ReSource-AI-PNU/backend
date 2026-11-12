"""Synthetic values transcribed from frontend #17/#18 demo, never real quotes."""

from app.agents.demo_routes import DemoConfig, Detection, Flow

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
    current_per_kg={"폐플라스틱": -250, "포장재": -250, "금속 스크랩": -250, "폐목재": -120},
    sale_per_kg={"폐플라스틱": 300, "포장재": 150, "금속 스크랩": 260, "폐목재": 40},
    reuse_per_kg=900,
    pyrolysis_per_kg=-80,
    bundle_transport_per_kg=50,
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
