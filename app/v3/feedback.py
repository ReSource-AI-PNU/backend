"""재학습 데이터 수집 (7단계).

두 가지를 근거 레지스트리에 쌓는다. 따로 표를 두지 않고 근거의 meta["feedback"]에 기록을 싣기
때문에 메모리 레지스트리와 SQL 레지스트리에서 똑같이 돈다.

- 교정(correction): 배출 기업 담당자가 고친 박스·재질·출처 등. human 근거로 등록하고
  탐지 모델 재학습 데이터 후보로 내보낸다. 흐름의 값 자체는 바꾸지 않는다(흐름은
  PUT /orgs/{orgId}/streams/{streamId}로 고친다).
- 실제 결과(outcome): 확정 뒤에 업체가 기록한 실제 등급·단가·중량. 소유는 업체이고 배출 기업과
  나눈다. 등급 모델 학습 행과 기준값 보정 후보가 된다. 검수(계량) 한 건마다 검수 ID가 있다.
  새 반입 등록(record_outcome)은 업체가 붙인 요청 번호(requestId)로 검수 ID를 만들어 재전송을
  한 건으로 묶고, 정정(correct_outcome)은 앞 판을 덮지 않고 새 판과 새 근거를 남긴다. 실적·재학습은
  검수 건마다 최신 판 하나만 쓴다. 2026-11-19 전에는 흐름과 검수일로 키를 만들어 같은 날 두 번 받은
  반입이 한 건으로 덮였다. 그때 만든 기록은 그 키를 검수 ID로 읽어 그대로 이어 쓴다(legacy).


여기서 만드는 숫자는 실제 단가와 기준표 단가의 차이 하나뿐이고, calc 근거로 남긴다.
평균을 내거나 기준값을 자동으로 바꾸지 않는다. 모델 버전 표는 등록된 실행만 보여 준다.

기록마다 출처(dataOrigin)를 남긴다. 당사자 조직 가운데 하나라도 시연(가상) 회사면 demo, 모두 실제 회사면
real이다(기록할 때 정한다). demo 기록은 시연 데이터 수집을 보이는 데에만 쓰고, 실제 학습 행·검증·배포
게이트·기준값 보정 후보에는 넣지 않는다. 출처 표시가 생기기 전의 기록은 당사자 조직으로 다시 정하고, 조직을
찾지 못하면 unknown으로 두어 실제로 세지 않는다.
"""

from __future__ import annotations

import contextvars
import json
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from app.v3 import ids, outcome_carbon
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import is_feedback
from app.v3.intake import grade_model
from app.v3.judge.common import quality
from app.v3.store import NotFound
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
DEMO_NOTE = "시연(가상) 회사 사이의 기록입니다. 실제 학습·검증·배포 게이트에는 세지 않습니다."
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MISSING = object()
SEOUL = timezone(timedelta(hours=9), "Asia/Seoul")

# 실제 결과(검수 한 건)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")
TICKET_MAX = 40
REASON_MAX = 200
VALUE_KEYS = (
    "inspectedOn",
    "actualGrade",
    "actualUnitPriceKrwPerKg",
    "actualKg",
    "note",
    "weighTicket",
    "documentName",
    # 업체가 검수 기록에 적은 실제 처리 방식(재활용·열분해·소각·매립). 적지 않았으면 None
    "actualProcess",
)
# 처음 기록할 때 고정하고 정정해도 바꾸지 않는 값(검수 때의 품질, 합의 단가, 처음 기록한 때)
CARRIED_KEYS = ("features", "criterionId", "tablePrice", "offeredPrice", "createdOn", "createdAt")
HISTORY_KEYS = (
    "revision",
    "reason",
    "recordedOn",
    "recordedAt",
    *VALUE_KEYS,
    "evidenceKind",
    "evidence",
    "deviation",
    "appliedProcess",
)


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


def _demo_org(app: Any, org_id: str) -> bool | None:
    """시연(가상) 회사인지. 조직을 찾지 못하면 None."""
    try:
        org = app.store.get_org(org_id)
    except NotFound:
        return None
    return org.get("demo") is True or org.get("synthetic") is True


def data_origin(app: Any, *org_ids: str | None) -> str:
    """기록 당사자 조직으로 정한 출처: demo(하나라도 시연 회사) | real(모두 실제 회사) | unknown."""
    flags = [_demo_org(app, org_id) for org_id in org_ids if org_id]
    if any(flag is True for flag in flags):
        return "demo"
    if not flags or any(flag is None for flag in flags):
        return "unknown"
    return "real"


