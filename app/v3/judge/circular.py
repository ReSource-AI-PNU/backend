"""자원순환 효과: 폐기물이 어떤 경로로 순환하고, 무엇을 대체하며, 그 결과 어떤 효과를 내는가.

세 지표는 서로 다른 성과라 합치지 않는다. 모두 현행 처리(시나리오 C)에 견준 값이다.
- ghg: 예상 온실가스 감축량(tCO2e/월). 원료 생산·운송·재활용·잔재 처리·소각의 에너지 회수를 담은 전 과정
  배출의 차이다. 양수가 감축이고 음수는 늘어남이다.
- energy: 예상 전 과정 에너지 절감량(GJ/월). 새 원료 생산과 기존 처리에 드는 에너지에서 바꾼 경로에 드는
  에너지를 뺀 순절감이다. 소각으로 얻던 전기·열을 잃는 몫이 들어 있다. 소각장 운영 전기나 전기요금이 아니다.
- virgin: 신재 원료 대체량(kg/월). 재생원료를 써서 새로 만들 필요가 없어진 원료의 양이다.

방법(method)
- literature(문헌 추정): 적용 조건이 맞는 물량 × 문헌 순계수. 문헌 계수에 수율·신재 대체·잔재 처리·평균 수거
  운송이 이미 들어 있으므로 다시 곱하거나 더하지 않는다(실제 거리의 운송 배출은 처리 단계 배출 변화에 따로 있다).
- site(현장 추정): 담당자가 연결한 증빙과 투입량·회수수율·품질 대체율·실제 용도로 신재 대체량을 낸다.
- 자료가 없으면 0이 아니라 hold(산정 보류)다. 정의상 바뀌지 않는 것(현행 유지, 이미 재활용하던 물량의
  거래처만 바뀜)은 zero다.

문헌 계수(근거 시드 circular.json, 기준값 키 circular.<지표>.<재질>.<경로>[.<기준선>])
- 온실가스: EU JRC 2021 Annex 5 (B) 소각 대비·(C) 매립 대비 순효과(kg CO2-eq./t polymer waste)
- 신재 대체: 같은 연구 Table 2의 선별 수율 × 재활용 수율 × 신재 대체율
- 에너지: US EPA WARM v16 Exhibit 7-12 경로별 순에너지(million Btu/short ton)
- 철 스크랩 원료 절감: 세계철강협회(소각 대비 계수가 아니라 철강 원료 대체 참고값)
EU·미국 연구 시나리오라 소비 후 폐플라스틱(PCR)에만 쓰고, 공정 스크랩(PIR)·공정 재투입·혼합 재질·열분해에는
쓰지 않는다. 다른 재질의 값으로 대신하지 않는다.
"""

from __future__ import annotations

import math
from typing import Any

from app.v3 import names
from app.v3.evidence.provenance import compatible
from app.v3.evidence.units import UnitError, convert
from app.v3.judge.common import Recorder, current_process, num
from app.v3.textutil import josa

METRICS = ("ghg", "energy", "virgin")
UNIT = {"ghg": "tCO2e/월", "energy": "GJ/월", "virgin": "kg/월"}
SUFFIX = {"ghg": "GhgReduction", "energy": "EnergySaving", "virgin": "VirginDisplaced"}
# 현행 처리 방식(common.current_process) → 기준선 이름
BASELINE = {"incinerate": "incineration", "landfill": "landfill", "recycle": "recycle"}
BASELINE_LABEL = {"incineration": "소각", "landfill": "매립", "recycle": "재활용(판매)"}
ROUTE_LABEL = {
    "reuse": "공정 재투입",
    "recycle": "재활용 업체(물질 재활용)",
    "pyrolysis": "열분해",
    "landfill": "매립",
    "keep": "현행 유지",
    "unknown": "처리 방식 확인 필요",
}
# WARM 표의 경로 이름
ENERGY_ROUTE = {"incineration": "combustion", "landfill": "landfill", "recycle": "recycle"}
# 문헌 계수가 있는 재질. PET는 형태로 병급·비정질을 가린다
JRC_MATERIALS = ("PP", "HDPE", "LDPE", "PS", "EPS")
STEEL = ("철", "철 스크랩", "고철", "STEEL")
# 처리 단계 배출(국내 환경성적표지 계수)의 기준값 키에 쓰는 처리 이름
KEITI_PROCESS = {"incineration": "incinerate", "landfill": "landfill", "recycle": "recycle"}

ROUTE_NEEDS = {
    "reuse": [
        "분쇄 전력",
        "쓸 수 있는 배합비",
        "배합 뒤 불량률",
        "실제로 줄어든 신재 구입량",
    ],
    "recycle": [
        "업체의 재생원료 회수수율",
        "재생원료가 대신하는 신재와 대체율",
        "선별 잔재의 처리 방법",
        "재활용 공정의 전력·연료",
    ],
    "pyrolysis": [
        "열분해 공정 에너지",
        "열분해유 수율",
        "열분해유의 용도(화학 원료인지 연료인지)",
        "열분해유가 대신하는 대상",
    ],
    "landfill": ["받아 줄 매립장과 사업장폐기물 반입 조건"],
}


