"""회사별 기록(개인화 메모리): 지난 찾기와 거래에서 남은 기록을 회사 하나 기준으로 묶는다.

2026-11-22 사용자 요청("각 기업마다 특성이 너무 다르니, 전체 루프에서 남는 기록으로 개인화 메모리를 만들어 서비스
품질을 높일 수 있나")에 답한다. 새로 저장하는 값은 없다. 이미 남은 기록을 읽어 묶기만 한다.

- 답변(answers): 지난 찾기에서 담당자가 질문에 답한 것. 답은 폐기물에 바로 적혀 다음 찾기에서 다시 묻지 않는다.
  '모름'으로 답한 항목도 다시 묻지 않으므로, 자료 화면에서 채울 수 있게 따로 보인다.
- 검수(inspections): 확정 뒤 업체가 기록한 실제 결과(feedback.outcomes)를 합의 때의 등급·단가와 견준다.
  합의 등급은 합의 때 고른 매입 기준(criterionId)의 등급, 합의 단가는 업체 제안 단가(없으면 기준표 단가)다.
- 교정(corrections): 담당자가 AI 판정을 고친 기록.
- 거래(deals): 함께 확정한 업체와 그 횟수.

원칙(feedback.py와 같다)
- 평균을 내거나 기준값·판정 숫자를 바꾸지 않는다. 건별 사실과 그 근거 ID만 묶는다.
- 기록이 MIN_RECORDS건보다 적으면 "참고"로만 보이고 판단에 쓰지 않는다.
- 실제 회사는 실제 기록(dataOrigin real)만, 시연 회사는 시연 기록만 본다(서로 섞지 않는다).
- 계약 검토(agents/reviewer.py)는 같은 두 회사 사이의 검수만 쓴다. 검토는 양쪽이 함께 보므로, 다른 업체와의
  검수 이력을 상대 업체에게 드러내지 않는다.
- 회사가 끄면(org.memory.enabled = False) 계약 검토에 쓰지 않는다. 기록 자체는 지우지 않는다(업체 소유 기록도 있다).

판정 반영(개인화, 2026-11-22): 배출 기업 담당자가 승인하면(org.memory.applied) 최적 배정이 회사별 보정(judge/personal.py)으로
업체마다 예상 단가를 맞춰 고른다. 승인은 human 근거로 남고 그 값이 보정 강도다(코드에 상수를 두지 않는다: 승인 기록 없는
가정값을 판정에 쓰지 않는 규칙). 회사 측 AI는 같은 업체와의 지난 검수 요약(숫자 없이)을 대화 맥락으로 받는다.
"""

from __future__ import annotations

from typing import Any

from app.v3 import feedback, ids
from app.v3.agents.questions import FIELDS as QUESTION_FIELDS
from app.v3.evidence.model import Evidence
from app.v3.intake import grade_model
from app.v3.judge import personal
from app.v3.store import NotFound
from app.v3.untrusted import stream_label

# 이 건수부터 판단(계약 검토)에 쓴다. 적은 표본으로 판단을 바꾸지 않는다(feedback.CALIBRATION_NOTE)
MIN_RECORDS = 3
# 판정 반영을 승인할 때 화면이 제안하는 보정 강도(계약 단가 쪽을 기록 몇 건 몫으로 믿는가). 담당자가 승인한 값만
# 근거(human)가 되어 판정에 쓰인다. 기록이 이만큼 쌓이면 회사 기록과 계약 단가를 반반 믿는다
PROPOSED_STRENGTH = 3
_EPS = 1e-9


def enabled(org: dict[str, Any]) -> bool:
    """회사가 기록 사용을 껐는지. 정하지 않았으면 켠 것으로 본다."""
    return (org.get("memory") or {}).get("enabled") is not False


def _grade_rank(grade: Any) -> int | None:
    return grade_model.GRADES.index(grade) if grade in grade_model.GRADES else None


def _agreed_grade(app: Any, record: dict[str, Any], cache: dict[str, dict[str, Any]]) -> Any:
    """합의 때 고른 매입 기준의 등급. 기준이나 업체 정보를 찾지 못하면 None."""
    criterion_id = record.get("criterionId")
    vendor = record.get("vendorOrgId")
    if not criterion_id or not vendor:
        return None
    if vendor not in cache:
        try:
            profile = app.store.get_vendor_profile(vendor)
        except NotFound:
            profile = {}
        cache[vendor] = {str(c.get("id")): c for c in profile.get("criteria") or [] if c.get("id")}
    criterion = cache[vendor].get(str(criterion_id)) or {}
    return criterion.get("grade")


