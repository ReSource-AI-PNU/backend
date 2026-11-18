"""재활용·탄소 실적 리포트(배출 기업이 원청·고객사에 낼 자료).

2026-11-19 배출 사업장 설문에서 원하는 기능으로 "원청에 낼 재활용·탄소 감축 실적 리포트 만들어 주기"를 골랐고,
같은 응답자는 "따로 남기는 기록이 없다"고 답했다. 그래서 이 서비스에서 확정하고 검수한 거래가 그대로 실적 기록이 된다.

    GET /orgs/{orgId}/report?from=YYYY-MM-DD&to=YYYY-MM-DD   (배출 기업 본인과 운영자만)

- 실적(records): 업체가 확정 뒤 기록한 실제 결과(feedback.outcomes) 가운데 이 배출 기업의 것, 검수일이 기간 안인 것.
  줄마다 실제 중량·단가·등급은 업체 기록 근거(doc 또는 human)이고, 금액(중량 × 단가)과 탄소 변화(중량 × (재활용 계수
  − 현행 처리 계수))는 calc 근거다. 현행 처리를 입력하지 않았으면 혼합 소각으로 본다(결과 한 장과 같은 가정).
- 합계(totals): 실적만 더한다. 반출 무게, 매각 수익, 처리비, 탄소 변화(tCO2e. 줄었으면 음수).
- 예상(planned): 양쪽이 확정한 합의의 한 달 예상(결과 한 장의 배정). 검수 기록 전에도 보이고, 화면에서 "예상"으로 구분한다.
- 숫자는 모두 근거가 붙은 claim이다. 배출 기업만 보는 calc 근거로 남긴다.
"""

from __future__ import annotations

import re
from typing import Any

from app.v3 import feedback, ids
from app.v3.agents.pool import safe_id
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.judge.common import Recorder, carbon_per_kg
from app.v3.store import NotFound
from app.v3.untrusted import org_label, stream_label

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_METHOD = "혼합 소각"
PROCESS_LABELS = {
    "재활용": "재활용",
    "열분해": "열분해(화학적 재활용)",
    "소각": "소각",
    "매립": "매립",
}


def _claim(
    evidence_id: str | None, registry: Any, org_id: str, claim_id: str, label: str
) -> dict | None:
    if not evidence_id:
        return None
    try:
        found = registry.get(evidence_id, org_id)
    except (EvidenceNotFound, EvidenceForbidden):  # 볼 수 없는 근거는 숫자로 쓰지 않는다
        return None
    if not isinstance(found.value, (int, float)):
        return None
    return {
        "id": claim_id,
        "kind": "number",
        "value": float(found.value),
        "unit": found.unit,
        "evidence": [found.id],
        "label": label,
    }


class _ReadThrough:
    """계산 근거를 쓸 때 같은 ID가 이미 있으면 읽기만 한다. 체험하기(읽기 전용 레지스트리)에서는 시연 되돌리기가
    미리 만들어 둔 근거를 쓰고, 그래도 없으면 이 응답 안에서만 쓴다(근거 패널은 그 근거를 열지 못한다)."""

    def __init__(self, registry: Any, org_id: str) -> None:
        self.registry, self.org_id = registry, org_id

    def put(self, evidence: Evidence) -> Evidence:
        try:
            return self.registry.get(evidence.id, self.org_id)
        except (EvidenceNotFound, EvidenceForbidden):
            pass
        try:
            return self.registry.put(evidence)
        except Exception:  # noqa: BLE001 - 읽기 전용(체험하기)
            return evidence


def _calc(
    writer: _ReadThrough,
    org_id: str,
    key: str,
    value: float,
    unit: str,
    formula: str,
    inputs: list[str],
    label: str,
    today: str,
) -> dict[str, Any]:
    """계산 근거와 claim. ID는 조직·항목·입력·값으로만 정한다(날짜가 바뀌어도 같은 계산이면 같은 근거)."""
    value = round(float(value), 6)
    evidence_id = ids.calc_id(
        "outcome_report", {"org": org_id, "key": key, "inputs": inputs, "value": value}, key
    )
    saved = writer.put(
        Evidence(
            id=evidence_id,
            kind="calc",
            label=label,
            value=value,
            unit=unit,
            excerpt=formula,
            source="실적 리포트 계산",
            as_of=today,
            access=(org_id,),
            meta={"tool": "outcome_report", "formula": formula, "inputEvidence": inputs},
        )
    )
    return {
        "id": key,
        "kind": "number",
        "value": value,
        "unit": unit,
        "evidence": [saved.id, *inputs],
        "label": label,
    }