# ── 재질과 기준선 ───────────────────────────────────────────────────────
def material_key(stream: dict) -> tuple[str | None, str | None]:
    """(문헌 재질 키, 쓰지 못한 이유). 섞인 재질과 구분이 필요한 재질은 키가 없다."""
    raw = str(stream.get("material") or "").strip()
    if not raw:
        return None, "재질이 확인되지 않아 문헌값을 쓰지 않았습니다."
    if "+" in raw or stream.get("singleMaterial") is False:
        return None, (
            f"{raw}처럼 재질이 섞인 폐기물에는 한 재질의 재활용 계수(예: PP 기계적 재활용)를 쓰지 않았습니다."
        )
    upper = raw.upper()
    if upper in JRC_MATERIALS:
        return upper, None
    if upper == "PET":
        form = str(stream.get("form") or "")
        if "병" in form:
            return "PET-병급", None
        if any(word in form for word in ("시트", "트레이", "비정질")):
            return "PET-비정질", None
        return None, "PET는 병급과 비정질의 값이 크게 달라 형태를 확인한 뒤에 계산합니다."
    if upper == "PE":
        return None, "PE는 HDPE와 LDPE의 값이 달라 어느 쪽인지 확인한 뒤에 계산합니다."
    if raw == "골판지":
        return raw, None
    if upper in STEEL or raw in STEEL:
        return "철", None
    return None, f"{raw}의 문헌 계수를 등록하지 않아 계산하지 않았습니다."


def energy_key(material: str | None) -> str | None:
    """WARM은 PET를 나누지 않는다."""
    if material is None:
        return None
    return "PET" if material.startswith("PET") else material


def baseline_of(method: Any) -> str | None:
    return BASELINE.get(current_process(method) or "")


def destination(choice: dict, after_process) -> str:
    """배정 선택지 → 자원순환 경로. after_process는 common.after_process(업체, 단가)."""
    if choice.get("reuse"):
        return "reuse"
    if choice.get("vendor") is None:
        return "keep"
    if choice["vendor"].get("process") == "재활용":
        return "unknown"  # Generic recycling does not establish mechanical recycling.
    process = after_process(choice["vendor"], choice.get("price"))
    return {"recycle": "recycle", "pyrolysis": "pyrolysis"}.get(process or "", "unknown")


# ── 지표 하나 ──────────────────────────────────────────────────────────
def _hold(reason: str, needs=()) -> dict:
    return {
        "status": "hold",
        "method": None,
        "value": None,
        "basis": [],
        "formula": None,
        "reason": reason,
        "needs": list(needs),
    }


def _zero(reason: str) -> dict:
    return {
        "status": "zero",
        "method": None,
        "value": 0.0,
        "basis": [],
        "formula": None,
        "reason": reason,
        "needs": [],
    }


def _estimated(value: float, method: str, basis: list[str], formula: str, reason: str = "") -> dict:
    return {
        "status": "estimated",
        "method": method,
        "value": value,
        "basis": basis,
        "formula": formula,
        "reason": reason,
        "needs": [],
    }


def _common_hold(metric: str, dest: str, baseline: str | None) -> dict | None:
    """경로와 기준선만 보고 정할 수 있는 결과. 문헌을 볼 차례면 None."""
    if dest == "keep":
        return _zero("현행 처리를 그대로 둡니다.")
    if dest == "unknown":
        return _hold("업체의 처리 방식을 몰라 효과를 계산하지 않았습니다.", ["업체의 처리 방식"])
    if dest == "pyrolysis":
        return _hold(
            "열분해는 공정 에너지·수율·열분해유 용도·대신하는 대상의 근거가 없어 효과를 확정하지 않았습니다.",
            ROUTE_NEEDS["pyrolysis"],
        )
    if baseline is None:
        return _hold("현행 처리 방식을 몰라 견줄 기준선이 없습니다.", ["현행 처리 방식"])
    if dest == baseline:
        if dest == "recycle":
            return _zero(
                "이미 재활용하던 물량이라 거래처만 바뀝니다. 재활용 효과를 새 성과로 세지 않았습니다."
            )
        return _zero("현행과 같은 처리입니다.")
    if dest == "reuse" and metric != "virgin":
        return _hold(
            "공정 재투입은 분쇄 전력과 배합 뒤 불량률 같은 현장 자료로 계산합니다. "
            "소비 후 폐플라스틱 기준의 해외 연구값은 쓰지 않았습니다.",
            ROUTE_NEEDS["reuse"],
        )
    return None


