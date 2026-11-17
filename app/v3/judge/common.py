"""도구 공통: calc 근거 기록, 거리, 등급, 매입 기준 대조."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from app.v3 import ids, names
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.intake import grade_model
from app.v3.judge import basis
from app.v3.textutil import josa

WORST = {"calculated": 0, "conditional": 1, "needs_information": 2}


def rnd(value: float) -> float:
    return round(float(value), 6)


def num(value: Any) -> float | None:
    """유한한 숫자만 받아들인다. bool, 문자열, NaN은 None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: WORST[s])


CALL_KEY = "call"  # 호출 기록 calc 근거의 값 이름


class Recorder:
    """도구 한 번 호출의 claim, check, calc 근거를 모은다."""

    def __init__(self, tool, registry, org_id, today, arguments, share: Iterable[str] = ()):
        self.tool, self.registry, self.org_id, self.today = tool, registry, org_id, today
        self.inputs = {"orgId": org_id, "today": today, "args": arguments}
        self.access = tuple(sorted({o for o in (org_id, *share) if o})) or ("*",)
        self.claims: list[dict] = []
        self.checks: list[dict] = []
        self.created: list[str] = []
        # calc 근거의 이름에 붙일 흐름 이름(흐름 하나를 다루는 도구일 때)
        stream = arguments.get("stream") if isinstance(arguments, dict) else None
        self.subject = (
            str(stream.get("name")) if isinstance(stream, dict) and stream.get("name") else None
        )

    def visible(self, evidence_ids: Iterable[str] | None) -> list[str]:
        """레지스트리에 있고 이 조직이 볼 수 있는 근거 ID만 남긴다."""
        out: list[str] = []
        for evidence_id in evidence_ids or ():
            if evidence_id in out:
                continue
            try:
                self.registry.get(evidence_id, self.org_id)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
            out.append(evidence_id)
        return out

    def basis(self, key: str) -> Evidence | None:
        return basis.resolve(self.registry, self.org_id, key)

    def number(self, key, value, unit, formula, input_evidence=(), label="", private=False) -> dict:
        """private이면 calc 근거를 부른 조직만 본다(현행 처리 단가처럼 상대에게 열지 않는 값)."""
        value = rnd(value)
        inputs = self.visible(input_evidence)
        evidence_id = ids.calc_id(self.tool, self.inputs, key)
        self.registry.put(
            Evidence(
                id=evidence_id,
                kind="calc",
                value=value,
                unit=unit,
                label=label or names.calc_label(key, self.subject, value),
                excerpt=formula,
                source=f"판단 도구 {self.tool}",
                as_of=self.today,
                access=(self.org_id,) if private and self.org_id else self.access,
                meta={
                    "tool": self.tool,
                    "inputs": self.inputs,
                    "formula": formula,
                    "inputEvidence": inputs,
                },
            )
        )
        self.created.append(evidence_id)
        claim = {
            "id": key,
            "kind": "number",
            "value": value,
            "unit": unit,
            "evidence": [evidence_id, *inputs],
        }
        self.claims.append(claim)
        return claim

    def clause(self, key, quote, evidence_id) -> dict:
        claim = {
            "id": key,
            "kind": "clause",
            "value": quote,
            "unit": None,
            "evidence": [evidence_id],
        }
        self.claims.append(claim)
        return claim

    def check(self, key, status, text, ask=None, evidence=()) -> dict:
        item = {
            "key": key,
            "status": status,
            "text": text,
            "ask": ask,
            "evidence": self.visible(evidence),
        }
        self.checks.append(item)
        return item

    def record_call(self, status: str) -> str:
        """호출 한 번의 기록을 calc 근거로 남긴다(숫자를 내지 않은 호출도 근거 저장소에 남는다).

        ID는 입력에서만 나오므로 같은 입력이면 같은 기록이다. 판정과 검사 결과를 담고, 검사에 쓴
        근거(허가증, 조항 등)와 이 호출이 낸 계산 근거를 입력 근거로 잇는다.
        """
        evidence_id = ids.calc_id(self.tool, self.inputs, CALL_KEY)
        checked = [e for check in self.checks for e in check.get("evidence") or []]
        linked = list(dict.fromkeys([*self.created, *self.visible(checked)]))
        summary = ", ".join(f"{c['key']}={c['status']}" for c in self.checks)
        text = f"판정 {status}" + (f". 검사: {summary}" if summary else "")
        self.registry.put(
            Evidence(
                id=evidence_id,
                kind="calc",
                label=f"{self.subject + ' ' if self.subject else ''}판단 도구 호출 기록",
                excerpt=text,
                source=f"판단 도구 {self.tool}",
                as_of=self.today,
                access=self.access,
                meta={
                    "tool": self.tool,
                    "inputs": self.inputs,
                    "formula": "판단 도구 호출 기록(판정과 검사 결과)",
                    "inputEvidence": linked,
                    "status": status,
                    "checks": [{"key": c["key"], "status": c["status"]} for c in self.checks],
                    "call": True,
                },
            )
        )
        return evidence_id

    def result(self, status: str, output: dict) -> dict:
        self.created.append(self.record_call(status))
        return {
            "tool": self.tool,
            "status": status,
            "output": output,
            "claims": self.claims,
            "checks": self.checks,
            "evidence": self.created,
        }


