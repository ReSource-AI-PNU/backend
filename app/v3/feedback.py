"""재학습 데이터 수집 (7단계).

두 가지를 근거 레지스트리에 쌓는다. 따로 표를 두지 않고 근거의 meta["feedback"]에 기록을 싣기
때문에 메모리 레지스트리와 SQL 레지스트리에서 똑같이 돈다.

- 교정(correction): 배출 기업 담당자가 고친 박스·재질·출처 등. human 근거로 등록하고
  탐지 모델 재학습 데이터 후보로 내보낸다. 흐름의 값 자체는 바꾸지 않는다(흐름은
  PUT /orgs/{orgId}/streams/{streamId}로 고친다).
- 실제 결과(outcome): 확정 뒤에 업체가 기록한 실제 등급·단가·중량. 소유는 업체이고 배출 기업과
  나눈다. 등급 모델 학습 행과 기준값 보정 후보가 된다.

여기서 만드는 숫자는 실제 단가와 기준표 단가의 차이 하나뿐이고, calc 근거로 남긴다.
평균을 내거나 기준값을 자동으로 바꾸지 않는다. 모델 버전 표는 등록된 실행만 보여 준다.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.intake import grade_model
from app.v3.judge.common import quality
from app.v3.textutil import josa

DETECTOR_MODEL = "resource-ai-detector"
DETECTOR_FIELDS = ("box", "material", "origin", "form", "contaminant")
CORRECTION_FIELDS = (*DETECTOR_FIELDS, "grade", "contaminationPct", "moisturePct")
FIELD_LABELS = {
    "box": "탐지 박스",
    "material": "재질",
    "origin": "출처",
    "form": "형태",
    "contaminant": "혼입물",
    "grade": "등급",
    "contaminationPct": "오염도",
    "moisturePct": "수분",
}
ORIGINS = ("PIR", "PCR")
RETRAIN_MODELS = (grade_model.MODEL_NAME, DETECTOR_MODEL)
CALIBRATION_NOTE = (
    "건별 목록입니다. 평균을 내지 않고, 적은 표본으로 기준값을 자동으로 바꾸지 않습니다."
)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MISSING = object()


class FeedbackError(ValueError):
    """기록할 수 없는 입력. service가 ServiceError로 바꾼다."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _show(value: Any) -> str:
    if value is None:
        return "없음"
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return f"{value:g}"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _note(body: dict[str, Any]) -> str:
    note = body.get("note")
    return note.strip() if isinstance(note, str) else ""


def _feedback(app: Any, kind: str) -> list[Evidence]:
    """meta["feedback"]["type"]이 kind인 근거 전부(시스템 조회, ID 순서)."""
    return [
        item
        for item in app.registry.list(org_id=None)
        if isinstance(item.meta.get("feedback"), dict) and item.meta["feedback"].get("type") == kind
    ]


# ── 교정 ─────────────────────────────────────────────────────────────


def _checked_after(field: str, after: Any) -> Any:
    label = FIELD_LABELS[field]
    if field == "grade":
        if after not in grade_model.GRADES:
            raise FeedbackError(
                "INVALID_VALUE", f"등급은 {', '.join(grade_model.GRADES)} 가운데 하나입니다."
            )
        return after
    if field == "origin":
        if after not in ORIGINS:
            raise FeedbackError("INVALID_VALUE", "출처는 PIR 또는 PCR입니다.")
        return after
    if field in ("contaminationPct", "moisturePct"):
        number = _number(after)
        if number is None or not 0 <= number <= 100:
            raise FeedbackError(
                "INVALID_VALUE", f"{josa(label, '은는')} 0에서 100 사이의 숫자(%)입니다."
            )
        return number
    if field == "box":
        if not isinstance(after, (dict, list)) or not after:
            raise FeedbackError(
                "INVALID_VALUE", "탐지 박스는 좌표와 라벨을 담은 객체나 배열로 보내 주세요."
            )
        return after
    if not isinstance(after, str) or not after.strip():
        raise FeedbackError("INVALID_VALUE", f"{label}의 고친 값을 글자로 보내 주세요.")
    return after.strip()


