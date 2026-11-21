"""검수 기록 한 건의 탄소 계산 기준을 기록할 때 고정한다.

2026-11-19 재현: 580kg 검수를 기록한 뒤 배출 기업이 현행 처리를 소각에서 매립으로만 바꿨는데, 실적 리포트가 같은
검수의 탄소를 −1.968493에서 +0.003130 tCO2e로 다시 계산했다. report.py가 볼 때마다 지금의 폐기물 자료, 업체 정보,
배출계수를 읽었기 때문이다. 그래서 탄소 계산에 쓰는 기준을 검수를 기록할 때 근거 하나에 적어 두고, 리포트는 그 값만 쓴다.

- 처음 기록할 때(capture): 비교한 현행 처리(기준선), 업체가 실제로 적용한 처리 방식, 두 배출계수의 값·단위·판·기준일,
  계산 범위(처리 방식 배출 차이만), 그리고 kg당 배출 차이(보낸 곳 계수 − 현행 계수, kgCO2e/kg)를 calc 근거에 적는다.
  현행 처리는 업체에 열지 않는 값이라 이 근거는 배출 기업만 보고, 업체와 나누는 검수 기록에는 싣지 않는다.
- 정정할 때: 무게·단가·등급만 고치면 기준은 그대로이고, 리포트가 고정한 kg당 차이 × 그 판의 실제 중량으로 탄소를 낸다.
  처리 방식을 고친 경우에만 그 판 번호로 근거를 하나 더 고정한다. 기준선과 현행 계수는 앞 판의 것을 물려받고, 바뀐
  처리의 계수만 정정한 날 기준으로 찾은 뒤 그렇게 적는다.
- 이 변경 전에 기록된 검수(reconstruct): 기록 당시 자료가 남아 있지 않다(흐름·업체 정보에는 수정 시각이 없고, 근거
  레지스트리는 같은 ID를 덮어쓴다). 확정한 합의의 배정 계산 근거(그때의 현행 처리, 월 발생량, 계수 ID가 남아 있다)를
  지금 계수로 다시 계산해 같은 값이 나오면 "합의 당시 기준으로 재구성"한다. 아니면 "당시 기준 확인 불가"로 두고
  탄소를 계산하지 않는다. 오늘 기준을 지난 기록에 소급하지 않는다. 재구성한 결과도 처음 읽을 때 고정한다.

고정한 근거는 같은 ID로 다시 쓰지 않는다(freeze).
"""

from __future__ import annotations

import math
from typing import Any

from app.v3 import current_cost as current_costs
from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.judge import basis as basis_keys
from app.v3.judge.common import current_process
from app.v3.store import NotFound

TOOL = "outcome_carbon"
FEEDBACK_TYPE = "outcome_carbon"
DEFAULT_METHOD = "혼합 소각"
FACTOR_UNIT = "kgCO2e/kg"

RECORDED, NOT_COMPUTED = "recorded", "not_computed"
RECONSTRUCTED, UNVERIFIED = "reconstructed", "unverified"
STATUS_LABELS = {
    RECORDED: "기록 당시 기준",
    NOT_COMPUTED: "계산하지 않음",
    RECONSTRUCTED: "합의 당시 기준으로 재구성",
    UNVERIFIED: "당시 기준 확인 불가",
}
# 합계에 넣는 상태. 기준을 확인할 수 없는 기록은 숫자를 만들지 않는다
COUNTED = (RECORDED, RECONSTRUCTED)

SCOPE = {
    "key": "process_only",
    "label": "처리 방식 배출 차이만",
    "includes": ["보낸 곳 처리 배출 − 현행 처리 배출"],
    "excludes": ["수거 운송 배출", "신재 대체 효과"],
    "text": "처리 방식이 바뀌어 달라진 배출만 넣었습니다. 수거 운송 배출과 신재 대체 효과는 넣지 않았습니다.",
}

