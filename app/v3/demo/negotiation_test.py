"""Synthetic vendor profiles for signed-in negotiation tests."""

from __future__ import annotations

from app.v3 import ids
from app.v3.demo.seed import _Seeder
from app.v3.evidence.model import Evidence
from app.v3.store import NotFound

COMPANY_NAME = "협상 테스트 회사"
MATERIALS = ("PP", "PE", "LDPE", "ABS", "PS", "PET", "PP+ABS")
REGION = "부산 강서구"
LOCATION = [128.826, 35.0871]

# 한국환경공단 2026-10-10 경남 잡색 중간가공품 판매가(원/kg).
# https://www.data.go.kr/data/3076421/fileData.do
STAGE_SALES = {
    "PE": {"압축": 482.6, "분쇄": 621.7, "펠릿": 888.3},
    "PP": {"압축": 390.5, "분쇄": 562.7, "펠릿": 723.7},
    "PET": {"압축": 600.0},
}
# LDPE 세부 시세와 ABS·PS·혼합재 인수가는 이 공표에서 확인할 수 없다.
# LDPE는 PE 압축품을 가격 규모의 참고로만 사용하고 나머지는 별도 시연값으로 둔다.
UNPRICED_BASE = {"ABS": 139, "PS": 82, "PP+ABS": -65}

# 제안 비율, 월 최소 물량, 오염도·수분 상한, 더 낼 수 있는 단가, 방문일, 1회 수거비.
# 이물·수분 조건의 참고 자료는 docs/v3/CONTRACT.md의 협상 테스트 회사 항목에 있다.
VENDOR_OPTIONS = (
    ("필름자원", 0.26, 100, 5, 3, 15, "화", None),
    ("순환소재", 0.30, 200, 4, 3, 10, "목", None),
    ("그린원료", 0.34, 300, 2, 2, 5, "금", 15_000),
)


def quote_prices(material: str, ratio: float):
    known = STAGE_SALES.get(material)
    if material == "LDPE":
        yield None, round(STAGE_SALES["PE"]["압축"] * ratio / 5) * 5
    elif known is not None:
        yield None, round(known["압축"] * ratio / 5) * 5
        for form, sale_price in known.items():
            # 선별·가공된 물량도 취급·판매에 드는 비용을 예시 단가에 반영한다.
            yield form, round(sale_price * (ratio + 0.25) / 5) * 5
    else:
        # 혼합재는 좋은 단일재보다 선별 비용이 커 처리비를 받는 경우를 재현한다.
        base = UNPRICED_BASE[material]
        relative_rate = ratio / 0.30
        adjusted_price = base * relative_rate if base > 0 else base / relative_rate
        yield None, round(adjusted_price / 5) * 5


def match_group(org: dict) -> str:
    if org.get("simulation") is True:
        return "simulation"
    return "demo" if org.get("demo") else "live"


def vendor_id(index: int) -> str:
    return f"org_negotiation_vendor_{index}"


def display_name(org_id: str | None, name: str | None) -> str | None:
    """Keep historic results readable without rewriting their saved quotes or messages."""
    if org_id and org_id.startswith("org_negotiation_vendor_") and name:
        return name.removeprefix("가상 · ")
    return name


def request_policy_id(org_id: str) -> str:
    return ids.human_id(org_id, "협상테스트-단가요청폭")


def request_policy(
    registry, org_id: str, today: str, *, vendors: tuple[str, ...] | None = None
) -> None:
    evidence_id = request_policy_id(org_id)
    if registry.exists(evidence_id):
        return
    if vendors is None:
        vendors = tuple(vendor_id(i) for i in range(1, len(VENDOR_OPTIONS) + 1))
    registry.put(
        Evidence(
            id=evidence_id,
            kind="human",
            label="협상 테스트용 추가 요청 폭",
            value=10,
            unit="%",
            excerpt="가상 협상에서 최초 단가보다 배출 기업에 10% 유리한 단가를 한 차례 요청합니다.",
            source="협상 테스트 시나리오",
            as_of=today,
            access=(org_id, *vendors),
            meta={"synthetic": True},
        )
    )


def ensure_vendors(store, registry, today: str) -> None:
    """Provision the three test vendors once; ordinary matching never sees them."""
    seeder = _Seeder(registry, today)
    for index, option in enumerate(VENDOR_OPTIONS, 1):
        name = option[0]
        org_id = vendor_id(index)
        try:
            store.get_vendor_profile(org_id)
        except NotFound:
            pass
        else:
            org = store.get_org(org_id)
            if org.get("simulation") and org.get("name") == f"가상 · {name}":
                store.put_org({**org, "name": name})
            continue

        seed_vendor(store, seeder, org_id, index, option, MATERIALS)


def seed_vendor(
    store, seeder, org_id: str, index: int, option: tuple, materials: tuple[str, ...]
) -> None:
    name, ratio, minimum, contamination, moisture, premium, day, fee = option
    store.put_org(
        {
            "id": org_id,
            "name": name,
            "role": "vendor",
            "region": REGION,
            "simulation": True,
            "synthetic": True,
        }
    )
    criteria = [
        seeder.criterion(
            org_id,
            f"pc_negotiation_{index}_{ids.slug(material)}_{ids.slug(form or '가공 전')}",
            material=material,
            form=form,
            grade="등외",
            unitPriceKrwPerKg=price,
            minMonthlyKg=minimum,
            maxContaminationPct=contamination,
            maxMoisturePct=moisture,
        )
        for material in materials
        for form, price in quote_prices(material, ratio)
    ]
    depot = [128.85 + index * 0.018, 35.13]
    store.put_vendor_profile(
        {
            "orgId": org_id,
            "permits": [
                seeder.typed_permit(org_id, item, list(materials))
                for item in ("폐플라스틱", "포장재")
            ],
            "criteria": criteria,
            "fleet": {
                **seeder.fleet(org_id, depot, 3_500, 24, 1_100),
                "routes": [{"name": "강서 노선", "days": [day], "regions": [REGION]}],
            },
            "transport": seeder.transport(org_id, "vendor" if fee is None else "emitter", fee),
            "process": "재활용",
            "guardrails": {"days": [day], "minPickupsPerMonth": 1},
            "simulationCounterPremium": premium,
            "synthetic": True,
        }
    )
