"""도구 공통: calc 근거 기록, 거리, 등급, 매입 기준 대조."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from app.v3 import ids, names
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.intake import grade_model
from app.v3.judge import basis

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

    def result(self, status: str, output: dict) -> dict:
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
            return [[float(v) for v in row] for row in road], False, []
    factor = rec.basis("distance.detourFactor")
    if factor is None:
        return None, True, []
    matrix = [[haversine_km(a, b) * float(factor.value) for b in points] for a in points]
    return matrix, True, [factor.id]


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
    moist = num(measure.get("moisturePct"))
    if moist is None:
        moist = num(answers.get("moisturePct"))
    return {
        "contaminationPct": cont,
        "moisturePct": moist,
        "basis": basis_name if cont is not None else None,
        "evidence": list(evidence.get("evidence") or []) + list(answers.get("evidence") or []),
    }


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


def criterion_checks(stream: dict, criterion: dict, q: dict) -> list[tuple]:
    """매입 기준 하나와 흐름을 대조한다. (key, status, text, ask) 목록. 본문에 숫자를 넣지 않는다."""
    out = []
    for field, key, name in (
        ("contaminationPct", "maxContaminationPct", "오염도"),
        ("moisturePct", "maxMoisturePct", "수분"),
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
            out.append((field, "met", f"{name}가 매입 기준 상한 안에 있습니다.", None))
        else:
            out.append((field, "unmet", f"{name}가 매입 기준 상한을 넘습니다.", None))
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