def _literature_gate(stream: dict, material: str | None, why: str | None, dest: str) -> dict | None:
    """문헌값을 쓸 수 있는 폐기물인가. 쓸 수 없으면 산정 보류 결과."""
    needs = ROUTE_NEEDS.get(dest, [])
    if material is None:
        return _hold(why or "문헌 계수를 쓸 수 없는 재질입니다.", needs)
    origin = stream.get("origin")
    if origin == "PIR":
        return _hold(
            "공정에서 나온 스크랩(PIR)에는 소비 후 폐플라스틱 기준의 해외 연구값을 그대로 쓰지 않았습니다. "
            "업체의 실제 회수수율과 공정 자료로 계산합니다.",
            needs,
        )
    if origin != "PCR":
        return _hold(
            "출처(PIR·PCR)가 확인되지 않아 문헌값을 쓰지 않았습니다.", ["출처(PIR·PCR)", *needs]
        )
    return None


def ghg_effect(rec: Recorder, stream: dict, kg: float, baseline: str | None, dest: str) -> dict:
    found = _common_hold("ghg", dest, baseline)
    if found is not None:
        return found
    material, why = material_key(stream)
    if material == "철":
        return _hold(
            "철 스크랩의 온실가스 감축량은 기준선(소각·매립하지 않는 금속)과 철강 공정에 따라 달라 계산하지 않았습니다.",
            ["철 스크랩이 쓰이는 철강 공정", "현행 처리와 비교할 기준선"],
        )
    gate = _literature_gate(stream, material, why, dest)
    if gate is not None:
        return gate
    if dest not in ("recycle", "landfill"):
        return _hold("이 경로의 문헌 계수가 없습니다.", ROUTE_NEEDS.get(dest, []))
    vs_incineration = rec.basis(f"circular.ghg.{material}.recycle.incineration")
    vs_landfill = rec.basis(f"circular.ghg.{material}.recycle.landfill")
    if vs_incineration is None or vs_landfill is None:
        return _hold(
            f"{material}의 온실가스 문헌 계수가 없어 계산하지 않았습니다.",
            ROUTE_NEEDS.get(dest, []),
        )
    if (
        not compatible(vs_incineration, vs_landfill, metric="ghg")
        or vs_incineration.meta.get("material") != material
    ):
        return _hold(
            "기능단위·계산 경계·자료 버전이 호환되지 않아 문헌을 혼용하지 않았습니다.",
            ["호환되는 원문 계수"],
        )
    try:
        b = convert(float(vs_incineration.value), vs_incineration.unit, "tCO2e/t")
        c = convert(float(vs_landfill.value), vs_landfill.unit, "tCO2e/t")
    except UnitError:
        return _hold("탄소 계수의 단위가 호환되지 않습니다.", ["원문 단위 확인"])
    # 순효과(음수가 감축) B = 재활용 − 소각, C = 재활용 − 매립. 소각 대비 절감 s: 소각 0, 재활용 −B, 매립 C − B
    saving = {"incineration": 0.0, "recycle": -b, "landfill": c - b}
    per_t = saving[dest] - saving[baseline]
    used = (
        [vs_incineration.id]
        if {dest, baseline} == {"recycle", "incineration"}
        else [vs_landfill.id]
        if {dest, baseline} == {"recycle", "landfill"}
        else [vs_incineration.id, vs_landfill.id]
    )
    formula = (
        f"문헌 순감축계수 × 월 발생량(t). {BASELINE_LABEL[baseline]} → {ROUTE_LABEL[dest]}, "
        f"EU JRC 2021 Annex 5 {material}. 수율·신재 대체·잔재 처리·평균 수거 운송이 계수에 들어 있어 "
        "따로 곱하거나 더하지 않았습니다"
    )
    if len(used) == 2:
        formula += ". 매립 ↔ 소각 차이는 같은 표의 (C) 순효과 − (B) 순효과입니다"
    return _estimated(per_t * kg / 1000, "literature", used, formula)


def energy_effect(rec: Recorder, stream: dict, kg: float, baseline: str | None, dest: str) -> dict:
    found = _common_hold("energy", dest, baseline)
    if found is not None:
        return found
    material, why = material_key(stream)
    if material == "철":
        return _hold(
            "철 스크랩의 에너지 절감은 금속 종류와 철강 공정을 확인한 뒤에 계산합니다.",
            ["철 스크랩이 쓰이는 철강 공정"],
        )
    gate = _literature_gate(stream, material, why, dest)
    if gate is not None:
        return gate
    key = energy_key(material)
    to_route, from_route = ENERGY_ROUTE.get(dest), ENERGY_ROUTE.get(baseline)
    if to_route is None or from_route is None:
        return _hold("이 경로의 문헌 계수가 없습니다.", ROUTE_NEEDS.get(dest, []))
    after = rec.basis(f"circular.energy.{key}.{to_route}")
    before = rec.basis(f"circular.energy.{key}.{from_route}")
    if after is None or before is None:
        missing = to_route if after is None else from_route
        word = {"recycle": "재활용", "combustion": "소각", "landfill": "매립"}[missing]
        return _hold(
            f"원문 표(미국 환경보호청 WARM)에 {key} {word} 에너지 값이 없습니다(NA). 다른 재질의 값으로 대신하지 않았습니다.",
            ROUTE_NEEDS.get(dest, []),
        )
    if not compatible(before, after, metric="energy") or before.meta.get("material") != key:
        return _hold(
            "기능단위·에너지 종류·계산 경계가 호환되지 않아 합산하지 않았습니다.",
            ["같은 에너지 기준의 원문 계수"],
        )
    try:
        per_t = convert(float(before.value), before.unit, "GJ/t") - convert(
            float(after.value), after.unit, "GJ/t"
        )
    except UnitError:
        return _hold("에너지 계수의 단위가 호환되지 않습니다.", ["원문 단위 확인"])
    formula = (
        f"(현행 {BASELINE_LABEL[baseline]}의 순에너지 − {ROUTE_LABEL[dest]}의 순에너지) × 월 발생량(t). "
        "EPA WARM v16 Exhibit 7-12, million Btu/short ton × 1.05505585262 ÷ 0.90718474 = GJ/t. "
        "회수·제조 손실과 소각의 전기 회수가 표에 들어 있습니다"
    )
    return _estimated(per_t * kg / 1000, "literature", [after.id, before.id], formula)