def haversine_km(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def distance_matrix(rec: Recorder, points, distance_provider=None):
    """(km 행렬, 가정 여부, 근거 ID 목록). 제공자가 없으면 직선 거리×우회 계수(가정값)."""
    if distance_provider is not None:
        road = distance_provider(points)
        if road is not None:
            return (
                [[float(v) for v in row] for row in road],
                False,
                _road_evidence(rec, points, distance_provider),
            )
    factor = rec.basis("distance.detourFactor")
    if factor is None:
        return None, True, []
    matrix = [[haversine_km(a, b) * float(factor.value) for b in points] for a in points]
    return matrix, True, [factor.id]


def _road_evidence(rec: Recorder, points, distance_provider) -> list[str]:
    """도로 거리 제공자가 조회한 값을 근거 저장소에 올리고 ID를 돌려준다(가정값이 아니라 조회한 값이다).

    같은 구간을 다른 조직의 도구가 다시 물어도 앞서 연 조직이 계속 볼 수 있게 접근 조직을 합친다.
    제공자가 evidence()를 내지 않으면(예: 근거 없는 행렬) 빈 목록이다.
    """
    make = getattr(distance_provider, "evidence", None)
    if make is None:
        return []
    out = []
    for record in make(points, rec.today, rec.access):
        try:
            seen = rec.registry.get(record.id, None)
            access = tuple(sorted({*seen.access, *record.access}))
        except EvidenceNotFound:
            access = tuple(record.access)
        out.append(rec.registry.put(replace(record, access=access)).id)
    return out


def grade_of(rec: Recorder, material, contamination, moisture):
    """(등급, 근거 ID). 등급 기준은 intake의 기준 파일(grade_thresholds) 하나만 쓴다."""
    evidence = rec.registry.put(grade_model.thresholds_evidence())
    result = grade_model.grade_by_thresholds(material, contamination, moisture)
    return result["grade"], [evidence.id]


def quality(stream: dict) -> dict:
    """흐름의 오염도·수분과 그 출처. 측정값이 사진 추정보다 우선한다."""
    measure, photo = stream.get("measure") or {}, stream.get("photo") or {}
    answers = stream.get("answers") or {}
    cont, basis_name, evidence = num(measure.get("contaminationPct")), "measured", measure
    if cont is None:
        cont, basis_name, evidence = num(photo.get("contaminationPct")), "photo_estimate", photo
    moist, moist_basis = num(measure.get("moisturePct")), "measured"
    if moist is None:
        moist, moist_basis = num(answers.get("moisturePct")), "answered"
    return {
        "contaminationPct": cont,
        "moisturePct": moist,
        "basis": basis_name if cont is not None else None,
        # 값마다의 출처. 측정값이 아니면 매입 기준을 충족해도 "확정"이 아니다(criterion_checks)
        "contaminationBasis": basis_name if cont is not None else None,
        "moistureBasis": moist_basis if moist is not None else None,
        "evidence": list(evidence.get("evidence") or []) + list(answers.get("evidence") or []),
    }


# 측정값이 아닌 출처의 이름. 이 출처의 값은 매입 기준 안이어도 조건부다
_ESTIMATED = {"photo_estimate": "사진 추정값", "answered": "담당자가 답한 값"}


def permit_match(stream: dict, vendor: dict):
    """(일치 여부 | None, 허가 근거 ID). 허가 품목이 올라와 있지 않으면 None."""
    permits = vendor.get("permits") or []
    evidence = [e for p in permits for e in (p.get("evidence") or [])]
    if not permits:
        return None, evidence
    for permit in permits:
        materials = permit.get("materials") or []
        if permit.get("wasteItem") == stream.get("wasteItem") and (
            not materials or stream.get("material") in materials
        ):
            return True, list(permit.get("evidence") or [])
    return False, evidence


def subject(word: str) -> str:
    """주격 조사를 붙인다: 받침이 있으면 '이', 없으면 '가'(오염도가, 수분이)."""
    return josa(word, "이가")


def criterion_checks(stream: dict, criterion: dict, q: dict) -> list[tuple]:
    """매입 기준 하나와 흐름을 대조한다. (key, status, text, ask) 목록. 본문에 숫자를 넣지 않는다."""
    out = []
    for field, key, name, basis_key, basis_check in (
        (
            "contaminationPct",
            "maxContaminationPct",
            "오염도",
            "contaminationBasis",
            "contamination_basis",
        ),
        ("moisturePct", "maxMoisturePct", "수분", "moistureBasis", "moisture_basis"),
    ):
        limit, value = num(criterion.get(key)), q[field]
        if limit is None:
            continue
        if value is None:
            out.append(
                (
                    field,
                    "conditional",
                    f"{name} 값이 없어 매입 기준과 대조하지 못했습니다.",
                    f"{name} 측정값 없이도 받을 수 있는지, 받는다면 어떤 조건인지 알려 주세요.",
                )
            )
        elif value <= limit:
            out.append((field, "met", f"{subject(name)} 매입 기준 상한 안에 있습니다.", None))
            source = _ESTIMATED.get(q.get(basis_key))
            if source:
                # 사진 추정이나 담당자 답만으로는 충족을 확정하지 않는다(추정 물량과 같은 원칙).
                # 값은 상한 안이지만 측정값이 없으므로 반입 검수에서 확인하는 조건을 붙인다
                out.append(
                    (
                        basis_check,
                        "conditional",
                        f"{subject(name)} 측정값이 아니라 {source}입니다. 반입 검수에서 확인합니다.",
                        (
                            f"{subject(name)} {source}입니다. 반입 검수에서 매입 기준 상한 안으로 "
                            "확인되면 같은 조건으로 받을 수 있는지 알려 주세요."
                        ),
                    )
                )
        else:
            out.append((field, "unmet", f"{subject(name)} 매입 기준 상한을 넘습니다.", None))
    want, have = criterion.get("form"), stream.get("form")
    if want:
        if not have:
            out.append(
                (
                    "form",
                    "conditional",
                    "흐름의 형태가 확인되지 않았습니다.",
                    f"형태를 모르는 상태입니다. {want} 형태가 아니어도 받을 수 있는지 알려 주세요.",
                )
            )
        elif have == want:
            out.append(("form", "met", "형태가 매입 기준과 같습니다.", None))
        else:
            out.append(
                (
                    "form",
                    "conditional",
                    f"흐름은 {have} 형태이고 매입 기준은 {want} 형태입니다.",
                    f"{have} 형태 그대로 받을 수 있는지, 전처리가 필요한지 알려 주세요.",
                )
            )
    return out


def _ranked_criteria(stream: dict, vendor: dict, q: dict) -> list[tuple]:
    ranked = []
    for criterion in vendor.get("criteria") or []:
        if criterion.get("material") != stream.get("material"):
            continue
        price = num(criterion.get("unitPriceKrwPerKg"))
        if price is None:
            continue
        checks = criterion_checks(stream, criterion, q)
        unmet = sum(1 for c in checks if c[1] == "unmet")
        conditional = sum(1 for c in checks if c[1] == "conditional")
        ranked.append(((unmet, conditional, -price, str(criterion.get("id"))), criterion, checks))
    ranked.sort(key=lambda r: r[0])
    return ranked


def ambiguous_forms(stream: dict, vendor: dict, q: dict) -> list[dict]:
    """형태를 모르는데 형태에 따라 매입 기준이 갈리면 그 기준들. 갈리지 않으면 빈 목록.

    가장 앞선 후보가 특정 형태를 요구하고, 같은 정도로 맞는 다른 후보가 다른 형태를 요구할 때다.
    이때 단가가 높은 쪽을 고르면 모르는 형태를 임의로 정하는 것이 된다.
    """
    if stream.get("form"):
        return []
    ranked = _ranked_criteria(stream, vendor, q)
    if not ranked or ranked[0][0][0] > 0 or not ranked[0][1].get("form"):
        return []
    top = ranked[0][0][:2]
    tied = [r[1] for r in ranked if r[0][:2] == top and r[1].get("form")]
    return tied if len({c.get("form") for c in tied}) > 1 else []


def best_criterion(stream: dict, vendor: dict, q: dict):
    """(매입 기준, 대조 결과). 불충족이 없는 기준 중 단가가 가장 높은 것, 동점이면 ID가 앞선 것.

    형태를 모르는데 형태에 따라 기준이 갈리면 어느 것도 고르지 않는다(None).
    """
    ranked = _ranked_criteria(stream, vendor, q)
    if not ranked or ambiguous_forms(stream, vendor, q):
        return None, []
    return ranked[0][1], ranked[0][2]


def stream_cautions(stream: dict) -> list[tuple]:
    """물량·재질·혼입에서 나오는 조건. (key, status, text, ask)."""
    out = []
    if stream.get("monthlyKgBasis") != "measured":
        out.append(
            (
                "monthly_kg_basis",
                "conditional",
                "월 발생량이 측정값이 아니라 추정값입니다.",
                "발생량이 추정값입니다. 실제 계량 뒤 물량이 달라져도 같은 조건으로 받을 수 있는지 알려 주세요.",
            )
        )
    if stream.get("materialBasis") == "estimated":
        out.append(
            (
                "material_basis",
                "conditional",
                "재질이 확인되지 않은 추정입니다.",
                "재질을 확인할 자료가 없습니다. 반입 전에 어떤 방법으로 재질을 확인하는지 알려 주세요.",
            )
        )
    mixed = [
        c.get("label") or c.get("name")
        for c in stream.get("contaminants") or []
        if not c.get("removed")
    ]
    if stream.get("singleMaterial") is False or mixed:
        names = ", ".join(str(m) for m in mixed if m) or "다른 재질"
        out.append(
            (
                "mixture",
                "conditional",
                f"혼입물이 있습니다: {names}.",
                f"{names} 혼입 상태로 받을 수 있는지, 분리나 전처리 조건이 있는지 알려 주세요.",
            )
        )
    return out


# ── 순비용의 나머지 항: 운반비와 전처리 인건비 ─────────────────────────────
# 회사 측 월 순비용 = 처리비 − 매각 수익 + 운반비 + 전처리 인건비 (설계안 v3 8.6).
# 운반비는 업체가 밝힌 조건(profile["transport"])을 따른다: 단가에 포함이면 0, 별도면 1회 운반비 × 월 횟수.
# 전처리 인건비는 담당자가 넣은 월 작업 시간(stream["pretreatHoursPerMonth"])이 있을 때만 계산한다.


def pickups_of(vendor: dict, pickups=None) -> float | None:
    return num(pickups) or num((vendor.get("guardrails") or {}).get("minPickupsPerMonth"))


def transport_charge(vendor: dict, pickups=None) -> dict:
    """업체 한 곳에 맡길 때 배출 기업이 따로 내는 월 운반비.

    {"known": 조건을 아는가, "paidBy", "monthly": 금액 | None, "perPickup", "pickups", "evidence"}
    - 조건이 없으면 known False (예전 프로필). 금액은 넣지 않고 "확인 필요"로 둔다.
    - 업체 부담(수거 포함)이면 0원.
    - 배출 기업 부담인데 1회 운반비나 월 횟수가 없으면 monthly None.
    """
    terms = vendor.get("transport") or {}
    paid_by = terms.get("paidBy")
    evidence = list(terms.get("evidence") or [])
    out = {
        "known": paid_by in ("vendor", "emitter"),
        "paidBy": paid_by,
        "monthly": None,
        "perPickup": num(terms.get("feePerPickupKrw")),
        "pickups": pickups_of(vendor, pickups),
        "evidence": evidence,
    }
    if paid_by == "vendor":
        out["monthly"] = 0.0
    elif paid_by == "emitter" and out["perPickup"] is not None and out["pickups"]:
        out["monthly"] = out["perPickup"] * out["pickups"]
    return out


def labor_cost(rec: Recorder, stream: dict):
    """(월 전처리 인건비 | None, 근거 ID, 시간, 시간당 단가). 작업 시간을 넣지 않았으면 None."""
    hours = num(stream.get("pretreatHoursPerMonth"))
    if hours is None or hours <= 0:
        return None, [], None, None
    own = num(stream.get("laborCostKrwPerHour"))
    if own is not None:
        return hours * own, list(stream.get("pretreatEvidence") or []), hours, own
    rate = rec.basis("labor.costPerHour")
    if rate is None:
        return None, [], hours, None
    evidence = [*list(stream.get("pretreatEvidence") or []), rate.id]
    return hours * float(rate.value), evidence, hours, float(rate.value)


# ── 탄소 ───────────────────────────────────────────────────────────────
# 탄소 변화 = (옮긴 뒤 처리의 배출계수 − 현행 처리의 배출계수) × 월 발생량.
# 계수는 환경성적표지 평가계수(공식값)다. 신재 대체 효과와 운송 배출은 넣지 않는다.
_AFTER_PROCESS = {"재활용": "recycle", "물질 재활용": "recycle", "열분해": "pyrolysis"}
CARBON_FORMULA = (
    "(옮긴 뒤 처리의 배출계수 − 현행 처리의 배출계수) × 월 발생량. "
    "신재 대체 효과와 운송 배출은 넣지 않았습니다."
)


def current_process(method) -> str | None:
    text = str(method or "")
    if "소각" in text:
        return "incinerate"
    if "매립" in text:
        return "landfill"
    return None


def after_process(vendor: dict, unit_price: float | None) -> str | None:
    """업체의 처리 방식. 프로필에 없으면 값을 주고 사 가는 경우만 재활용으로 본다."""
    process = vendor.get("process")
    if process:
        return _AFTER_PROCESS.get(str(process))
    return "recycle" if unit_price is not None and unit_price >= 0 else None


def carbon_per_kg(rec: Recorder, stream: dict, vendor: dict, unit_price, method):
    """(kg당 탄소 변화 tCO2e | None, 근거 ID, 계산하지 못한 이유)."""
    item = stream.get("wasteItem")
    now_key = current_process(method)
    if now_key is None:
        return None, [], "현행 처리 방식의 배출계수가 없어 탄소를 계산하지 않았습니다. 확인 필요."
    after_key = after_process(vendor, unit_price)
    if after_key is None:
        return None, [], "업체의 처리 방식을 몰라 탄소를 계산하지 않았습니다. 확인 필요."
    now_ef = rec.basis(f"emission.{now_key}.{item}")
    after_ef = rec.basis(f"emission.{after_key}.{item}")
    if now_ef is None or after_ef is None:
        return None, [], "배출계수가 없어 탄소를 계산하지 않았습니다. 확인 필요."
    return (float(after_ef.value) - float(now_ef.value)) / 1000, [now_ef.id, after_ef.id], None


# ── 배출 기업만의 흐름 항목 ─────────────────────────────────────────────
# 현행 처리(계약 단가), 재투입 입력(신재 단가), 전처리 작업 시간과 인건비는 업체에 넘기지 않는다.
EMITTER_ONLY_KEYS = (
    "currentTreatment",
    "reuse",
    "pretreatHoursPerMonth",
    "laborCostKrwPerHour",
    "pretreatEvidence",
)


def without_emitter_only(stream: dict) -> dict:
    """업체에 넘길 흐름. 배출 기업만의 항목을 뺀 새 dict."""
    return {k: v for k, v in stream.items() if k not in EMITTER_ONLY_KEYS}