def build(app: Any, org_id: str, date_from: str | None, date_to: str | None) -> dict[str, Any]:
    """실적 리포트. 기간을 주지 않으면 모든 검수 기록이다."""
    for value in (date_from, date_to):
        if value is not None and not _DATE.match(value):
            raise ValueError("기간은 YYYY-MM-DD로 보내 주세요.")
    registry, store, today = app.registry, app.store, app.today
    rec = Recorder("outcome_report", registry, org_id, today, {})  # 배출계수 찾기에만 쓴다
    writer = _ReadThrough(registry, org_id)
    names: dict[str, str] = {}

    def name_of(org: str) -> str:
        if org not in names:
            try:
                names[org] = org_label(store.get_org(org).get("name"), "처리 업체")
            except NotFound:
                names[org] = "처리 업체"
        return names[org]

    records = []
    sums = {"kg": [], "revenue": [], "fee": [], "carbon": []}
    found = [
        r
        for r in feedback.outcomes(app)
        if r.get("emitterOrgId") == org_id
        and (date_from is None or r["inspectedOn"] >= date_from)
        and (date_to is None or r["inspectedOn"] <= date_to)
    ]
    for record in sorted(found, key=lambda r: (r["inspectedOn"], r["streamId"])):
        tag = safe_id(record["id"])
        try:
            stream = store.get_stream(record["streamId"], org_id)
        except NotFound:
            continue
        try:
            profile = store.get_vendor_profile(record["vendorOrgId"])
        except NotFound:
            profile = {}
        parts = record.get("evidence") or {}
        kg = _claim(parts.get("kg"), registry, org_id, f"{tag}_kg", "실제 중량")
        price = _claim(parts.get("unitPrice"), registry, org_id, f"{tag}_unitPrice", "실제 단가")
        label = stream_label(stream)
        amount = carbon = None
        if kg and price:
            amount = _calc(
                writer,
                org_id,
                f"{tag}_amount",
                kg["value"] * price["value"],
                "원",
                "실제 중량 × 실제 단가(+는 받은 돈, −는 낸 처리비)",
                [*price["evidence"], *kg["evidence"]],
                f"{label} 검수 {record['inspectedOn']} 금액 계산",
                today,
            )
            (sums["revenue"] if price["value"] >= 0 else sums["fee"]).append(amount)
        method = (stream.get("currentTreatment") or {}).get("method") or DEFAULT_METHOD
        if kg:
            per_kg, factors, _ = carbon_per_kg(
                rec, stream, profile, price["value"] if price else None, method
            )
            if per_kg is not None:
                carbon = _calc(
                    writer,
                    org_id,
                    f"{tag}_carbon",
                    per_kg * kg["value"],
                    "tCO2e",
                    f"실제 중량 × (보낸 곳 처리 계수 − 현행 처리({method}) 계수)",
                    [*factors, *kg["evidence"]],
                    f"{label} 검수 {record['inspectedOn']} 탄소 변화 계산",
                    today,
                )
                sums["carbon"].append(carbon)
            sums["kg"].append(kg)
        records.append(
            {
                "id": record["id"],
                "inspectedOn": record["inspectedOn"],
                "streamId": record["streamId"],
                "streamLabel": label,
                "streamName": stream.get("name"),
                "vendorOrgId": record["vendorOrgId"],
                "vendorName": name_of(record["vendorOrgId"]),
                "process": PROCESS_LABELS.get(
                    str(profile.get("process") or ""), profile.get("process")
                ),
                "grade": record.get("actualGrade"),
                "kg": kg,
                "unitPrice": price,
                "amount": amount,
                "carbon": carbon,
                "currentMethod": method,
                "currentMethodAssumed": not (stream.get("currentTreatment") or {}).get("method"),
            }
        )

    def total(key: str, items: list[dict], unit: str, formula: str, label: str) -> dict | None:
        if not items:
            return None
        value = sum(abs(c["value"]) if key == "fee" else c["value"] for c in items)
        inputs = [c["evidence"][0] for c in items]
        return _calc(writer, org_id, f"total_{key}", value, unit, formula, inputs, label, today)

    totals = None
    if records:
        totals = {
            "kg": total(
                "kg", sums["kg"], "kg", "검수 기록의 실제 중량 합", "기간 반출 무게 합계 계산"
            ),
            "revenue": total(
                "revenue",
                sums["revenue"],
                "원",
                "매입으로 받은 금액 합",
                "기간 매각 수익 합계 계산",
            ),
            "fee": total("fee", sums["fee"], "원", "처리비로 낸 금액 합", "기간 처리비 합계 계산"),
            "carbon": total(
                "carbon",
                sums["carbon"],
                "tCO2e",
                "검수 기록별 탄소 변화 합",
                "기간 탄소 변화 합계 계산",
            ),
            "count": len(records),
        }

    planned = []
    for session in store.list_sessions(org_id):
        if session.get("emitterOrgId") != org_id:
            continue
        result = (session.get("work") or {}).get("result") or {}
        confirmed = {
            t.get("vendorOrgId")
            for t in store.list_threads(session["id"])
            if t.get("status") == "confirmed" and t.get("vendorOrgId")
        }
        for assignment in result.get("assignments") or []:
            if assignment.get("vendorOrgId") not in confirmed:
                continue
            try:
                stream = store.get_stream(assignment["streamId"], org_id)
            except NotFound:
                continue
            planned.append(
                {
                    "sessionId": session["id"],
                    "streamId": assignment["streamId"],
                    "streamLabel": stream_label(stream),
                    "streamName": stream.get("name"),
                    "vendorOrgId": assignment["vendorOrgId"],
                    "vendorName": name_of(assignment["vendorOrgId"]),
                    "route": assignment.get("route"),
                    "unitPrice": assignment.get("unitPrice"),
                    "monthlyAfter": assignment.get("monthlyAfter"),
                    "carbon": assignment.get("carbon"),
                }
            )
    return {
        "orgId": org_id,
        "period": {"from": date_from, "to": date_to},
        "generatedOn": today,
        "records": records,
        "totals": totals,
        "planned": planned,
    }