def virgin_effect(
    rec: Recorder,
    stream: dict,
    kg: float,
    baseline: str | None,
    dest: str,
    reuse_kg=None,
    reuse_ev=(),
) -> dict:
    if dest == "reuse":
        from app.v3.judge.recovery import displaced

        reuse = stream.get("reuse") or {}
        value = displaced(reuse, min(kg, reuse_kg)) if reuse_kg is not None else None
        if value is not None and baseline in ("incineration", "landfill"):
            return _estimated(
                value,
                "site",
                list(dict.fromkeys([*reuse_ev, *reuse.get("recoveryEvidence", [])])),
                "재투입량 × 현장 회수수율 × 품질·용도별 신재 대체율. 담당자가 연결한 계량·시험 증빙에 따른 추정이며 인증 실적이 아닙니다.",
            )
        return _hold(
            "재투입량만으로 신재 대체량을 확정할 수 없습니다. 실제 신재 사용 감소와 수율·품질 증빙이 필요합니다.",
            ROUTE_NEEDS["reuse"],
        )
    found = _common_hold("virgin", dest, baseline)
    if found is not None:
        return found
    if dest == "landfill" and baseline != "recycle":
        return _zero("매립은 원료를 회수하지 않습니다.")
    material, why = material_key(stream)
    if material == "철":
        return _hold(
            "철 스크랩이 대신하는 신재(선철)의 양은 업체의 회수수율을 확인한 뒤에 계산합니다.",
            ["업체의 회수수율"],
        )
    gate = _literature_gate(stream, material, why, dest)
    if gate is not None:
        return gate
    share = rec.basis(f"circular.yield.{material}.recycle")
    if share is None:
        return _hold(f"{material}의 수율·신재 대체율 문헌값이 없습니다.", ROUTE_NEEDS["recycle"])
    rate = float(share.value) / 100
    meta = share.meta or {}
    if not compatible(share, metric="virgin") or meta.get("material") != material:
        return _hold("수율·대체율의 기능단위와 원문 확인이 필요합니다.", ROUTE_NEEDS["recycle"])
    rates = [num(meta.get(k)) for k in ("sortingRate", "recyclingRate", "substitutionFactor")]
    if any(v is None for v in rates) or not (
        0 <= rates[0] <= 100 and 0 <= rates[1] <= 100 and 0 <= rates[2] <= 1
    ):
        return _hold("수율·대체율이 없거나 유효 범위를 벗어납니다.", ROUTE_NEEDS["recycle"])
    rate = rates[0] / 100 * rates[1] / 100 * rates[2]
    if share.unit != "%" or not math.isclose(rate * 100, float(share.value), abs_tol=1e-6):
        return _hold(
            "수율·대체율의 곱과 등록된 합성 계수 또는 단위가 일치하지 않습니다.",
            ["계수 원문 재대조"],
        )
    detail = (
        f"선별 수율 {meta.get('sortingRate')}% × 재활용 수율 {meta.get('recyclingRate')}% × "
        f"신재 대체율 {meta.get('substitutionFactor')}"
    )
    if dest == "recycle":
        return _estimated(
            kg * rate,
            "literature",
            [share.id],
            f"월 발생량 × {detail} (EU JRC 2021 Table 2의 평균값. 업체의 실제 회수수율이 아닙니다)",
        )
    # 지금 재활용하던 물량을 매립하면 지금의 대체를 잃는다
    return _estimated(
        -kg * rate,
        "literature",
        [share.id],
        f"−월 발생량 × {detail}. 지금 재활용으로 대신하던 신재를 잃는 양입니다",
    )


def resource_refs(rec: Recorder, stream: dict, kg: float, dest: str) -> list[dict]:
    """철 스크랩을 재활용으로 보낼 때 세계철강협회 원료 절감 참고값(kg/월). 철이 아니면 빈 목록."""
    material, _ = material_key(stream)
    if material != "철" or dest != "recycle":
        return []
    out = []
    for name in ("철광석", "석탄", "석회석"):
        found = rec.basis(f"circular.resource.철.{name}")
        if found is None or found.meta.get("verificationStatus") != "original_checked":
            continue
        per_kg = convert(float(found.value), found.unit, "t/t")
        out.append({"name": name, "value": per_kg * kg, "basis": [found.id]})
    return out