def _price_claim(claim_id: str, value: float, evidence: list[str], label: str) -> dict[str, Any]:
    """단가 claim(계약 7장 모양). 받는 돈은 매입 단가, 내는 돈(음수)은 처리비로 이름 붙인다."""
    return {
        "id": claim_id,
        "kind": "number",
        "value": value,
        "unit": "원/kg",
        "evidence": [str(e) for e in evidence],
        "label": label,
        "role": "unitPrice" if value >= 0 else "treatmentFee",
    }


def _agreed_price(record: dict[str, Any]) -> dict[str, Any] | None:
    """합의 단가 claim: 업체 제안 단가, 없으면 매입 기준표 단가."""
    for key in ("offeredPrice", "tablePrice"):
        claim = record.get(key)
        if isinstance(claim, dict) and isinstance(claim.get("value"), (int, float)):
            return _price_claim(
                "agreedUnitPrice",
                float(claim["value"]),
                list(claim.get("evidence") or []),
                "합의 단가" if key == "offeredPrice" else "합의 단가(매입 기준표)",
            )
    return None


def _compare(actual: Any, agreed: Any, *, grade: bool) -> str | None:
    """lower(합의보다 나쁨) | same | higher(합의보다 좋음) | None(견줄 수 없음)."""
    if grade:
        a, b = _grade_rank(actual), _grade_rank(agreed)
        if a is None or b is None:
            return None
        return "lower" if a > b else ("higher" if a < b else "same")
    if not isinstance(actual, (int, float)) or not isinstance(agreed, (int, float)):
        return None
    # 단가는 받는 돈이 +, 내는 처리비가 −다. 작을수록 배출 기업에 나쁘다
    if actual < agreed - _EPS:
        return "lower"
    return "higher" if actual > agreed + _EPS else "same"