def _with_origin(app: Any, record: dict[str, Any]) -> dict[str, Any]:
    """기록에 남은 출처. 출처 표시가 생기기 전의 기록은 당사자 조직으로 정한다(값은 바꾸지 않는다)."""
    if record.get("dataOrigin") in ("real", "demo"):
        return record
    if record.get("type") == "outcome":
        parties = (record.get("vendorOrgId"), record.get("emitterOrgId"))
    else:
        parties = (record.get("orgId"),)
    return {**record, "dataOrigin": data_origin(app, *parties)}


# 읽기 요청 하나 안에서 같은 피드백 목록을 다시 읽지 않게 담아 두는 곳(reuse()를 연 동안만 있다).
# 진단 뷰 하나가 계약 검토마다 검수 기록을 다시 읽어, 담아 두지 않으면 같은 조회를 대여섯 번 한다
_reused: contextvars.ContextVar[dict[str, tuple[Any, list[Evidence]]] | None] = (
    contextvars.ContextVar("v3_feedback_reuse", default=None)
)


@contextmanager
def reuse() -> Iterator[None]:
    """이 안에서는 피드백 목록을 종류마다 한 번만 읽는다. 피드백을 쓰지 않는 읽기 요청에만 연다.

    이미 열려 있으면 바깥 것을 그대로 쓴다.
    """
    if _reused.get() is not None:
        yield
        return
    token = _reused.set({})
    try:
        yield
    finally:
        _reused.reset(token)


def _feedback(app: Any, kind: str) -> list[Evidence]:
    """meta["feedback"]["type"]이 kind인 근거 전부(시스템 조회, ID 순서)."""
    cache = _reused.get()
    held = cache.get(kind) if cache is not None else None
    if held is not None and held[0] is app.registry:
        return list(held[1])
    listed = getattr(app.registry, "list_feedback", None)
    found = listed(kind) if listed is not None else app.registry.list(org_id=None)
    items = [item for item in found if is_feedback(item, kind)]
    if cache is not None:
        cache[kind] = (app.registry, items)
    return list(items)


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
        "dataOrigin": data_origin(app, org_id),
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


def corrections(
    app: Any, session_id: str | None = None, origin: str | None = None
) -> list[dict[str, Any]]:
    """교정 기록. origin을 주면 그 출처(real | demo)의 것만."""
    found = [
        _with_origin(app, dict(item.meta["feedback"])) for item in _feedback(app, "correction")
    ]
    return [
        r
        for r in found
        if (session_id is None or r["sessionId"] == session_id)
        and (origin is None or r["dataOrigin"] == origin)
    ]