# ── 설명 ───────────────────────────────────────────────────────────────
def explain(stream: dict, baseline: str | None, dest: str, effects: dict) -> dict:
    """기대효과 / 효과가 생기는 이유 / 필요한 실행조건 / 계산 근거. 숫자는 쓰지 않는다(숫자는 claim으로 보인다)."""
    material = str(stream.get("material") or "재질 미확인")
    before = {"incineration": "소각하던", "landfill": "매립하던", "recycle": "재활용하던"}.get(
        baseline or "", "지금 처리하던"
    )
    if dest == "reuse":
        effect = f"깨끗한 {material} 스크랩을 분쇄해 공정에 다시 넣으면 신재 {material} 투입 일부를 대신합니다."
        why = "현장 수율·품질·실제 사용 용도에 따라 신재 사용을 줄일 수 있습니다. 필요한 선별·세척과 공정 에너지는 현장 자료로 확인합니다."
    elif dest == "recycle" and baseline == "recycle":
        effect = (
            "이미 재활용하던 물량이라 거래처만 바뀝니다. 재활용 효과는 새로 생기는 성과가 아닙니다."
        )
        why = "지금도 재생원료가 되고 있어, 바뀌는 것은 단가와 운송 거리뿐입니다."
    elif dest == "recycle":
        effect = (
            f"{before} {josa(material, '이가')} 선별·세척·재생처리를 거쳐 재생원료가 되고, "
            f"그 재생원료가 신재 {material} 생산 일부를 대신합니다."
        )
        why = (
            "재생원료를 쓰는 만큼 원유에서 새 수지를 만드는 공정이 줄어듭니다. 넘긴 무게가 모두 재생원료가 되지는 않습니다"
            "(선별·세척 손실과 잔재)."
        )
    elif dest == "pyrolysis":
        effect = (
            "섞인 플라스틱을 열로 분해해 기름(열분해유)으로 바꿉니다. 열분해유를 화학 원료로 쓰면 원료가 순환하지만, "
            "연료로 태우면 에너지 회수에 가깝습니다."
        )
        why = "재질이 섞여 기계적 재활용이 어려운 물량을 소각 대신 다른 경로로 보냅니다."
    elif dest == "landfill":
        effect = "매립하면 원료를 회수하지 않아 신재를 대신하지 못하고, 소각에서 얻던 전기·열도 없어집니다."
        why = (
            "온실가스 수치는 소각보다 낮게 나올 수 있습니다. 플라스틱의 탄소가 타지 않고 땅에 남기 때문이며, "
            "자원이 순환한다는 뜻은 아닙니다."
        )
    elif dest == "keep" and baseline == "incineration":
        effect = "지금처럼 소각합니다. 원료 순환은 없고, 소각장이 전기·열을 회수하면 그만큼의 에너지만 얻습니다."
        why = "받아 줄 업체를 정하지 못했거나 옮기면 손익이 나빠져 현행을 유지합니다."
    elif dest == "keep":
        effect = f"지금 방식({BASELINE_LABEL.get(baseline or '', '확인 필요')})을 그대로 둡니다."
        why = "받아 줄 업체를 정하지 못했거나 옮기면 손익이 나빠져 현행을 유지합니다."
    else:
        effect = "업체의 처리 방식을 확인하면 효과를 설명할 수 있습니다."
        why = "처리 방식에 따라 대신하는 대상이 달라집니다."
    needs = list(dict.fromkeys(n for m in METRICS for n in effects[m]["needs"]))
    conditions = (
        f"{josa(', '.join(needs), '을를')} 확인해야 합니다."
        + (
            " 실제 효과는 재생원료 회수수율과 신재 대체율에 따라 달라집니다."
            if dest in ("recycle", "reuse")
            else ""
        )
        if needs
        else "실제 효과는 재생원료 회수수율과 신재 대체율에 따라 달라집니다."
        if dest in ("recycle", "reuse")
        else "추가로 확인할 실행조건이 없습니다."
    )
    return {"effect": effect, "why": why, "conditions": conditions, "basis": basis_text(effects)}


def basis_text(effects: dict) -> str:
    words = {"ghg": "온실가스", "energy": "에너지", "virgin": "신재 대체"}
    groups: dict[str, list[str]] = {}
    for metric in METRICS:
        found = effects[metric]
        if found["status"] == "estimated":
            # 화면 문장에는 숫자를 두지 않는다(자료의 연도·판은 출처 목록에 있다)
            source = {
                "literature": "EU 공동연구센터 연구"
                if metric != "energy"
                else "미국 환경보호청 WARM",
                "site": "현장 입력값",
            }[found["method"]]
            kind = "문헌 추정" if found["method"] == "literature" else "현장 추정"
            groups.setdefault(f"{kind}({source})", []).append(words[metric])
        elif found["status"] == "hold":
            groups.setdefault("산정 보류", []).append(words[metric])
        else:
            groups.setdefault("변화 없음", []).append(words[metric])
    parts = [f"{'·'.join(items)}: {head}" for head, items in groups.items()]
    text = " / ".join(parts)
    if any(effects[m]["method"] == "literature" for m in METRICS):
        text += ". 해외 연구 시나리오 값이며 이 사업장의 실측·인증 감축량이 아닙니다"
    return text + "."