# 업체가 검수 기록에 적는 실제 처리 방식
PROCESS_CHOICES = ("재활용", "열분해", "소각", "매립")
PROCESS_LABELS = {
    "재활용": "재활용",
    "물질 재활용": "재활용",
    "열분해": "열분해(화학적 재활용)",
    "소각": "소각",
    "매립": "매립",
}
APPLIED_SOURCES = {
    "vendor_input": "업체가 검수 기록에 적음",
    "vendor_profile": "기록 당시 업체 정보",
    "price": "업체 정보에 처리 방식이 없어 값을 받고 사 간 것을 재활용으로 봄",
    "deal": "합의 당시 배정",
    "unknown": "업체가 밝히지 않음",
}
BASELINE_SOURCES = {
    "stream": "기록 당시 폐기물 자료",
    "unknown": "기록 당시 현행 처리 방식 미확인",
    "assumed": "기록 당시 현행 처리 입력이 없어 혼합 소각으로 가정",
    "deal": "합의 당시 판단",
    "deal_assumed": "합의 당시 현행 처리 입력이 없어 혼합 소각으로 가정",
}
_FACTOR_UNITS = ("kgCO2e/kg", "tCO2e/t")  # 둘 다 kg당 kgCO2e와 같은 크기다
_KEY_LABELS = {"incinerate": "소각", "landfill": "매립", "recycle": "재활용", "pyrolysis": "열분해"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def process_key(process: Any) -> str | None:
    """처리 방식 이름 → 배출계수 키의 처리 부분. 열분해(화학적 재활용)는 재활용보다 먼저 본다."""
    text = str(process or "")
    if "열분해" in text:
        return "pyrolysis"
    if "소각" in text:
        return "incinerate"
    if "매립" in text:
        return "landfill"
    if "재활용" in text:
        return "recycle"
    return None


def process_label(process: Any) -> str | None:
    text = str(process or "").strip()
    return PROCESS_LABELS.get(text, text) or None


def applied_process(declared: Any, profile: dict[str, Any] | None, unit_price: Any) -> dict:
    """업체가 실제로 적용한 처리 방식. 검수 기록에 적은 것, 기록 당시 업체 정보, 값을 받고 사 간 것 순서로 본다.

    {"value", "source", "assumed"}. 업체 쪽 정보라 업체와 나누는 검수 기록에 그대로 싣는다.
    """
    if declared:
        return {"value": str(declared), "source": "vendor_input", "assumed": False}
    process = str((profile or {}).get("process") or "").strip()
    if process:
        return {"value": process, "source": "vendor_profile", "assumed": False}
    price = _number(unit_price)
    if price is not None and price >= 0:
        # judge.common.after_process와 같은 가정: 값을 주고 사 가면 재활용으로 본다
        return {"value": "재활용", "source": "price", "assumed": True}
    return {"value": None, "source": "unknown", "assumed": False}


def _applied(process: dict[str, Any] | None) -> dict[str, Any]:
    process = process or {}
    source = process.get("source") or "unknown"
    return {
        "process": process_label(process.get("value")),
        "key": process_key(process.get("value")),
        "source": source,
        "sourceLabel": APPLIED_SOURCES.get(source),
        "assumed": bool(process.get("assumed")),
    }


def _baseline_now(stream: dict[str, Any]) -> dict[str, Any]:
    """기록하는 지금 배출 기업 자료에 적힌 현행 처리. 판매 중이면 재활용으로 견준다(economics와 같다)."""
    current = stream.get("currentTreatment") or {}
    method = str(current.get("method") or "").strip()
    sale = current_costs.is_sale(current)
    if sale:
        method = current_costs.sale_method(method)
    assumed = (not method) or bool(current.get("methodAssumed"))
    method = method or "미확인"
    source = "unknown" if method == "미확인" else "assumed" if assumed else "stream"
    return {
        "method": method,
        "assumed": assumed,
        "process": current_process(current_costs.SALE_METHOD if sale else method),
        "source": source,
        "sourceLabel": BASELINE_SOURCES[source],
        "evidence": list(current.get("evidence") or []),
    }


def _factor_of(found: Evidence, name: str | None) -> dict[str, Any] | None:
    value = _number(found.value)
    if value is None or found.unit not in _FACTOR_UNITS:
        return None
    return {
        "key": name,
        "evidenceId": found.id,
        "kind": found.kind,
        "label": found.label,
        "value": value,
        "unit": found.unit,
        "version": found.meta.get("originalVersion", found.version),
        "asOf": found.as_of,
        "source": found.source,
        "meta": dict(found.meta),
    }


def _factor(registry: Any, org_id: str, key: str | None, item: Any) -> dict[str, Any] | None:
    """배출계수 하나를 값·단위·판·기준일과 함께 옮겨 적는다. 없거나 단위가 맞지 않으면 None."""
    if not key or not item:
        return None
    name = f"emission.{key}.{item}"
    found = basis_keys.resolve(registry, org_id, name)
    return _factor_of(found, name) if found is not None else None


def _difference(snapshot: dict[str, Any]) -> dict[str, Any]:
    """kg당 배출 차이(보낸 곳 계수 − 현행 계수, kgCO2e/kg). 정하지 못하면 상태와 이유를 적는다."""
    base, applied, factors = snapshot["baseline"], snapshot["applied"], snapshot["factors"]
    reason = None
    if not base.get("process"):
        reason = f"현행 처리({base.get('method') or '모름'})의 배출계수를 정할 수 없어"
    elif not applied.get("key"):
        reason = "업체가 실제로 적용한 처리 방식을 몰라"
    elif factors.get("baseline") is None or factors.get("applied") is None:
        reason = "그 처리 방식의 공인된 배출계수가 없어"
    elif base.get("assumed") or applied.get("assumed"):
        reason = "기존 또는 실제 처리 방식이 확인되지 않아"
    elif any(
        factors[k].get("meta", {}).get("verificationStatus") != "original_checked"
        for k in ("baseline", "applied")
    ):
        reason = "처리계수의 원문 계산 경계를 확인하지 못해"
    elif any(
        not factors["baseline"]["meta"].get(k)
        or factors["baseline"]["meta"].get(k) != factors["applied"]["meta"].get(k)
        for k in ("datasetVersion", "functionalUnit", "systemBoundary", "country", "material")
    ):
        reason = "처리계수의 계산 경계가 서로 달라"
    if reason is not None:
        if snapshot["status"] == RECORDED:
            snapshot["status"] = NOT_COMPUTED
        snapshot["perKg"] = None
        snapshot["reason"] = snapshot.get("reason") or f"{reason} 탄소를 계산하지 않았습니다."
        return snapshot
    snapshot["perKg"] = round(factors["applied"]["value"] - factors["baseline"]["value"], 9)
    return snapshot


def _shell(app: Any, record: dict[str, Any], revision: int, status: str, by: str) -> dict[str, Any]:
    return {
        "type": FEEDBACK_TYPE,
        "outcomeId": record["id"],
        "vendorOrgId": record["vendorOrgId"],
        "emitterOrgId": record["emitterOrgId"],
        "streamId": record["streamId"],
        "inspectedOn": record["inspectedOn"],
        "revision": revision,
        "status": status,
        "capturedOn": app.today,
        "capturedBy": by,
        "baseline": {
            "method": None,
            "assumed": False,
            "process": None,
            "source": None,
            "sourceLabel": None,
            "evidence": [],
        },
        "applied": _applied(record.get("appliedProcess")),
        "factors": {"baseline": None, "applied": None},
        "wasteItem": None,
        "perKg": None,
        "perKgUnit": FACTOR_UNIT,
        "scope": dict(SCOPE),
        "reason": None,
        "notes": [],
        "deal": None,
    }


def capture(
    app: Any,
    record: dict[str, Any],
    stream: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """검수 기록의 탄소 기준. previous(앞 판에 고정한 기준)를 주면 정정으로 보고 기준선과 현행 계수를 물려받는다."""
    revision = int(record.get("revision") or 1)
    emitter = record["emitterOrgId"]
    if previous is None:
        snapshot = _shell(app, record, revision, RECORDED, "record")
        snapshot["baseline"] = _baseline_now(stream)
        snapshot["wasteItem"] = item = stream.get("wasteItem")
        snapshot["factors"] = {
            "baseline": _factor(app.registry, emitter, snapshot["baseline"]["process"], item),
            "applied": _factor(app.registry, emitter, snapshot["applied"]["key"], item),
        }
        return _difference(snapshot)
    # 처리 방식을 고친 정정: 기준선·현행 계수·상태는 앞 판의 것. 오늘의 폐기물 자료로 바꾸지 않는다
    status = previous["status"] if previous["status"] in (RECONSTRUCTED, UNVERIFIED) else RECORDED
    snapshot = _shell(app, record, revision, status, "correction")
    snapshot.update(
        baseline=dict(previous["baseline"]),
        wasteItem=previous.get("wasteItem") or stream.get("wasteItem"),
        deal=previous.get("deal"),
        reason=previous.get("reason") if status == UNVERIFIED else None,
        notes=[
            *(previous.get("notes") or []),
            f"{app.today} 정정 판 {revision}에서 처리 방식을 바꿔, 그 처리의 배출계수는 정정한 날 기준으로 찾았습니다.",
        ],
        basedOn={"revision": previous.get("revision"), "capturedOn": previous.get("capturedOn")},
    )
    snapshot["factors"] = {
        "baseline": (previous.get("factors") or {}).get("baseline"),
        "applied": _factor(
            app.registry, emitter, snapshot["applied"]["key"], snapshot["wasteItem"]
        ),
    }
    if status == UNVERIFIED:
        return snapshot
    return _difference(snapshot)


# ── 고정과 읽기 ──────────────────────────────────────────────────────


def snapshot_id(record: dict[str, Any], revision: int | None = None) -> str:
    """검수 기록의 탄소 기준 근거 ID. 기록 id(검수 ID)·업체·기준을 고정한 판으로 정해진다."""
    return ids.calc_id(
        TOOL,
        {
            "outcome": record["id"],
            "vendor": record["vendorOrgId"],
            "revision": int(revision or record.get("revision") or 1),
        },
        "perKg",
    )


def _formula(snapshot: dict[str, Any]) -> str:
    factors, base, applied = snapshot["factors"], snapshot["baseline"], snapshot["applied"]
    if snapshot.get("perKg") is None:
        head = snapshot.get("reason") or "탄소를 계산하지 않았습니다."
    else:
        head = (
            f"보낸 곳 처리 계수 {factors['applied']['value']:g} − 현행 처리 계수 "
            f"{factors['baseline']['value']:g} = kg당 {snapshot['perKg']:g} {FACTOR_UNIT}."
        )
    return " ".join(
        [
            head,
            f"비교한 현행 처리: {base.get('method') or '모름'}({base.get('sourceLabel') or '확인 불가'}).",
            f"적용한 처리: {applied.get('process') or '모름'}({applied.get('sourceLabel') or '확인 불가'}).",
            SCOPE["text"],
        ]
    )


def freeze(registry: Any, snapshot: dict[str, Any], record: dict[str, Any], label: str) -> Evidence:
    """탄소 기준을 근거 레지스트리에 한 번만 쓴다. 이미 있으면 그것을 돌려준다(덮어쓰지 않는다)."""
    evidence_id = snapshot_id(record, snapshot["revision"])
    try:
        return registry.get(evidence_id, None)
    except EvidenceNotFound:
        pass
    factors = [f["evidenceId"] for f in snapshot["factors"].values() if f]
    formula = _formula(snapshot)
    evidence = Evidence(
        id=evidence_id,
        kind="calc",
        label=label,
        value=snapshot.get("perKg"),
        unit=FACTOR_UNIT if snapshot.get("perKg") is not None else None,
        excerpt=formula,
        source="검수 기록의 탄소 계산 기준(기록할 때 고정)",
        as_of=snapshot["capturedOn"],
        access=(snapshot["emitterOrgId"],),
        meta={
            "tool": TOOL,
            "formula": formula,
            "inputEvidence": list(dict.fromkeys(factors)),
            "ownerOrgId": snapshot["emitterOrgId"],
            "feedback": snapshot,
        },
    )
    try:
        return registry.put(evidence)
    except Exception:  # noqa: BLE001 - 읽기 전용 레지스트리(체험하기)에서는 이 응답 안에서만 쓴다
        return evidence


def latest(registry: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    """이 판에 쓰는 고정 기준: 지금 판 번호 이하에서 가장 최근에 고정한 것. 없으면 None."""
    for revision in range(int(record.get("revision") or 1), 0, -1):
        try:
            found = registry.get(snapshot_id(record, revision), None)
        except (EvidenceNotFound, EvidenceForbidden):
            continue
        snapshot = (found.meta or {}).get("feedback")
        if isinstance(snapshot, dict):
            return {**snapshot, "evidenceId": found.id}
    return None


def basis_for(app: Any, record: dict[str, Any], writer: Any | None = None) -> dict[str, Any]:
    """이 판의 탄소 기준. 고정한 것이 없으면(이 변경 전의 기록) 이전 형식의 기록 당시 기준을 옮기고, 그것도
    없으면 합의 기록에서 재구성해 판 1로 고정한다."""
    found = latest(app.registry, record)
    if found is not None:
        return found
    snapshot = from_earlier_basis(app, record) or reconstruct(app, record)
    saved = freeze(writer or app.registry, snapshot, record, snapshot_label(record))
    return {**snapshot, "evidenceId": saved.id}


def snapshot_label(record: dict[str, Any]) -> str:
    return f"{record['inspectedOn']} 검수의 kg당 탄소 변화 기준"


def on_write(
    app: Any,
    record: dict[str, Any],
    stream: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """검수 기록을 쓴 뒤 부른다. 처음 기록이면 기준을 고정하고, 정정이면 처리 방식이 바뀐 경우에만 새로 고정한다."""
    if previous is None:
        snapshot = capture(app, record, stream)
    else:
        before = (previous.get("appliedProcess") or {}).get("value")
        if process_key(before) == process_key((record.get("appliedProcess") or {}).get("value")):
            return None
        snapshot = capture(app, record, stream, basis_for(app, previous))
    freeze(app.registry, snapshot, record, snapshot_label(record))
    return snapshot


def carbon(snapshot: dict[str, Any], kg: Any) -> float | None:
    """이 판의 탄소 변화(tCO2e) = 실제 중량 × kg당 배출 차이 ÷ 1000. 합계에 넣지 않는 상태면 None."""
    weight, per_kg = _number(kg), _number(snapshot.get("perKg"))
    if snapshot.get("status") not in COUNTED or weight is None or per_kg is None:
        return None
    return round(weight * per_kg / 1000, 6)


# ── 이 변경 전의 기록 ────────────────────────────────────────────────

EARLIER_TOOL = (
    "outcome_carbon_basis"  # 2026-11-19 한때 쓴 기록 당시 기준의 형식(검수 기록마다 근거 하나)
)


def earlier_basis_id(record: dict[str, Any]) -> str:
    return ids.calc_id(
        EARLIER_TOOL, {"outcome": record["id"], "emitter": record["emitterOrgId"]}, "carbonBasis"
    )


def from_earlier_basis(app: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    """이전 형식으로 검수를 기록할 때 남긴 기준(현행 처리, 업체 처리 공정, 배출계수 ID·판·값)을 이 형식으로 옮긴다.

    기록할 때 남긴 값이므로 RECORDED다. 계수 값은 그때 옮겨 적은 값을 쓰고 지금 레지스트리를 읽지 않는다.
    """
    try:
        found = app.registry.get(earlier_basis_id(record), None)
    except EvidenceNotFound:
        return None
    old = (found.meta or {}).get("carbonBasis")
    if not isinstance(old, dict):
        return None
    snapshot = _shell(app, record, 1, RECORDED, "record")
    snapshot["capturedOn"] = old.get("capturedOn") or found.as_of
    method = str(old.get("currentMethod") or DEFAULT_METHOD)
    assumed = bool(old.get("currentMethodAssumed"))
    source = "assumed" if assumed else "stream"
    snapshot["baseline"] = {
        "method": method,
        "assumed": assumed,
        "process": current_process(method),
        "source": source,
        "sourceLabel": BASELINE_SOURCES[source],
        "evidence": [],
    }
    vendor_process = old.get("vendorProcess")
    snapshot["applied"] = _applied(
        applied_process(None, {"process": vendor_process}, old.get("unitPrice"))
    )
    snapshot["wasteItem"] = old.get("wasteItem")
    factors = []
    for item in old.get("factors") or []:
        value = _number(item.get("value"))
        if value is None or item.get("unit") not in _FACTOR_UNITS:
            continue
        try:
            label = app.registry.get(item["id"], None).label
        except EvidenceNotFound:
            label = item["id"]
        factors.append(
            {
                "key": None,
                "evidenceId": item["id"],
                "kind": str(item["id"]).split(":", 1)[0],
                "label": label,
                "value": value,
                "unit": item["unit"],
                "version": int(item.get("version") or 1),
                "asOf": item.get("asOf"),
                "source": label,
            }
        )
    if len(factors) == 2:
        snapshot["factors"] = {"baseline": factors[0], "applied": factors[1]}
    elif old.get("reason"):
        snapshot["reason"] = str(old["reason"])
    snapshot["notes"] = ["검수를 기록할 때 남긴 계산 기준(이전 형식)을 옮겼습니다."]
    if len(factors) == 2 and _number(old.get("perKg")) is not None and not old.get("reason"):
        # Preserve an already recorded historical calculation, not a new factor adoption.
        snapshot["perKg"] = round(factors[1]["value"] - factors[0]["value"], 9)
        snapshot["notes"].append(
            "과거 기록의 수치를 보존했습니다. 현재 원문 검증 기준을 충족했다는 뜻이 아니며, 새 계산과 구분해야 합니다."
        )
        return snapshot
    return _difference(snapshot)


def _factor_processes() -> dict[str, str]:
    """배출계수 근거 ID → 처리(incinerate·recycle 등). defaults의 refs와 가정값 ID로 거꾸로 찾는다."""
    out: dict[str, str] = {}
    for key in basis_keys.keys():  # noqa: SIM118 - 모듈 함수다(dict가 아니다)
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != "emission" or parts[1] == "transport":
            continue
        for evidence_id in basis_keys.candidates(key):
            out.setdefault(evidence_id, parts[1])
    return out


def reconstruct(app: Any, record: dict[str, Any]) -> dict[str, Any]:
    """기록 당시 기준이 없는 검수: 확정한 합의의 배정 계산에서 기준선·처리 방식·계수를 되살린다.

    합의 당시 계산을 지금 계수로 다시 냈을 때 값이 같아야 계수가 그때와 같다고 본다(RECONSTRUCTED).
    하나라도 확인하지 못하면 UNVERIFIED이고 탄소를 계산하지 않는다.
    """
    snapshot = _shell(app, record, 1, UNVERIFIED, "backfill")
    snapshot["notes"] = ["이 검수는 기록할 때의 탄소 계산 기준이 남아 있지 않았습니다."]

    def unverified(reason: str) -> dict[str, Any]:
        snapshot["perKg"] = None
        snapshot["reason"] = f"{reason} 당시 기준을 확인할 수 없어 탄소를 계산하지 않았습니다."
        return snapshot

    try:
        session = app.store.get_session(record["sessionId"])
    except NotFound:
        return unverified("합의 기록을 찾지 못해")
    assignment = next(
        (
            a
            for a in ((session.get("work") or {}).get("result") or {}).get("assignments") or []
            if a.get("streamId") == record["streamId"]
            and a.get("vendorOrgId") == record["vendorOrgId"]
        ),
        None,
    )
    if assignment is None:
        return unverified("합의한 배정에서 이 폐기물을 찾지 못해")
    cited = list((assignment.get("carbonProcess") or {}).get("evidence") or [])
    if not cited:
        return unverified("합의 당시 처리 방식 탄소 계산이 없어")
    try:
        calc = app.registry.get(cited[0], None)
        factors = [app.registry.get(x, None) for x in cited[1:3]]
    except EvidenceNotFound:
        return unverified("합의 당시 계산 근거가 지워져")
    if len(factors) == 1:  # 현행과 보낸 곳의 계수가 같으면 근거 ID가 하나만 남는다
        factors = [factors[0], factors[0]]
    if len(factors) != 2:
        return unverified("합의 당시 배출계수를 찾지 못해")
    deal_stream = next(
        (
            s
            for s in ((calc.meta or {}).get("inputs") or {}).get("args", {}).get("streams") or []
            if isinstance(s, dict) and s.get("id") == record["streamId"]
        ),
        None,
    )
    processes = _factor_processes()
    base_key, after_key = (
        processes.get(factor.meta.get("originalId", factor.id)) for factor in factors
    )
    now_f, after_f = _factor_of(factors[0], None), _factor_of(factors[1], None)
    deal_kg = _number((deal_stream or {}).get("monthlyKg"))
    if deal_kg is None or not (base_key and after_key and now_f and after_f):
        return unverified("합의 당시 계산의 입력을 읽지 못해")
    current = deal_stream.get("currentTreatment") or {}
    assumed = bool(assignment.get("currentAssumed")) or not current.get("method")
    assumed = assumed or bool(current.get("methodAssumed"))
    method = str(current.get("method") or "").strip()
    if current_costs.is_sale(current):
        method, assumed = current_costs.sale_method(method), False
    item = deal_stream.get("wasteItem")
    snapshot.update(
        baseline={
            "method": method or (DEFAULT_METHOD if assumed else _KEY_LABELS[base_key]),
            "assumed": assumed,
            "process": base_key,
            "source": "deal",
            "sourceLabel": BASELINE_SOURCES["deal_assumed" if assumed else "deal"],
            "evidence": list(current.get("evidence") or []),
        },
        applied={
            "process": process_label(assignment.get("process") or _KEY_LABELS[after_key]),
            "key": after_key,
            "source": "deal",
            "sourceLabel": APPLIED_SOURCES["deal"],
            "assumed": False,
        },
        wasteItem=item,
        deal={
            "sessionId": record["sessionId"],
            "calcEvidence": calc.id,
            "computedOn": calc.as_of,
            "monthlyKg": deal_kg,
            "carbonPerMonth": _number(calc.value),
        },
    )
    expected = round((after_f["value"] - now_f["value"]) / 1000 * deal_kg, 6)
    if _number(calc.value) is None or abs(expected - float(calc.value)) > 1e-6:
        return unverified("합의 뒤에 배출계수가 바뀌어 그때 쓴 계수 값을 확인할 수 없고,")
    snapshot["status"] = RECONSTRUCTED
    snapshot["factors"] = {
        "baseline": {**now_f, "key": f"emission.{base_key}.{item}"},
        "applied": {**after_f, "key": f"emission.{after_key}.{item}"},
    }
    snapshot["notes"].append(
        f"{calc.as_of}에 확정한 합의의 배정 계산에서 현행 처리·처리 방식·배출계수를 되살렸고, 지금 계수로 그 계산을 "
        "다시 내 같은 값임을 확인했습니다. 검수를 기록한 날의 자료가 아니라 합의 당시의 기준입니다."
    )
    return _difference(snapshot)


# ── 화면에 보이는 꼴 ─────────────────────────────────────────────────


def view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """실적 리포트에 싣는 탄소 기준. 사람이 읽는 값과 계수 근거 ID만 둔다."""
    factors = snapshot.get("factors") or {}
    deal = snapshot.get("deal") or None
    return {
        "status": snapshot["status"],
        "statusLabel": STATUS_LABELS[snapshot["status"]],
        # 고정한 기준 근거(배출 기업만 본다). 근거 패널에서 연다
        "evidenceId": snapshot.get("evidenceId"),
        "capturedOn": snapshot.get("capturedOn"),
        "capturedBy": snapshot.get("capturedBy"),
        "revision": snapshot.get("revision"),
        "baseline": {
            k: (snapshot.get("baseline") or {}).get(k)
            for k in ("method", "assumed", "source", "sourceLabel")
        },
        "applied": {
            k: (snapshot.get("applied") or {}).get(k)
            for k in ("process", "source", "sourceLabel", "assumed")
        },
        "factors": {
            role: (
                {
                    k: factor.get(k)
                    for k in ("evidenceId", "label", "value", "unit", "version", "asOf", "source")
                }
                if factor
                else None
            )
            for role, factor in (
                ("baseline", factors.get("baseline")),
                ("applied", factors.get("applied")),
            )
        },
        "perKg": snapshot.get("perKg"),
        "perKgUnit": FACTOR_UNIT,
        "scope": dict(snapshot.get("scope") or SCOPE),
        "reason": snapshot.get("reason"),
        "notes": list(snapshot.get("notes") or []),
        "deal": (
            {k: deal.get(k) for k in ("computedOn", "monthlyKg", "carbonPerMonth")}
            if deal
            else None
        ),
    }