def record_correction(
    app: Any, session: dict[str, Any], stream: dict[str, Any], body: dict[str, Any]
) -> dict[str, Any]:
    """배출 기업 담당자가 고친 값 하나를 human 근거로 등록한다. 권한은 service가 확인한다."""
    field = body.get("field")
    if field not in CORRECTION_FIELDS:
        raise FeedbackError(
            "INVALID_FIELD", f"고칠 수 있는 항목은 {', '.join(CORRECTION_FIELDS)}입니다."
        )
    if body.get("after", _MISSING) in (_MISSING, None, ""):
        raise FeedbackError("INVALID_VALUE", "고친 값(after)이 없습니다.")
    after = _checked_after(field, body["after"])
    before = body.get("before")
    if before == after:
        raise FeedbackError("NO_CHANGE", "고치기 전과 뒤의 값이 같습니다.")
    org_id, stream_id = stream["orgId"], stream["id"]
    seq = 1 + sum(
        1
        for item in _feedback(app, "correction")
        if item.meta["feedback"]["streamId"] == stream_id
        and item.meta["feedback"]["field"] == field
    )
    label = FIELD_LABELS[field]
    name = stream.get("name") or stream_id
    photo = list((stream.get("photo") or {}).get("evidence") or [])
    evidence_id = ids.human_id(org_id, f"교정-{stream_id}-{field}-{seq}")
    record = {
        "type": "correction",
        "id": evidence_id,
        "sessionId": session["id"],
        "orgId": org_id,
        "streamId": stream_id,
        "field": field,
        "before": before,
        "after": after,
        "note": _note(body),
        "seq": seq,
        "target": "detector" if field in DETECTOR_FIELDS else "grade",
        "photoEvidence": photo,
        "recordedOn": app.today,
    }
    scalar = isinstance(after, (str, float))
    app.registry.put(
        Evidence(
            id=evidence_id,
            kind="human",
            label=f"{name} {label} 교정",
            value=after if scalar else None,
            unit="%" if field in ("contaminationPct", "moisturePct") else None,
            excerpt=f"담당자 교정: {name} {label} {_show(before)} → {_show(after)}",
            source="담당자 교정(재학습 데이터 후보)",
            as_of=app.today,
            access=(org_id,),
            meta={"ownerOrgId": org_id, "inputEvidence": photo, "feedback": record},
        )
    )
    app.store.add_event(
        session["id"],
        "system",
        "feedback",
        f"담당자가 {name}의 {josa(label, '을를')} 고쳤습니다.",
        detail={
            "orgId": org_id,
            "type": "correction",
            "streamId": stream_id,
            "field": field,
            "evidence": [evidence_id],
        },
    )
    return record


def corrections(app: Any, session_id: str | None = None) -> list[dict[str, Any]]:
    found = [dict(item.meta["feedback"]) for item in _feedback(app, "correction")]
    return [r for r in found if session_id is None or r["sessionId"] == session_id]


def export_detection_corrections(app: Any) -> list[dict[str, Any]]:
    """탐지 모델 재학습 데이터 후보. 박스·재질·출처·형태·혼입물을 사람이 고친 기록이다.

    사진 근거 ID(photoEvidence)로 원본 이미지를 찾는다. 사진이 없는 흐름의 교정도 그대로 싣고
    hasPhoto로 표시한다(학습 작업이 걸러 쓴다).
    """
    return [
        {
            "source": record["id"],
            "orgId": record["orgId"],
            "streamId": record["streamId"],
            "field": record["field"],
            "before": record["before"],
            "after": record["after"],
            "photoEvidence": list(record["photoEvidence"]),
            "hasPhoto": bool(record["photoEvidence"]),
            "recordedOn": record["recordedOn"],
            "note": record["note"],
        }
        for record in corrections(app)
        if record["target"] == "detector"
    ]