def unconfirmed(
    stream: dict, baseline: str | None, baseline_assumed: bool, dest: str, effects: dict
) -> list[str]:
    """미확인 항목(숫자 없는 문장). 같은 문장은 한 번만 적는다."""
    out: list[str] = []

    def add(text: str) -> None:
        line = text if text.endswith(".") else f"{text}."
        if line not in out:
            out.append(line)

    if baseline_assumed:
        add("현행 처리 방식이 확인되지 않아 환경 효과를 보류했습니다")
    literature = any(effects[m]["method"] == "literature" for m in METRICS)
    if literature and "incineration" in (baseline, dest):
        add("지금 소각장이 전기·열을 회수하는지와 그 효율(문헌값은 회수하는 소각을 가정합니다)")
    if literature and dest in ("recycle", "landfill"):
        add("실제 수거 거리·차량·적재율·방문 횟수(문헌값에는 평균 수거 운송만 들어 있습니다)")
    if literature and dest == "recycle":
        for need in ROUTE_NEEDS["recycle"][:2]:
            add(need)
    for need in ROUTE_NEEDS.get(dest, []) if dest in ("landfill",) else []:
        add(need)
    for metric in METRICS:
        if effects[metric]["status"] == "hold":
            for need in effects[metric]["needs"]:
                add(need)
    return out


def stream_effect(
    rec: Recorder,
    stream: dict,
    kg: float,
    baseline: str | None,
    baseline_assumed: bool,
    dest: str,
    reuse_kg=None,
    reuse_ev=(),
) -> dict:
    if baseline_assumed or (stream.get("currentTreatment") or {}).get("methodAssumed"):
        baseline = None
    effects = {
        "ghg": ghg_effect(rec, stream, kg, baseline, dest),
        "energy": energy_effect(rec, stream, kg, baseline, dest),
        "virgin": virgin_effect(rec, stream, kg, baseline, dest, reuse_kg, reuse_ev),
    }
    material, _ = material_key(stream)
    virgin_material = None
    if effects["virgin"]["status"] == "estimated":
        virgin_material = (
            "PET" if (material or "").startswith("PET") else (material or stream.get("material"))
        )
    return {
        **effects,
        "virginMaterial": virgin_material,
        "resources": resource_refs(rec, stream, kg, dest),
        "unconfirmed": unconfirmed(stream, baseline, baseline_assumed, dest, effects),
        "explain": explain(stream, baseline, dest, effects),
    }


# ── 시나리오 ────────────────────────────────────────────────────────────
SCENARIOS = {
    "A": "흐름별 최적안",
    "B": "묶음 없이 개별 처리",
    "C": "현행 유지",
    "D": "전부 매립",
}


def _kg_evidence(stream: dict) -> list[str]:
    return list(
        stream.get("monthlyKgEvidence") or (stream.get("measure") or {}).get("evidence") or []
    )