def export_detection_corrections(app: Any, origin: str = "real") -> list[dict[str, Any]]:
    """탐지 모델 재학습 데이터 후보. 박스·재질·출처·형태·혼입물을 사람이 고친 기록이다.

    사진 근거 ID(photoEvidence)로 원본 이미지를 찾는다. 사진이 없는 흐름의 교정도 그대로 싣고
    hasPhoto로 표시한다(학습 작업이 걸러 쓴다). 기본은 실제 회사의 교정만이고, origin="demo"면 시연 교정이다.
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
            "dataOrigin": record["dataOrigin"],
        }
        for record in corrections(app, origin=origin)
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


def _request_id(body: dict[str, Any]) -> str:
    value = body.get("requestId")
    text = value.strip() if isinstance(value, str) else ""
    if not _REQUEST_ID.match(text):
        raise FeedbackError(
            "INVALID_REQUEST_ID",
            "등록 요청 번호(requestId)를 영문·숫자·_.:- 8~100자로 보내 주세요. "
            "같은 요청을 다시 보낼 때는 같은 번호를 씁니다.",
        )
    return text


def _values(app: Any, body: dict[str, Any]) -> dict[str, Any]:
    """검수 한 건의 값. 새 반입 등록과 정정이 같은 규칙으로 읽는다."""
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
    ticket = body.get("weighTicket")
    if ticket is not None and not isinstance(ticket, str):
        raise FeedbackError("INVALID_VALUE", "계량표 번호(weighTicket)는 글자로 보내 주세요.")
    ticket = (ticket or "").strip()
    if len(ticket) > TICKET_MAX:
        raise FeedbackError("INVALID_VALUE", f"계량표 번호는 {TICKET_MAX}자까지 적을 수 있습니다.")
    document = body.get("documentName")
    document = document.strip() if isinstance(document, str) else ""
    process = body.get("actualProcess")
    if process in ("", None):
        process = None
    elif process not in outcome_carbon.PROCESS_CHOICES:
        raise FeedbackError(
            "INVALID_VALUE",
            f"실제 처리 방식은 {', '.join(outcome_carbon.PROCESS_CHOICES)} 가운데 하나입니다.",
        )
    return {
        "inspectedOn": inspected,
        "actualGrade": grade,
        "actualUnitPriceKrwPerKg": _number(price),
        "actualKg": _number(kg),
        "note": _note(body),
        "weighTicket": ticket or None,
        "documentName": document or None,
        "actualProcess": process,
    }


def _stored(record: dict[str, Any]) -> dict[str, Any]:
    """저장한 판의 값(_values와 같은 모양). 재전송과 정정 전후를 견줄 때 쓴다."""
    return {key: record.get(key) for key in VALUE_KEYS}


def _now(app: Any) -> str:
    """기록한 시각. 앱에 주입한 시계(App.wall_clock)만 읽는다(규칙 12, 시험에서 바꿔 끼운다)."""
    return datetime.fromtimestamp(app.wall_clock(), SEOUL).isoformat(timespec="milliseconds")


def inspection_key(vendor: str, session_id: str, request_id: str) -> str:
    """새 반입 등록의 검수 ID. 같은 업체가 같은 진단에 같은 요청 번호로 보내면 같은 ID다."""
    return "insp-" + ids.digest({"vendor": vendor, "session": session_id, "request": request_id})


def _evidence_key(inspection_id: str, revision: int) -> str:
    # 검수일로 키를 만든 이전 기록(실제결과-…)은 그 키 뒤에 판 번호를 붙인다
    base = inspection_id if inspection_id.startswith("실제결과-") else f"검수-{inspection_id}"
    return f"{base}-r{revision}"


def _revisions(app: Any) -> dict[tuple[str, str], dict[int, dict[str, Any]]]:
    """(업체, 검수 ID) → {판 번호: 기록}. 등급·단가·중량 근거에 같은 기록이 실려 있어 하나로 묶는다."""
    found: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for item in _feedback(app, "outcome"):
        record = {k: v for k, v in item.meta["feedback"].items() if k != "part"}
        if "inspectionId" not in record:  # 검수일로 키를 만든 이전 기록: 그 키를 검수 ID로 읽는다
            record.update(
                inspectionId=record["id"],
                revision=1,
                legacy=True,
                requestId=None,
                supersedes=None,
                reason=None,
                weighTicket=None,
                documentName=None,
                createdOn=record.get("recordedOn"),
                createdAt=None,
                recordedAt=None,
            )
        key = (record["vendorOrgId"], record["inspectionId"])
        found.setdefault(key, {})[int(record["revision"])] = record
    return found


def _with_history(revisions: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """최신 판 하나에 앞 판들(정정 이력)을 붙인다."""
    last = max(revisions)
    history = [
        {key: revisions[n].get(key) for key in HISTORY_KEYS} for n in sorted(revisions) if n != last
    ]
    return {**revisions[last], "history": history}


def _inspection(app: Any, inspection_id: str) -> dict[int, dict[str, Any]] | None:
    for (_, found), revisions in _revisions(app).items():
        if found == inspection_id:
            return revisions
    return None


def find_outcome(app: Any, inspection_id: str) -> dict[str, Any] | None:
    """검수 ID로 최신 판(정정 이력 포함)을 찾는다. 권한은 service가 확인한다."""
    revisions = _inspection(app, inspection_id)
    return _with_history(revisions) if revisions else None


def _check_ticket(app: Any, vendor: str, values: dict[str, Any], own: str | None) -> None:
    """같은 업체가 같은 날 같은 계량표 번호로 다른 검수를 이미 기록했으면 막는다(손으로 두 번 적은 경우)."""
    ticket = values["weighTicket"]
    if not ticket:
        return
    for (org, inspection_id), revisions in _revisions(app).items():
        latest = revisions[max(revisions)]
        if org != vendor or inspection_id == own:
            continue
        if latest.get("weighTicket") == ticket and latest["inspectedOn"] == values["inspectedOn"]:
            raise FeedbackError(
                "DUPLICATE_TICKET",
                f"같은 날({values['inspectedOn']}) 같은 계량표 번호({ticket})로 기록한 검수가 "
                "이미 있습니다. 그 건을 고치려면 표에서 정정을 눌러 주세요.",
                409,
            )


def _write_revision(
    app: Any,
    session: dict[str, Any],
    thread: dict[str, Any],
    stream: dict[str, Any],
    values: dict[str, Any],
    *,
    inspection_id: str,
    revision: int,
    request_id: str,
    reason: str | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """검수 건의 한 판을 근거로 남긴다. 앞 판의 근거는 그대로 두고 새 ID로 쓴다."""
    vendor, emitter = thread["vendorOrgId"], thread["emitterOrgId"]
    stream_id = stream["id"]
    name = stream.get("name") or stream_id
    document = values["documentName"] or ""
    inspected = values["inspectedOn"]
    grade = values["actualGrade"]
    price = values["actualUnitPriceKrwPerKg"]
    kg = values["actualKg"]
    key = _evidence_key(inspection_id, revision)
    now = _now(app)

    def evidence_id(part: str) -> str:
        if document:
            return ids.doc_id(vendor, document, f"{key}-{part}")
        return ids.human_id(vendor, f"{key}-{part}")

    if previous is None:
        # 검수 때의 품질과 합의 단가는 처음 기록할 때 고정하고, 정정은 값만 바꾼다
        q = quality(stream)
        agreed = _agreed(session, thread["id"], stream_id)
        carried = {
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
            "createdOn": app.today,
            "createdAt": now,
        }
    else:
        carried = {key_: previous.get(key_) for key_ in CARRIED_KEYS}
    if values["actualProcess"] is None and previous is not None:
        # 정정에서 처리 방식을 적지 않으면 앞 판의 것을 잇는다(오늘의 업체 정보로 바꾸지 않는다)
        applied = dict(
            previous.get("appliedProcess") or {"value": None, "source": "unknown", "assumed": False}
        )
    else:
        try:
            profile = app.store.get_vendor_profile(vendor)
        except NotFound:  # 업체 정보가 없으면 처리 방식을 모르는 것으로 둔다
            profile = {}
        applied = outcome_carbon.applied_process(values["actualProcess"], profile, price)
    parts = {
        "grade": evidence_id("등급") if grade is not None else None,
        "unitPrice": evidence_id("단가") if price is not None else None,
        "kg": evidence_id("중량") if kg is not None else None,
    }
    record = {
        "type": "outcome",
        "id": inspection_id,
        "inspectionId": inspection_id,
        "revision": revision,
        "supersedes": previous["revision"] if previous else None,
        "requestId": request_id,
        "reason": reason,
        "legacy": bool(previous and previous.get("legacy")),
        "sessionId": session["id"],
        "threadId": thread["id"],
        "vendorOrgId": vendor,
        "emitterOrgId": emitter,
        "streamId": stream_id,
        **values,
        "evidenceKind": "doc" if document else "human",
        "evidence": {k: v for k, v in parts.items() if v},
        # 탄소 계산에 쓴 처리 방식과 그 출처(검수 기록에 적음, 기록 당시 업체 정보 등)
        "appliedProcess": applied,
        **carried,
        "deviation": None,
        "recordedOn": app.today,
        "recordedAt": now,
        "dataOrigin": data_origin(app, vendor, emitter),
    }
    if previous is None:
        source = (
            f"검수 문서 {document}(업체 제출)"
            if document
            else "업체 담당자 입력(확정 뒤 실제 결과)"
        )
        tail = ""
    else:
        source = (
            f"검수 문서 {document}(업체 정정 제출)"
            if document
            else "업체 담당자 정정(확정 뒤 실제 결과)"
        )
        tail = f" ({previous['revision']}판을 정정한 {revision}판. 사유: {reason})"

    def put(part: str, label: str, value: Any, unit: str | None, text: str) -> None:
        app.registry.put(
            Evidence(
                id=parts[part],
                kind="doc" if document else "human",
                label=f"{name} {label}",
                value=value,
                unit=unit,
                excerpt=f"{inspected} 검수: {name} {text}{tail}",
                source=source,
                as_of=inspected,
                access=(vendor, emitter),
                meta={"ownerOrgId": vendor, "feedback": {**record, "part": part}},
            )
        )

    table = record["tablePrice"]
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
    # 탄소 계산 기준(현행 처리·처리 방식·배출계수)을 고정한다. 처음 기록이면 지금 고정하고, 정정이면 처리 방식이
    # 바뀐 판만 새로 고정한다. 배출 기업만 보는 근거라 이 기록에는 싣지 않는다
    outcome_carbon.on_write(app, record, stream, previous)
    app.store.add_event(
        session["id"],
        "system",
        "feedback",
        (
            f"업체가 {name}의 {inspected} 검수 결과를 기록했습니다."
            if previous is None
            else f"업체가 {name}의 {inspected} 검수 기록을 정정했습니다({revision}판)."
        ),
        thread_id=thread["id"],
        detail={
            "orgId": vendor,
            "type": "outcome",
            "streamId": stream_id,
            "inspectionId": inspection_id,
            "revision": revision,
            "evidence": list(record["evidence"].values()),
        },
    )
    return record


def record_outcome(
    app: Any,
    session: dict[str, Any],
    thread: dict[str, Any],
    stream: dict[str, Any],
    body: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """새 반입 검수 한 건을 등록한다. 권한과 스레드 상태는 service가 확인한다.

    같은 날 같은 폐기물을 여러 번 받아도 요청 번호(requestId)가 다르면 건마다 따로 남는다. 같은 요청
    번호로 다시 오면(재시도·두 번 누름) 새로 만들지 않고 이미 만든 건의 최신 판을 돌려준다. 번호가 없으면
    (2026-11-19 전 화면) 서버가 만든 번호로 새 반입 한 건이 된다.
    (기록, 새로 만들었는지)를 돌려준다. documentName을 주면 doc 근거(검수 문서), 없으면 human 근거다.
    """
    if body.get("inspectionId") not in (None, ""):
        raise FeedbackError(
            "USE_CORRECTION",
            "이미 기록한 검수를 고치려면 정정(PUT /sessions/{sessionId}/outcomes/{inspectionId})으로 "
            "보내 주세요. 새 반입이면 inspectionId 없이 보냅니다.",
        )
    values = _values(app, body)
    vendor = thread["vendorOrgId"]
    if body.get("requestId") in (None, ""):
        # 하위 호환: requestId를 보내지 않는 예전 화면·API 호출은 서버가 번호를 만들어 새 반입 한 건으로 받는다.
        # 번호가 매번 달라 재전송을 한 건으로 묶지 못하므로 화면은 늘 보낸다. 같은 날 같은 값이라도 기존 기록을
        # 고치지 않는다. 시계는 앱에 주입한 것만 읽고(규칙 12) 지금까지의 검수 수를 섞어 같은 시각에도 겹치지 않게 한다
        request_id = "auto-" + ids.digest(
            {
                "thread": thread["id"],
                "stream": stream["id"],
                "values": values,
                "at": _now(app),
                "count": len(_revisions(app)),
            }
        )
    else:
        request_id = _request_id(body)
    inspection_id = inspection_key(vendor, session["id"], request_id)
    revisions = _inspection(app, inspection_id)
    if revisions:
        first = revisions[min(revisions)]
        same = (
            first["threadId"] == thread["id"]
            and first["streamId"] == stream["id"]
            and _stored(first) == values
        )
        if not same:
            raise FeedbackError(
                "REQUEST_ID_REUSED",
                "이 등록 요청 번호로 다른 내용이 이미 기록되어 있습니다. 새 반입이면 새 번호로 보내 주세요.",
                409,
            )
        return _with_history(revisions), False
    _check_ticket(app, vendor, values, None)
    record = _write_revision(
        app,
        session,
        thread,
        stream,
        values,
        inspection_id=inspection_id,
        revision=1,
        request_id=request_id,
        reason=None,
        previous=None,
    )
    return {**record, "history": []}, True


def correct_outcome(
    app: Any,
    session: dict[str, Any],
    thread: dict[str, Any],
    stream: dict[str, Any],
    inspection_id: str,
    body: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """이미 기록한 검수 건을 정정한다. 앞 판을 덮지 않고 새 판을 만든다. 권한은 service가 확인한다.

    baseRevision은 담당자가 보고 고친 판 번호다. 그 사이 다른 정정이 들어왔으면 409로 알린다. 같은
    요청 번호로 다시 오면 이미 만든 판을 돌려준다. (기록, 새로 만들었는지)를 돌려준다.
    """
    revisions = _inspection(app, inspection_id)
    if not revisions:
        raise FeedbackError("OUTCOME_NOT_FOUND", "그런 검수 기록이 없습니다.", 404)
    latest = revisions[max(revisions)]
    request_id = _request_id(body)
    reason = body.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise FeedbackError("INVALID_VALUE", "정정 사유(reason)를 적어 주세요.")
    if len(reason) > REASON_MAX:
        raise FeedbackError("INVALID_VALUE", f"정정 사유는 {REASON_MAX}자까지 적을 수 있습니다.")
    values = _values(app, body)
    for record in revisions.values():
        if record.get("requestId") != request_id:
            continue
        if record["revision"] > 1 and _stored(record) == values and record["reason"] == reason:
            return _with_history(revisions), False
        raise FeedbackError(
            "REQUEST_ID_REUSED",
            "이 요청 번호로 다른 내용이 이미 기록되어 있습니다. 새 정정이면 새 번호로 보내 주세요.",
            409,
        )
    base = body.get("baseRevision")
    if isinstance(base, bool) or not isinstance(base, int):
        raise FeedbackError("INVALID_VALUE", "고친 판 번호(baseRevision)를 정수로 보내 주세요.")
    if base != latest["revision"]:
        raise FeedbackError(
            "REVISION_CONFLICT",
            f"그 사이 이 검수 기록이 정정되었습니다(지금 {latest['revision']}판). "
            "화면을 새로 불러온 뒤 다시 정정해 주세요.",
            409,
        )
    if _stored(latest) == values:
        raise FeedbackError("NO_CHANGE", "정정하기 전과 값이 같습니다.")
    _check_ticket(app, thread["vendorOrgId"], values, inspection_id)
    _write_revision(
        app,
        session,
        thread,
        stream,
        values,
        inspection_id=inspection_id,
        revision=latest["revision"] + 1,
        request_id=request_id,
        reason=reason,
        previous=latest,
    )
    return _with_history(_inspection(app, inspection_id) or {}), True


def outcomes(
    app: Any, session_id: str | None = None, origin: str | None = None
) -> list[dict[str, Any]]:
    """실제 결과 기록. 검수 건마다 최신 판 하나이고 앞 판은 history에 싣는다.

    검수일 → 처음 기록한 시각 → 검수 ID 순서다. origin을 주면 그 출처(real | demo)의 것만.
    """
    found = []
    for revisions in _revisions(app).values():
        record = _with_origin(app, _with_history(revisions))
        if session_id is not None and record["sessionId"] != session_id:
            continue
        if origin is not None and record["dataOrigin"] != origin:
            continue
        found.append(record)
    return sorted(
        found,
        key=lambda r: (r["inspectedOn"], r.get("createdAt") or "", r["vendorOrgId"], r["id"]),
    )


def export_grade_labels(app: Any, origin: str = "real") -> list[dict[str, Any]]:
    """등급 모델 학습 행. 실제 등급이 있고 재질·오염도·수분이 모두 있는 기록만 행이 된다.

    합성 데이터(make_synthetic_dataset)와 같은 열 이름을 쓴다. 기본은 실제 회사의 기록(synthetic=False)만이고,
    origin="demo"면 시연(가상) 회사의 기록을 synthetic=True, data_origin="demo"로 낸다(학습에 쓰지 않는다).
    """
    version = grade_model.thresholds()["version"]
    rows = []
    for record in outcomes(app, origin=origin):
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
                "synthetic": origin != "real",
                "data_origin": origin,
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
        # 시연(가상) 회사 사이의 등급 행. 게이트 판정과 평가에는 넣지 않고 따로 센다
        "demoLabelCount": len(export_grade_labels(app, "demo")),
        "evaluated": {
            "model": f"등급 기준값 규칙 {grade_model.thresholds()['version']}",
            "n": metrics["n"],
            "accuracy": metrics["accuracy"],
            "label": metrics["label"],
        },
    }


def calibration_candidates(app: Any, viewer: str | None = None) -> dict[str, Any]:
    """기준값 보정 후보. 실제 단가와 매입 기준표 단가의 차이를 흐름·업체별로 건별로 늘어놓는다.

    viewer를 주면 그 조직이 당사자인 기록만 준다(None이면 전부). 실제 회사의 기록만 쓴다(시연 기록은 보정 후보가 아니다).
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in outcomes(app, origin="real"):
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
                "inspectionId": record["inspectionId"],
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
    recorded = outcomes(app, origin="real")
    labelled = [r for r in recorded if r["actualGrade"] is not None]
    usable = export_grade_labels(app)
    fixed = corrections(app, origin="real")
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
        # 시연(가상) 회사 사이에서 쌓인 기록. 수집 과정을 보이려고 세기만 하고 위 표와 게이트에는 넣지 않는다
        "demo": {
            "outcomes": len(outcomes(app, origin="demo")),
            "gradeLabels": len(export_grade_labels(app, "demo")),
            "corrections": len(corrections(app, origin="demo")),
            "note": DEMO_NOTE,
        },
    }