# ── 실제 결과 ────────────────────────────────────────────────────────


def _agreed(session: dict[str, Any], thread_id: str, stream_id: str) -> dict[str, Any]:
    """합의 때의 기준표 단가(판단 도구가 낸 claim)와 업체가 제안한 단가."""
    work = session.get("work") or {}
    table = None
    criterion_id = None
    for entry in (work.get("threadTools") or {}).get(thread_id) or []:
        if entry.get("name") != "judge_route":
            continue
        if (entry.get("arguments") or {}).get("streamId") != stream_id:
            continue
        result = entry.get("result") or {}
        criterion_id = (result.get("output") or {}).get("criterionId")
        table = next((c for c in result.get("claims") or [] if c["id"] == "unitPrice"), None)
    offer = ((work.get("offers") or {}).get(thread_id) or {}).get(stream_id) or {}
    return {"table": table, "criterionId": criterion_id, "offer": offer.get("claim")}


def record_outcome(
    app: Any,
    session: dict[str, Any],
    thread: dict[str, Any],
    stream: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    """업체가 확정 뒤에 기록한 실제 등급·단가·중량. 권한과 스레드 상태는 service가 확인한다.

    documentName을 주면 doc 근거(검수 문서), 없으면 human 근거(담당자 입력)로 등록한다.
    같은 흐름과 검수일로 다시 보내면 같은 근거의 새 판이 된다.
    """
    inspected = body.get("inspectedOn")
    if not isinstance(inspected, str) or not _DATE.match(inspected):
        raise FeedbackError("INVALID_VALUE", "검수일(inspectedOn)은 YYYY-MM-DD로 보내 주세요.")
    if inspected > app.today:
        raise FeedbackError("INVALID_VALUE", "검수일이 오늘보다 뒤입니다.")
    grade = body.get("actualGrade")
    if grade is not None and grade not in grade_model.GRADES:
        raise FeedbackError(
            "INVALID_VALUE", f"실제 등급은 {', '.join(grade_model.GRADES)} 가운데 하나입니다."
        )
    price = body.get("actualUnitPriceKrwPerKg")
    if price is not None and _number(price) is None:
        raise FeedbackError("INVALID_VALUE", "실제 단가는 숫자(원/kg)로 보내 주세요.")
    kg = body.get("actualKg")
    if kg is not None and (_number(kg) is None or kg < 0):
        raise FeedbackError("INVALID_VALUE", "실제 중량은 0 이상의 숫자(kg)로 보내 주세요.")
    if grade is None and price is None:
        raise FeedbackError("INVALID_VALUE", "실제 등급이나 실제 단가 가운데 하나는 있어야 합니다.")

    vendor, emitter = thread["vendorOrgId"], thread["emitterOrgId"]
    stream_id = stream["id"]
    name = stream.get("name") or stream_id
    document = body.get("documentName")
    document = document.strip() if isinstance(document, str) else ""
    key = f"실제결과-{session['id']}-{stream_id}-{inspected}"

    def evidence_id(part: str) -> str:
        if document:
            return ids.doc_id(vendor, document, f"{key}-{part}")
        return ids.human_id(vendor, f"{key}-{part}")

    q = quality(stream)
    agreed = _agreed(session, thread["id"], stream_id)
    parts = {
        "grade": evidence_id("등급") if grade is not None else None,
        "unitPrice": evidence_id("단가") if price is not None else None,
        "kg": evidence_id("중량") if kg is not None else None,
    }
    record = {
        "type": "outcome",
        "id": key,
        "sessionId": session["id"],
        "threadId": thread["id"],
        "vendorOrgId": vendor,
        "emitterOrgId": emitter,
        "streamId": stream_id,
        "inspectedOn": inspected,
        "actualGrade": grade,
        "actualUnitPriceKrwPerKg": _number(price),
        "actualKg": _number(kg),
        "note": _note(body),
        "evidenceKind": "doc" if document else "human",
        "evidence": {k: v for k, v in parts.items() if v},
        "features": {
            "material": stream.get("material"),
            "contaminationPct": q["contaminationPct"],
            "moisturePct": q["moisturePct"],
            "basis": q["basis"],
            "evidence": list(q["evidence"]),
        },
        "criterionId": agreed["criterionId"],
        "tablePrice": agreed["table"],
        "offeredPrice": agreed["offer"],
        "deviation": None,
        "recordedOn": app.today,
    }
    source = (
        f"검수 문서 {document}(업체 제출)" if document else "업체 담당자 입력(확정 뒤 실제 결과)"
    )

    def put(part: str, label: str, value: Any, unit: str | None, text: str) -> None:
        app.registry.put(
            Evidence(
                id=parts[part],
                kind="doc" if document else "human",
                label=f"{name} {label}",
                value=value,
                unit=unit,
                excerpt=f"{inspected} 검수: {name} {text}",
                source=source,
                as_of=inspected,
                access=(vendor, emitter),
                meta={"ownerOrgId": vendor, "feedback": {**record, "part": part}},
            )
        )

    table = agreed["table"]
    if price is not None and table is not None:
        # 여기서 만드는 숫자는 이 차이 하나다. calc 근거로 남겨 계산 사슬을 따라갈 수 있게 한다
        difference = round(float(price) - float(table["value"]), 6)
        inputs = {"outcome": key, "actual": float(price), "table": float(table["value"])}
        calc_id = ids.calc_id("price_deviation", inputs, "difference")
        record["deviation"] = {
            "id": "difference",
            "kind": "number",
            "value": difference,
            "unit": "원/kg",
            "evidence": [calc_id, parts["unitPrice"], *table["evidence"]],
        }
        app.registry.put(
            Evidence(
                id=calc_id,
                kind="calc",
                label=f"{name} 실제 단가와 기준표 단가의 차이",
                value=difference,
                unit="원/kg",
                excerpt="실제 단가 − 매입 기준표 단가",
                source="재학습 데이터 수집(기준값 보정 후보)",
                as_of=inspected,
                access=(vendor, emitter),
                meta={
                    "tool": "price_deviation",
                    "inputs": {"orgId": vendor, **inputs},
                    "formula": "실제 단가 − 매입 기준표 단가",
                    "inputEvidence": [parts["unitPrice"], *table["evidence"]],
                },
            )
        )
    if grade is not None:
        put("grade", "실제 등급", grade, None, f"실제 등급 {grade}")
    if price is not None:
        put("unitPrice", "실제 단가", float(price), "원/kg", f"실제 단가 kg당 {float(price):g}원")
    if kg is not None:
        put("kg", "실제 중량", float(kg), "kg", f"실제 중량 {float(kg):g}kg")
    app.store.add_event(
        session["id"],
        "system",
        "feedback",
        f"업체가 {name}의 실제 결과를 기록했습니다.",
        thread_id=thread["id"],
        detail={
            "orgId": vendor,
            "type": "outcome",
            "streamId": stream_id,
            "evidence": list(record["evidence"].values()),
        },
    )
    return record


def outcomes(app: Any, session_id: str | None = None) -> list[dict[str, Any]]:
    """실제 결과 기록(검수 한 건에 하나). 등급·단가·중량 근거에 같은 기록이 실려 있어 하나로 묶는다."""
    found: dict[str, dict[str, Any]] = {}
    for item in _feedback(app, "outcome"):
        record = {k: v for k, v in item.meta["feedback"].items() if k != "part"}
        found[f"{record['vendorOrgId']}|{record['id']}"] = record
    return [
        record
        for _, record in sorted(found.items())
        if session_id is None or record["sessionId"] == session_id
    ]


def export_grade_labels(app: Any) -> list[dict[str, Any]]:
    """등급 모델 학습 행. 실제 등급이 있고 재질·오염도·수분이 모두 있는 기록만 행이 된다.

    합성 데이터(make_synthetic_dataset)와 같은 열 이름을 쓰고 synthetic은 항상 False다.
    """
    version = grade_model.thresholds()["version"]
    rows = []
    for record in outcomes(app):
        features = record["features"]
        if record["actualGrade"] is None or not features.get("material"):
            continue
        if features.get("contaminationPct") is None or features.get("moisturePct") is None:
            continue
        rows.append(
            {
                "material": str(features["material"]).strip().upper(),
                "contamination_pct": features["contaminationPct"],
                "moisture_pct": features["moisturePct"],
                "grade": record["actualGrade"],
                "synthetic": False,
                "thresholds_version": version,
                "quality_basis": features.get("basis"),
                "source": record["evidence"]["grade"],
                "stream_id": record["streamId"],
                "vendor_org_id": record["vendorOrgId"],
                "inspected_on": record["inspectedOn"],
            }
        )
    return rows


def gate_status(app: Any) -> dict[str, Any]:
    """배포 게이트. 실제 등급 행으로 지금 기준값 규칙을 평가하고 deploy_gate에 넣는다.

    재학습한 후보 모델의 점수는 Azure ML 작업(scripts/v3/ml/compare_and_gate.py)이 내고,
    그 결과는 register_model_run으로 등록된 것만 모델 버전 표에 나온다.
    """
    rows = export_grade_labels(app)
    metrics = grade_model.evaluate(rows)
    gate = grade_model.deploy_gate(metrics, len(rows))
    return {
        **gate,
        "realLabelCount": len(rows),
        "evaluated": {
            "model": f"등급 기준값 규칙 {grade_model.thresholds()['version']}",
            "n": metrics["n"],
            "accuracy": metrics["accuracy"],
            "label": metrics["label"],
        },
    }


def calibration_candidates(app: Any, viewer: str | None = None) -> dict[str, Any]:
    """기준값 보정 후보. 실제 단가와 매입 기준표 단가의 차이를 흐름·업체별로 건별로 늘어놓는다.

    viewer를 주면 그 조직이 당사자인 기록만 준다(None이면 전부).
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in outcomes(app):
        if record["deviation"] is None:
            continue
        if viewer is not None and viewer not in (record["vendorOrgId"], record["emitterOrgId"]):
            continue
        key = (record["streamId"], record["vendorOrgId"])
        group = groups.setdefault(
            key,
            {
                "streamId": record["streamId"],
                "vendorOrgId": record["vendorOrgId"],
                "criterionId": record["criterionId"],
                "count": 0,
                "items": [],
            },
        )
        group["count"] += 1
        group["items"].append(
            {
                "inspectedOn": record["inspectedOn"],
                "sessionId": record["sessionId"],
                "tablePrice": record["tablePrice"],
                "actualPrice": {
                    "id": "actualUnitPrice",
                    "kind": "number",
                    "value": record["actualUnitPriceKrwPerKg"],
                    "unit": "원/kg",
                    "evidence": [record["evidence"]["unitPrice"]],
                },
                "difference": record["deviation"],
            }
        )
    found = [groups[key] for key in sorted(groups)]
    return {
        "count": sum(g["count"] for g in found),
        "groups": found,
        "autoApply": False,
        "note": CALIBRATION_NOTE,
    }


# ── 모델 버전 표 ─────────────────────────────────────────────────────


def register_model_run(app: Any, run: dict[str, Any], owner: str) -> dict[str, Any]:
    """재학습 실행 하나를 등록한다(Azure ML 작업의 compare_and_gate.py가 낸 결과).

    받은 값을 그대로 적을 뿐 점수나 판정을 여기서 만들지 않는다.
    """
    model, version, run_id = run.get("model"), run.get("version"), run.get("runId")
    if model not in RETRAIN_MODELS:
        raise FeedbackError(
            "INVALID_VALUE", f"model은 {', '.join(RETRAIN_MODELS)} 가운데 하나입니다."
        )
    if not isinstance(version, str) or not version.strip():
        raise FeedbackError("INVALID_VALUE", "version을 글자로 보내 주세요.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise FeedbackError("INVALID_VALUE", "runId(MLflow 실행 ID)를 보내 주세요.")
    gate = run.get("gate")
    if not isinstance(gate, dict) or not isinstance(gate.get("allowed"), bool):
        raise FeedbackError("INVALID_VALUE", "gate.allowed(게이트 판정)를 참·거짓으로 보내 주세요.")
    metrics = run.get("metrics")
    if not isinstance(metrics, dict):
        raise FeedbackError("INVALID_VALUE", "metrics를 객체로 보내 주세요.")
    record = {
        "type": "model_run",
        "model": model,
        "version": version.strip(),
        "runId": run_id.strip(),
        "trainedOn": str(run.get("trainedOn") or app.today),
        "dataBasis": str(run.get("dataBasis") or ""),
        "realLabelCount": run.get("realLabelCount"),
        "syntheticCount": run.get("syntheticCount"),
        "metrics": metrics,
        "baseline": run.get("baseline") if isinstance(run.get("baseline"), dict) else None,
        "gate": {"allowed": gate["allowed"], "reason": str(gate.get("reason") or "")},
        "deployed": bool(run.get("deployed")),
        "registeredOn": app.today,
    }
    app.registry.put(
        Evidence(
            id=ids.model_id(model, record["version"], f"재학습-{record['runId']}"),
            kind="model",
            label=f"{model} {record['version']} 재학습 실행",
            excerpt=(
                f"{model} {record['version']} 재학습 실행 {record['runId']}: 게이트 "
                f"{'통과' if gate['allowed'] else '미통과'}. {record['gate']['reason']}"
            ).strip(),
            source="Azure ML 재학습 작업(compare_and_gate.py)이 낸 결과를 관리자가 등록",
            as_of=record["trainedOn"],
            access=(owner,),
            meta={"ownerOrgId": owner, "feedback": record},
        )
    )
    return record


def model_versions(app: Any) -> list[dict[str, Any]]:
    """등록된 재학습 실행. 등록된 것이 없으면 빈 목록이다(지어내지 않는다)."""
    runs = [dict(item.meta["feedback"]) for item in _feedback(app, "model_run")]
    return sorted(runs, key=lambda r: (r["model"], r["trainedOn"], r["version"], r["runId"]))


# ── 관리자 화면 표 ───────────────────────────────────────────────────


def retrain_table(app: Any, viewer: str | None = None) -> dict[str, Any]:
    """관리자 화면의 재학습 표. 건수는 전체, 건별 목록은 viewer가 당사자인 것만 싣는다."""
    recorded = outcomes(app)
    labelled = [r for r in recorded if r["actualGrade"] is not None]
    usable = export_grade_labels(app)
    fixed = corrections(app)
    by_field = {field: 0 for field in CORRECTION_FIELDS}
    for record in fixed:
        by_field[record["field"]] += 1
    return {
        "gradeLabels": {
            "count": len(labelled),
            "usable": len(usable),
            "incomplete": len(labelled) - len(usable),
            "note": "재질·오염도·수분이 모두 있는 기록만 학습 행으로 씁니다.",
        },
        "outcomes": {"count": len(recorded)},
        "corrections": {
            "count": len(fixed),
            "byField": {k: v for k, v in by_field.items() if v},
            "detectorCandidates": sum(1 for r in fixed if r["target"] == "detector"),
        },
        "gate": gate_status(app),
        "calibration": calibration_candidates(app, viewer),
        "modelVersions": model_versions(app),
    }