def scenario(rec: Recorder, tag: str, rows: list[dict]) -> dict:
    """rows: [{"stream", "kg", "baseline", "baselineAssumed", "dest", "reuseKg", "reuseEv"}].

    폐기물마다 세 지표를 내고, 산정한 폐기물만 더한 부분합과 산정 보류 물량을 남긴다(claim 키로).
    """
    out_rows, sums = [], {m: [] for m in METRICS}
    held: dict[str, list[int]] = {m: [] for m in METRICS}
    counted: dict[str, list[int]] = {m: [] for m in METRICS}
    by_material: dict[str, list[tuple[float, str]]] = {}
    flows: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        stream, kg = row["stream"], row["kg"]
        who = stream.get("name") or None
        found = stream_effect(
            rec,
            stream,
            kg,
            row["baseline"],
            row["baselineAssumed"],
            row["dest"],
            row.get("reuseKg"),
            row.get("reuseEv") or (),
        )
        flows.setdefault(row["dest"], []).append(i)
        entry = {
            "streamId": stream.get("id"),
            "route": row["dest"],
            "baseline": row["baseline"],
            "baselineAssumed": row["baselineAssumed"],
            "virginMaterial": found["virginMaterial"],
            "unconfirmed": found["unconfirmed"],
            "explain": found["explain"],
            "resources": [],
        }
        for metric in METRICS:
            effect = found[metric]
            item = {
                "status": effect["status"],
                "method": effect["method"],
                "reason": effect["reason"],
                "claim": None,
                "basis": rec.visible(effect["basis"]),
            }
            if effect["status"] == "estimated":
                claim = rec.number(
                    f"ce{tag}_s{i}{SUFFIX[metric]}",
                    effect["value"],
                    UNIT[metric],
                    effect["formula"],
                    [*effect["basis"], *_kg_evidence(stream)],
                    label=names.calc_label(SUFFIX[metric], who),
                    private=True,
                )
                item["claim"] = claim["id"]
                sums[metric].append(claim["evidence"][0])
                counted[metric].append(i)
                if metric == "virgin":
                    by_material.setdefault(str(found["virginMaterial"]), []).append(
                        (float(claim["value"]), claim["evidence"][0])
                    )
                item["value"] = float(claim["value"])
            elif effect["status"] == "hold":
                held[metric].append(i)
            else:
                counted[metric].append(i)
            entry[metric] = item
        for k, ref in enumerate(found["resources"]):
            claim = rec.number(
                f"ce{tag}_s{i}r{k}VirginDisplaced",
                ref["value"],
                "kg/월",
                f"월 발생량 × 철 스크랩 1톤당 {ref['name']} 절감 참고값(세계철강협회, 세계 평균)",
                [*ref["basis"], *_kg_evidence(stream)],
                label=f"{who + ' ' if who else ''}{ref['name']} 절감 참고값 계산",
                private=True,
            )
            entry["resources"].append({"name": ref["name"], "claim": claim["id"]})
        out_rows.append(entry)

    def kg_claim(key: str, members: list[int], formula: str) -> str | None:
        if not members:
            return None
        made = rec.number(
            key,
            math.fsum(rows[i]["kg"] for i in members),
            "kg/월",
            formula,
            [e for i in members for e in _kg_evidence(rows[i]["stream"])],
            private=True,
        )
        return made["id"]

    totals = {}
    for metric in METRICS:
        estimated = [i for i in counted[metric] if out_rows[i][metric]["status"] == "estimated"]
        status = (
            "hold"
            if held[metric] and not estimated
            else "partial"
            if held[metric]
            else "estimated"
            if estimated
            else "zero"
        )
        total = None
        if estimated:
            total = rec.number(
                f"ce{tag}_total{SUFFIX[metric]}",
                math.fsum(float(out_rows[i][metric]["value"]) for i in estimated),
                UNIT[metric],
                "산정한 폐기물의 값만 더한 부분합. 산정 보류인 폐기물은 0으로 넣지 않고 뺐습니다"
                if held[metric]
                else "폐기물별 값의 합",
                sums[metric],
                label=f"시나리오 {tag} {names.LABELS[names.role_of(SUFFIX[metric])]} 합계 계산",
                private=True,
            )["id"]
        totals[metric] = {
            "status": status,
            "claim": total,
            "countedKg": kg_claim(
                f"ce{tag}_{metric}CountedRoutedKg",
                counted[metric],
                "산정한 폐기물(변화 없음 포함)의 월 발생량 합",
            ),
            "heldKg": kg_claim(
                f"ce{tag}_{metric}HeldRoutedKg", held[metric], "산정 보류한 폐기물의 월 발생량 합"
            ),
            "heldStreams": [rows[i]["stream"].get("id") for i in held[metric]],
        }
    virgin_by_material = []
    for material, parts in sorted(by_material.items()):
        made = rec.number(
            f"ce{tag}_m{len(virgin_by_material)}VirginDisplaced",
            math.fsum(v for v, _ in parts),
            "kg/월",
            f"신재 {material} 대체량 합",
            [e for _, e in parts],
            label=f"시나리오 {tag} 신재 {material} 대체량 합계 계산",
            private=True,
        )
        virgin_by_material.append({"material": material, "claim": made["id"]})
    flow_claims = {
        dest: kg_claim(
            f"ce{tag}_{dest}RoutedKg", members, f"{ROUTE_LABEL[dest]}로 가는 폐기물의 월 발생량 합"
        )
        for dest, members in sorted(flows.items())
    }
    recycled = [i for i, r in enumerate(rows) if r["dest"] in ("reuse", "recycle")]
    return {
        "id": tag,
        "label": SCENARIOS[tag],
        "rows": out_rows,
        "totals": totals,
        "virginByMaterial": virgin_by_material,
        "flows": flow_claims,
        "circularKg": kg_claim(
            f"ce{tag}_circularRoutedKg",
            recycled,
            "공정 재투입과 재활용 업체로 가는 폐기물의 월 발생량 합",
        ),
    }