def inspection_rows(
    app: Any,
    emitter_id: str | None,
    *,
    vendor_id: str | None = None,
    stream_ids: set[str] | None = None,
    exclude_session: str | None = None,
) -> list[dict[str, Any]]:
    """배출 기업의 검수 한 건마다 합의와 견준 줄. 검수일 순서다.

    vendor_id를 주면 그 업체와의 검수만, stream_ids를 주면 그 폐기물만, exclude_session을 주면 그 찾기의 검수는 뺀다.
    emitter_id가 None이면 vendor_id의 모든 배출 기업과의 검수다(업체 쪽 회사 기록). 출처는 기준 조직(배출 기업, 없으면 업체)으로 정한다.
    """
    origin = feedback.data_origin(app, emitter_id or vendor_id)
    origin = origin if origin in ("real", "demo") else "real"
    cache: dict[str, dict[str, Any]] = {}
    rows = []
    for record in feedback.outcomes(app, origin=origin):
        if emitter_id is not None and record.get("emitterOrgId") != emitter_id:
            continue
        if vendor_id is not None and record.get("vendorOrgId") != vendor_id:
            continue
        if stream_ids is not None and record.get("streamId") not in stream_ids:
            continue
        if exclude_session is not None and record.get("sessionId") == exclude_session:
            continue
        agreed_grade = _agreed_grade(app, record, cache)
        agreed_price = _agreed_price(record)
        evidence = record.get("evidence") or {}
        rows.append(
            {
                "inspectionId": record["inspectionId"],
                "inspectedOn": record["inspectedOn"],
                "sessionId": record["sessionId"],
                "streamId": record["streamId"],
                "vendorOrgId": record["vendorOrgId"],
                "emitterOrgId": record["emitterOrgId"],
                "agreedGrade": agreed_grade,
                "actualGrade": record.get("actualGrade"),
                "grade": _compare(record.get("actualGrade"), agreed_grade, grade=True),
                "agreedPrice": agreed_price,
                "actualPrice": None
                if record.get("actualUnitPriceKrwPerKg") is None
                else _price_claim(
                    "actualUnitPrice",
                    float(record["actualUnitPriceKrwPerKg"]),
                    [evidence["unitPrice"]] if evidence.get("unitPrice") else [],
                    "실제 단가(검수)",
                ),
                "price": _compare(
                    record.get("actualUnitPriceKrwPerKg"),
                    (agreed_price or {}).get("value"),
                    grade=False,
                ),
                "evidence": list(evidence.values()),
                "dataOrigin": record["dataOrigin"],
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """검수 줄 묶음의 요약. 셈만 하고 평균은 내지 않는다.

    status: none(기록 없음) | few(MIN_RECORDS건 미만, 참고만) | as_agreed(합의대로) | below(합의보다 낮게 나온 건이 절반 이상)
    """
    grade_lower = sum(1 for r in rows if r["grade"] == "lower")
    price_lower = sum(1 for r in rows if r["price"] == "lower")
    below = sum(1 for r in rows if "lower" in (r["grade"], r["price"]))
    if not rows:
        status = "none"
    elif len(rows) < MIN_RECORDS:
        status = "few"
    elif below * 2 >= len(rows):
        status = "below"
    else:
        status = "as_agreed"
    return {
        "count": len(rows),
        "gradeLower": grade_lower,
        "priceLower": price_lower,
        "below": below,
        "status": status,
        "minRecords": MIN_RECORDS,
        "evidence": [e for r in rows for e in r["evidence"]],
    }


def pair_history(
    app: Any, emitter_id: str, vendor_id: str, stream_ids: list[str], session_id: str
) -> dict[str, dict[str, Any]] | None:
    """계약 검토에 쓰는 같은 두 회사 사이의 지난 검수(폐기물마다 요약). 회사가 껐으면 None."""
    try:
        org = app.store.get_org(emitter_id)
    except NotFound:
        return None
    if not enabled(org):
        return None
    rows = inspection_rows(
        app,
        emitter_id,
        vendor_id=vendor_id,
        stream_ids=set(stream_ids),
        exclude_session=session_id,
    )
    return {sid: summarize([r for r in rows if r["streamId"] == sid]) for sid in stream_ids}


def _name(app: Any, org_id: str) -> str | None:
    try:
        return str(app.store.get_org(org_id).get("name") or "") or None
    except NotFound:
        return None


def _answers(app: Any, org_id: str, streams: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """지난 찾기의 질문 답. 폐기물·항목마다 가장 최근 답 하나."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    sessions = sorted(
        app.store.list_sessions(org_id),
        key=lambda s: (s.get("createdAt") or "", s["id"]),
    )
    for session in sessions:
        if session.get("emitterOrgId") != org_id:
            continue
        for question in app.store.list_questions(session["id"]):
            if not question.get("answered") or question.get("streamId") not in streams:
                continue
            field = question.get("field")
            spec = QUESTION_FIELDS.get(str(field))
            if spec is None:
                continue
            stream = streams[question["streamId"]]
            latest[(question["streamId"], str(field))] = {
                "streamId": question["streamId"],
                "field": field,
                "label": spec["label"],
                "answer": None if question.get("unknown") else question.get("answer"),
                "unit": spec.get("unit"),
                "unknown": bool(question.get("unknown")),
                # 모름으로 답한 뒤 자료 화면에서 채웠으면 더는 모름이 아니다
                "stillUnknown": field in (stream.get("unknownFields") or []),
                "answeredOn": session.get("today"),
                "sessionId": session["id"],
            }
    return sorted(latest.values(), key=lambda a: (a["streamId"], a["field"]))


def _corrections(app: Any, org_id: str, origin: str) -> list[dict[str, Any]]:
    found = [r for r in feedback.corrections(app, origin=origin) if r.get("orgId") == org_id]
    return [
        {
            "id": r["id"],
            "streamId": r["streamId"],
            "field": r["field"],
            "label": feedback.FIELD_LABELS.get(r["field"], r["field"]),
            "before": r["before"] if isinstance(r["before"], (str, int, float)) else None,
            "after": r["after"] if isinstance(r["after"], (str, int, float)) else None,
            "recordedOn": r["recordedOn"],
            "evidence": [r["id"]],
        }
        for r in sorted(found, key=lambda r: (r["recordedOn"], r["id"]))
    ]


def _deals(app: Any, org_id: str) -> list[dict[str, Any]]:
    by_vendor: dict[str, dict[str, Any]] = {}
    for thread in app.store.list_threads(org_id=org_id):
        if thread.get("status") != "confirmed" or thread.get("emitterOrgId") != org_id:
            continue
        vendor = thread.get("vendorOrgId")
        if not vendor:
            continue
        try:
            day = app.store.get_session(thread["sessionId"]).get("today")
        except (NotFound, KeyError):
            day = None
        entry = by_vendor.setdefault(
            vendor,
            {"vendorOrgId": vendor, "vendorName": _name(app, vendor), "count": 0, "lastOn": None},
        )
        entry["count"] += 1
        if day and (entry["lastOn"] is None or day > entry["lastOn"]):
            entry["lastOn"] = day
    return sorted(by_vendor.values(), key=lambda d: (-d["count"], d["vendorOrgId"]))


def org_memory(app: Any, org_id: str) -> dict[str, Any]:
    """배출 기업 하나의 기록 묶음(GET /orgs/{orgId}/memory). 권한은 service가 확인한다."""
    org = app.store.get_org(org_id)
    origin = feedback.data_origin(app, org_id)
    origin = origin if origin in ("real", "demo") else "real"
    streams = {s["id"]: s for s in app.store.list_streams(org_id)}
    rows = inspection_rows(app, org_id)
    names: dict[str, str | None] = {}
    for row in rows:
        vendor = row["vendorOrgId"]
        if vendor not in names:
            names[vendor] = _name(app, vendor)
        row["vendorName"] = names[vendor]
    answers = _answers(app, org_id, streams)
    corrections = _corrections(app, org_id, origin)
    per_stream = []
    for sid, stream in streams.items():
        mine = [r for r in rows if r["streamId"] == sid]
        fixed = [c for c in corrections if c["streamId"] == sid]
        said = [a for a in answers if a["streamId"] == sid]
        if not (mine or fixed or said):
            continue
        per_stream.append(
            {
                "streamId": sid,
                "name": stream_label(stream),
                "inspections": summarize(mine),
                "inspectionRows": mine,
                "answers": said,
                "corrections": fixed,
            }
        )
    deals = _deals(app, org_id)
    return {
        "orgId": org_id,
        "role": "emitter",
        "enabled": enabled(org),
        "dataOrigin": origin,
        "minRecords": MIN_RECORDS,
        "summary": {
            "streams": len(per_stream),
            "inspections": len(rows),
            "below": sum(1 for r in rows if "lower" in (r["grade"], r["price"])),
            "answers": len(answers),
            "stillUnknown": sum(1 for a in answers if a["stillUnknown"]),
            "corrections": len(corrections),
            "deals": sum(d["count"] for d in deals),
            "vendors": len(deals),
        },
        "streams": per_stream,
        "deals": deals,
        "calibration": _calibration(app, org, rows, names, streams),
    }


def vendor_memory(app: Any, vendor_id: str) -> dict[str, Any]:
    """처리 업체 하나의 기록 묶음(GET /orgs/{orgId}/memory, 업체). 자기가 기록한 검수와 함께 확정한 거래를 배출 기업별로 묶는다.

    업체가 당사자인 기록만 쓴다. 폐기물 이름은 상대에게 보이는 이름(stream_label)이다. 판정에는 쓰지 않고 보여 주기만 한다
    (업체 측 AI는 이 기록을 받지 않는다). 권한은 service가 확인한다.
    """
    origin = feedback.data_origin(app, vendor_id)
    origin = origin if origin in ("real", "demo") else "real"
    rows = inspection_rows(app, None, vendor_id=vendor_id)
    labels: dict[str, str] = {}
    for row in rows:
        sid = row["streamId"]
        if sid not in labels:
            try:
                labels[sid] = stream_label(app.store.get_stream(sid))
            except NotFound:
                labels[sid] = "폐기물"
        row["streamName"] = labels[sid]
    deals: dict[str, int] = {}
    for thread in app.store.list_threads(org_id=vendor_id):
        if thread.get("status") == "confirmed" and thread.get("vendorOrgId") == vendor_id:
            emitter = str(thread.get("emitterOrgId"))
            deals[emitter] = deals.get(emitter, 0) + 1
    emitters = []
    for emitter in sorted({*deals, *(r["emitterOrgId"] for r in rows)}):
        mine = [r for r in rows if r["emitterOrgId"] == emitter]
        emitters.append(
            {
                "emitterOrgId": emitter,
                "emitterName": _name(app, emitter),
                "deals": deals.get(emitter, 0),
                "inspections": summarize(mine),
                "inspectionRows": mine,
            }
        )
    return {
        "orgId": vendor_id,
        "role": "vendor",
        "dataOrigin": origin,
        "minRecords": MIN_RECORDS,
        "summary": {
            "emitters": len(emitters),
            "inspections": len(rows),
            "below": sum(1 for r in rows if "lower" in (r["grade"], r["price"])),
            "deals": sum(deals.values()),
        },
        "emitters": emitters,
    }


def applied(org: dict[str, Any]) -> dict[str, Any] | None:
    """판정 반영 승인(켜져 있을 때만). {evidence, strength, approvedOn}."""
    memo = org.get("memory") or {}
    found = memo.get("applied")
    if not enabled(org) or not isinstance(found, dict) or not found.get("evidence"):
        return None
    return found


def observations(
    rows: list[dict[str, Any]], stream_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """검수 줄 → 보정 관측(judge/personal.py). 합의 단가와 실제 단가가 모두 있는 건만."""
    out = []
    for row in rows:
        if stream_ids is not None and row["streamId"] not in stream_ids:
            continue
        agreed, actual = row.get("agreedPrice"), row.get("actualPrice")
        if not agreed or not actual:
            continue
        rel = personal.relative(float(actual["value"]), float(agreed["value"]))
        if rel is None:
            continue
        out.append(
            {
                "streamId": row["streamId"],
                "vendorOrgId": row["vendorOrgId"],
                "inspectionId": row["inspectionId"],
                "rel": round(rel, 6),
                "evidence": list(dict.fromkeys([*actual["evidence"], *agreed["evidence"]])),
            }
        )
    return out


def _calibration(app, org, rows, names, streams) -> dict[str, Any]:
    """자료 화면에 보이는 보정 미리 보기. 승인했으면 그 강도로, 아니면 제안 강도로 계산한다(숫자는 검수 표에서 따라간다)."""
    found = applied(org)
    strength = float(found["strength"]) if found else float(PROPOSED_STRENGTH)
    obs = observations(rows)
    out = []
    for sid in dict.fromkeys(o["streamId"] for o in obs):
        if sid not in streams:
            continue
        vendors = []
        for vendor in dict.fromkeys(o["vendorOrgId"] for o in obs if o["streamId"] == sid):
            made = personal.estimate(obs, strength, sid, vendor)
            if made is None:
                continue
            vendors.append(
                {
                    "vendorOrgId": vendor,
                    "vendorName": names.get(vendor) or _name(app, vendor),
                    "records": made["records"],
                    "ratePct": round(made["rate"] * 100, 1),
                }
            )
        base = personal.estimate(obs, strength, sid, "")
        out.append(
            {
                "streamId": sid,
                "name": stream_label(streams[sid]),
                "records": base["streamRecords"] if base else 0,
                # 아직 거래하지 않은 업체에 쓰는 비율(이 회사의 이 폐기물 기록만으로)
                "streamRatePct": round(base["streamRate"] * 100, 1) if base else None,
                "vendors": vendors,
            }
        )
    return {
        "applied": found,
        "strength": strength,
        "proposedStrength": PROPOSED_STRENGTH,
        "streams": out,
    }


def judgment_history(
    app: Any, emitter_id: str, stream_ids: list[str], session_id: str
) -> dict[str, Any] | None:
    """최적 배정 도구에 넘길 회사 기록(승인했을 때만). {approval, observations}."""
    try:
        org = app.store.get_org(emitter_id)
    except NotFound:
        return None
    found = applied(org)
    if found is None:
        return None
    rows = inspection_rows(app, emitter_id, exclude_session=session_id)
    obs = observations(rows, set(stream_ids))
    if not obs:
        return None
    return {"approval": found["evidence"], "observations": obs}


def agent_history(
    app: Any, emitter_id: str, vendor_id: str, stream_ids: list[str], session_id: str
) -> list[dict[str, Any]] | None:
    """회사 측 AI가 받는 같은 업체와의 지난 검수 요약. 숫자(단가)는 싣지 않는다: 메시지의 숫자는 근거가 있어야 하고,
    이 요약은 말투와 물을 것을 정하는 데만 쓴다. 회사가 껐거나 기록이 없으면 None."""
    pairs = pair_history(app, emitter_id, vendor_id, stream_ids, session_id)
    if not pairs:
        return None
    rows = inspection_rows(
        app, emitter_id, vendor_id=vendor_id, stream_ids=set(stream_ids), exclude_session=session_id
    )
    out = []
    for sid, summary in pairs.items():
        if summary["status"] == "none":
            continue
        out.append(
            {
                "streamId": sid,
                "status": summary["status"],
                "records": summary["count"],
                "gradeLower": summary["gradeLower"],
                "priceLower": summary["priceLower"],
                "inspections": [
                    {
                        "inspectedOn": r["inspectedOn"],
                        # 등급은 정해진 값만 싣는다(업체가 적은 기준표 글이 AI 맥락에 들어가지 않게)
                        "agreedGrade": r["agreedGrade"]
                        if r["agreedGrade"] in grade_model.GRADES
                        else None,
                        "actualGrade": r["actualGrade"]
                        if r["actualGrade"] in grade_model.GRADES
                        else None,
                        "grade": r["grade"],
                        "price": r["price"],
                    }
                    for r in rows
                    if r["streamId"] == sid
                ],
            }
        )
    return out or None


def set_enabled(app: Any, org_id: str, value: bool) -> dict[str, Any]:
    """기록 사용 켜기·끄기. 조직 기록에 memory.enabled로 남긴다. 권한은 service가 확인한다."""
    return update(app, org_id, {"enabled": value})


def update(app: Any, org_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """PUT /orgs/{orgId}/memory. enabled(계약 검토·AI 맥락에 쓰기)와 applyToJudgment(판정 반영 승인)를 받는다.

    판정 반영을 켜면 담당자 승인을 human 근거로 남기고(값이 보정 강도) 그 ID를 org.memory.applied에 둔다. 끄면
    applied만 지운다(지난 승인 근거는 그대로 남는다). 기록 사용을 끄면 판정 반영도 함께 멈춘다. 권한은 service가 확인한다.
    """
    org = dict(app.store.get_org(org_id))
    memo = dict(org.get("memory") or {})
    if "enabled" in body:
        memo["enabled"] = bool(body["enabled"])
        if not memo["enabled"]:
            memo["applied"] = None
    if "applyToJudgment" in body:
        if not body["applyToJudgment"]:
            memo["applied"] = None
        elif memo.get("enabled") is False:
            raise ValueError("회사 기록 사용을 먼저 켜 주세요.")
        elif not (memo.get("applied") or {}).get("evidence"):
            now = feedback._now(app)
            evidence_id = ids.human_id(
                org_id, f"회사기록-판정반영-승인-{ids.digest({'org': org_id, 'at': now})[:12]}"
            )
            app.registry.put(
                Evidence(
                    id=evidence_id,
                    kind="human",
                    label="회사 기록 판정 반영 승인",
                    value=float(PROPOSED_STRENGTH),
                    unit="건",
                    excerpt=(
                        "배출 기업 담당자가 지난 검수 기록으로 업체별 예상 단가를 맞추는 보정을 판정에 반영하도록 "
                        f"승인했습니다. 보정 강도는 계약 단가를 검수 기록 {PROPOSED_STRENGTH}건 몫으로 믿는다는 뜻입니다."
                    ),
                    source="배출 기업 담당자 승인(자료 화면 회사 기록)",
                    as_of=app.today,
                    access=(org_id,),
                    meta={
                        "ownerOrgId": org_id,
                        "memoryApproval": True,
                        "strength": PROPOSED_STRENGTH,
                        "approvedAt": now,
                    },
                )
            )
            memo["applied"] = {
                "evidence": evidence_id,
                "strength": PROPOSED_STRENGTH,
                "approvedOn": app.today,
            }
    org["memory"] = memo
    app.store.put_org(org)
    return org_memory(app, org_id)


def strength_suggestion(app: Any) -> dict[str, Any]:
    """운영 화면의 보정 강도 제안(judge/personal.suggest_strength). 실제 회사 사이의 검수만 쓰고, 묶음은 (배출 기업, 폐기물)이다."""
    groups: dict[tuple[str, str], list[float]] = {}
    for record in feedback.outcomes(app, origin="real"):
        agreed = _agreed_price(record)
        actual = record.get("actualUnitPriceKrwPerKg")
        if agreed is None or actual is None:
            continue
        rel = personal.relative(float(actual), float(agreed["value"]))
        if rel is None:
            continue
        groups.setdefault((record["emitterOrgId"], record["streamId"]), []).append(rel)
    return {**personal.suggest_strength(groups), "current": PROPOSED_STRENGTH}