def baseline_emission(rec: Recorder, rows: list[dict]) -> dict:
    """현행 처리 단계 배출(국내 환경성적표지 계수, 신재 대체·에너지 회수 미반영). 감축량 0이 배출 0이 아님을 보인다."""
    parts, missing = [], []
    for row in rows:
        stream, baseline = row["stream"], row["baseline"]
        process = KEITI_PROCESS.get(baseline or "")
        factor = rec.basis(f"emission.{process}.{stream.get('wasteItem')}") if process else None
        if (
            row.get("baselineAssumed")
            or (stream.get("currentTreatment") or {}).get("methodAssumed")
            or not compatible(factor, metric="process_ghg")
        ):
            missing.append(stream.get("id"))
            continue
        try:
            per_kg = convert(float(factor.value), factor.unit, "kgCO2e/kg")
        except UnitError:
            missing.append(stream.get("id"))
            continue
        parts.append(
            rec.number(
                f"ceC_s{len(parts)}BaselineEmission",
                per_kg * row["kg"] / 1000,
                "tCO2e/월",
                f"현행 처리({BASELINE_LABEL[baseline]}) 배출계수 × 월 발생량. 처리 공정의 배출만이며 "
                "신재 대체와 소각의 에너지 회수는 넣지 않았습니다",
                [factor.id, *_kg_evidence(stream)],
                label=names.calc_label("baselineEmission", stream.get("name")),
                private=True,
            )
        )
    total = None
    if parts:
        total = rec.number(
            "ceC_totalBaselineEmission",
            math.fsum(float(p["value"]) for p in parts),
            "tCO2e/월",
            "폐기물별 현행 처리 단계 배출의 합(국내 계수가 없는 처리는 뺐습니다)",
            [p["evidence"][0] for p in parts],
            private=True,
        )["id"]
    return {"claim": total, "missing": missing}


def build(
    rec: Recorder,
    usable: list[dict],
    best,
    unbundled,
    after_process,
    *,
    money_a: str | None,
    money_b: str | None,
    skipped: list[dict],
) -> dict:
    """네 시나리오. usable[i]에는 stream, kg, method, assumed가 있고, best·unbundled는 흐름마다 고른 선택지다."""

    def rows_for(dest_of) -> list[dict]:
        out = []
        for i, item in enumerate(usable):
            dest, reuse_kg, reuse_ev = dest_of(i)
            out.append(
                {
                    "stream": item["stream"],
                    "kg": item["kg"],
                    "baseline": baseline_of(item["method"]),
                    "baselineAssumed": bool(item["assumed"]),
                    "dest": dest,
                    "reuseKg": reuse_kg,
                    "reuseEv": reuse_ev,
                }
            )
        return out

    def chosen(combo):
        def dest_of(i):
            choice = combo[i]
            dest = destination(choice, after_process)
            if dest == "reuse":
                reuse = usable[i]["stream"].get("reuse") or {}
                limit = num(reuse.get("reusableKgPerMonth"))
                used = min(usable[i]["kg"], limit) if limit is not None else None
                return dest, used, list(reuse.get("evidence") or [])
            return dest, None, []

        return dest_of

    def fixed(dest):
        return lambda i: (dest, None, [])

    scenarios = []
    a_rows = rows_for(chosen(best))
    a = scenario(rec, "A", a_rows)
    a["money"] = money_a
    scenarios.append(a)
    bundle = {"available": unbundled is not None, "streams": [], "kg": None}
    if unbundled is not None:
        b_rows = rows_for(chosen(unbundled))
        b = scenario(rec, "B", b_rows)
        b["money"] = money_b
        b["available"] = True
        circular = ("reuse", "recycle")
        only = [
            i
            for i, (x, y) in enumerate(zip(a_rows, b_rows, strict=True))
            if x["dest"] in circular and y["dest"] not in circular
        ]
        bundle["streams"] = [usable[i]["stream"].get("id") for i in only]
        if only:
            bundle["kg"] = rec.number(
                "ceBundleKg",
                math.fsum(usable[i]["kg"] for i in only),
                "kg/월",
                "흐름별 최적안에서는 재활용 경로로 가지만 묶음 없이는 업체 최소 물량을 혼자 채우지 못해 그러지 못하는 "
                "폐기물의 월 발생량 합",
                [e for i in only for e in _kg_evidence(usable[i]["stream"])],
                private=True,
            )["id"]
    else:
        b = {
            "id": "B",
            "label": SCENARIOS["B"],
            "available": False,
            "reason": "폐기물·업체 조합이 많아 묶음 없는 배정을 따로 풀지 않았습니다.",
            "rows": [],
            "totals": {},
            "virginByMaterial": [],
            "flows": {},
            "circularKg": None,
            "money": None,
        }
    scenarios.append(b)
    c_rows = rows_for(fixed("keep"))
    c = scenario(rec, "C", c_rows)
    c["money"] = None
    c["baselineEmission"] = baseline_emission(rec, c_rows)
    scenarios.append(c)
    d_rows = rows_for(fixed("landfill"))
    d = scenario(rec, "D", d_rows)
    d["money"] = None
    d["moneyReason"] = "매립 처리비 자료가 없어 계산하지 않았습니다."
    scenarios.append(d)
    for item in scenarios:
        item.setdefault("available", True)
    baselines: dict[str, int] = {}
    for row in c_rows:
        key = row["baseline"] or "unknown"
        baselines[key] = baselines.get(key, 0) + 1
    return {
        "scenarios": scenarios,
        "baselines": baselines,
        "bundle": bundle,
        "excluded": [{"streamId": s.get("streamId"), "reason": s.get("reason")} for s in skipped],
    }
