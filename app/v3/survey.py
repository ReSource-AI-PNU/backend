"""설문 후속: 공개 가격 범위, 거래별 확인, 계량 기록과 선택한 상대에게만 공유하는 사진.

범위를 임의의 한 값으로 바꾸지 않는다. 업체 담당자가 사유와 확인 항목을 남겨 좁히고,
배출 기업이 단일 제안을 수락한 뒤에도 기존 허가·적재·양쪽 승인 절차를 거친다.
"""

from __future__ import annotations

import base64
import copy
import csv
import io
import math
from datetime import date
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.judge.common import criterion_checks, permit_match, quality
from app.v3.store import SHARED_THREAD_STATUSES, NotFound, VersionConflict

FACTS = ("material", "quality", "transport")
BASIS = (
    "material",
    "form",
    "origin",
    "monthlyKg",
    "monthlyKgBasis",
    "photo",
    "measure",
    "answers",
    "contaminants",
    "singleMaterial",
)


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("유한한 숫자를 적어 주세요.")
    return float(value)


def clean_criterion(item: dict) -> dict:
    item = copy.deepcopy(item)
    low, high = item.get("priceLow"), item.get("priceHigh")
    if low is not None or high is not None:
        low, high = number(low), number(high)
        if low > high or low < -10_000 or high > 10_000 or low * high < 0:
            raise ValueError(
                "단가 범위는 같은 거래 방향으로, 작은 값부터 kg당 만 원 이내로 적어 주세요."
            )
        item.update(priceLow=low, priceHigh=high, unitPriceKrwPerKg=None)
    return item


def stream_hash(stream: dict) -> str:
    return stream.get("_rangeSourceHash") or ids.digest({k: stream.get(k) for k in BASIS})


def criterion_hash(profile: dict, criterion: dict) -> str:
    # 허가·수거·협상 조건을 고치면 과거 범위를 다시 확인해야 한다.
    return ids.digest(
        {"criterion": criterion, "profile": profile.get("_rangeBasis") or profile_basis(profile)}
    )


def profile_basis(profile: dict) -> str:
    return ids.digest(
        {
            k: profile.get(k)
            for k in ("permits", "guardrails", "fleet", "transport", "collectionPolicy")
        }
    )


def effective_criterion(stream: dict, profile: dict, criterion: dict) -> dict:
    if criterion.get("priceLow") is None:
        return criterion
    deal = (stream.get("rangeDeals") or {}).get(profile.get("orgId"), {})
    if (
        deal.get("criterionId") == criterion.get("id")
        and deal.get("streamHash") == stream_hash(stream)
        and deal.get("criterionHash") == criterion_hash(profile, criterion)
    ):
        return {**criterion, "unitPriceKrwPerKg": deal["price"], "evidence": deal["evidence"]}
    return {**criterion, "unitPriceKrwPerKg": None}


def _service():
    from app.v3 import service

    return service


def _owned_stream(app, stream_id, org_id):
    try:
        return app.store.get_stream(stream_id, org_id)
    except NotFound:
        raise _service().ServiceError("NOT_FOUND", "그 폐기물을 찾지 못했습니다.", 404) from None


def _quote_parts(app, record):
    stream = _owned_stream(app, record["streamId"], record["orgId"])
    try:
        profile = app.store.get_vendor_profile(record["vendorOrgId"])
        criterion = next(c for c in profile["criteria"] if c["id"] == record["criterionId"])
    except (NotFound, StopIteration):
        raise ValueError("업체 기준이 바뀌었습니다. 새로 문의해 주세요.") from None
    if record["streamHash"] != stream_hash(stream) or record["criterionHash"] != criterion_hash(
        profile, criterion
    ):
        raise ValueError("폐기물 자료나 업체 조건이 바뀌었습니다. 새로 문의해 주세요.")
    return stream, profile, criterion


def outward(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in ("streamHash", "criterionHash")}


def view(app, viewer: str) -> dict:
    me = _service()._org(app, viewer)
    records = app.store.list_survey_records()
    quotes = [
        r
        for r in records
        if r.get("kind") == "range_quote" and viewer in (r["orgId"], r["vendorOrgId"])
    ]
    photos = [
        {k: v for k, v in r.items() if k != "data"}
        for r in records
        if r.get("kind") == "photo" and r["orgId"] == viewer
    ]
    # 원본 사진은 소유자 또는 명시적으로 선택한 업체만 볼 수 있다. 문의 본문에는 접근 URL만 싣는다.
    shared = [
        {k: v for k, v in r.items() if k != "data"}
        for r in records
        if r.get("kind") == "photo" and viewer in r.get("sharedWith", [])
    ]
    for entry in photos + shared:
        try:
            entry["streamName"] = app.store.get_stream(entry["streamId"])["name"]
        except NotFound:
            entry["streamName"] = "폐기물 사진"
    candidates = []
    if me.get("role") == "emitter":
        for stream in app.store.list_streams(viewer):
            for profile in app.store.list_vendor_profiles():
                if permit_match(stream, profile)[0] is not True:
                    continue
                for criterion in profile.get("criteria") or []:
                    if (
                        criterion.get("material") != stream.get("material")
                        or criterion.get("priceLow") is None
                    ):
                        continue
                    checks = criterion_checks(stream, criterion, quality(stream))
                    if any(c[1] == "unmet" for c in checks):
                        continue
                    kg = stream.get("monthlyKg")
                    low, high = criterion["priceLow"], criterion["priceHigh"]
                    from app.v3.current_cost import signed_cost

                    current = signed_cost(stream.get("currentTreatment"))
                    now = -current * kg if current is not None and kg else None
                    from app.v3.judge.common import Recorder

                    rec = Recorder(
                        "price_range_preview",
                        app.registry,
                        viewer,
                        app.today,
                        {
                            "stream": stream,
                            "criterionId": criterion["id"],
                            "vendorOrgId": profile["orgId"],
                        },
                    )
                    money_evidence = []
                    if kg and not app.read_only:
                        source = [
                            *(criterion.get("rangeEvidence") or []),
                            *(stream.get("monthlyKgEvidence") or []),
                        ]
                        for key, value, formula in (
                            (
                                "monthlyLow",
                                low * kg,
                                "공개 범위 하한 × 월 발생량 (운반비·작업비 제외)",
                            ),
                            (
                                "monthlyHigh",
                                high * kg,
                                "공개 범위 상한 × 월 발생량 (운반비·작업비 제외)",
                            ),
                        ):
                            claim = rec.number(key, value, "원/월", formula, source)
                            money_evidence += claim["evidence"]
                        if now is not None:
                            current_ev = (stream.get("currentTreatment") or {}).get("evidence", [])
                            for key, value in (
                                ("differenceLow", low * kg - now),
                                ("differenceHigh", high * kg - now),
                            ):
                                claim = rec.number(
                                    key,
                                    value,
                                    "원/월",
                                    "예상 정산액 − 현재 처리 방식의 월 손익 (운반비·작업비 반영 전)",
                                    [*source, *current_ev],
                                )
                                money_evidence += claim["evidence"]
                    candidates.append(
                        {
                            "streamId": stream["id"],
                            "streamName": stream.get("name"),
                            "vendorOrgId": profile["orgId"],
                            "vendorName": app.store.get_org(profile["orgId"]).get("name"),
                            "criterionId": criterion["id"],
                            "material": criterion["material"],
                            "form": criterion.get("form"),
                            "low": low,
                            "high": high,
                            "monthlyLow": low * kg if kg else None,
                            "moneyEvidence": list(dict.fromkeys(money_evidence)),
                            "differenceLow": low * kg - now if now is not None else None,
                            "differenceHigh": high * kg - now if now is not None else None,
                            "monthlyHigh": high * kg if kg else None,
                            "evidence": criterion.get("rangeEvidence", []),
                            "checks": [{"key": c[0], "status": c[1], "text": c[2]} for c in checks],
                            "note": "품질·허가·운반 조건 확인 전 예상 정산액입니다. 양수는 받는 돈, 음수는 내는 돈이고 운반비와 작업비는 반영 전입니다.",
                        }
                    )
    for q in quotes:
        try:
            _quote_parts(app, q)
            q["stale"] = False
            if me.get("role") == "vendor":
                stream, profile, _ = _quote_parts(app, q)
                q["route"] = route_preview(app, profile, stream)
        except ValueError:
            q["stale"] = True
    recipients = {
        (q["streamId"], q["vendorOrgId"]): q["vendorName"] for q in quotes if q["orgId"] == viewer
    }
    for thread in app.store.list_threads(org_id=viewer):
        if thread.get("emitterOrgId") == viewer and thread.get("status") in SHARED_THREAD_STATUSES:
            for sid in thread.get("streamIds", []):
                recipients[(sid, thread["vendorOrgId"])] = app.store.get_org(
                    thread["vendorOrgId"]
                ).get("name")
    return {
        "role": me.get("role"),
        "recipients": [
            {"streamId": sid, "vendorOrgId": vid, "vendorName": name}
            for (sid, vid), name in recipients.items()
        ],
        "candidates": candidates,
        "quotes": [outward(q) for q in quotes],
        "photos": photos,
        "sharedPhotos": shared,
    }


def start_quote(app, viewer: str, body: dict) -> dict:
    service = _service()
    service._owner(app, viewer, viewer, "emitter")
    entry = next(
        (
            c
            for c in view(app, viewer)["candidates"]
            if all(c.get(k) == body.get(k) for k in ("streamId", "vendorOrgId", "criterionId"))
        ),
        None,
    )
    if entry is None:
        raise service.ServiceError("NOT_FOUND", "지금 자료에 맞는 범위 단가 후보가 아닙니다.", 404)
    stream = _owned_stream(app, entry["streamId"], viewer)
    profile = app.store.get_vendor_profile(entry["vendorOrgId"])
    criterion = next(c for c in profile["criteria"] if c["id"] == entry["criterionId"])
    for old in app.store.list_survey_records():
        if (
            old.get("kind") == "range_quote"
            and all(old.get(k) == entry.get(k) for k in ("streamId", "vendorOrgId", "criterionId"))
            and old.get("streamHash") == stream_hash(stream)
            and old.get("criterionHash") == criterion_hash(profile, criterion)
        ):
            return outward(old)
    record = {
        "id": app.store.next_id("rq"),
        "kind": "range_quote",
        "orgId": viewer,
        **{
            k: entry[k]
            for k in (
                "streamId",
                "streamName",
                "vendorOrgId",
                "vendorName",
                "criterionId",
                "low",
                "high",
            )
        },
        "streamHash": stream_hash(stream),
        "criterionHash": criterion_hash(profile, criterion),
        "status": "range",
        "revision": 1,
        "confirmed": [],
        "history": [],
        "evidence": entry["evidence"],
        "facts": {
            k: stream.get(k)
            for k in ("material", "form", "monthlyKg", "monthlyKgBasis", "contaminants")
        },
        "quality": {k: v for k, v in quality(stream).items() if k != "evidence"},
        "createdAt": app.store.now(),
    }
    return outward(app.store.put_survey_record(record))


def change_quote(app, viewer: str, quote_id: str, body: dict, *, accept=False) -> dict:
    service = _service()
    try:
        q = app.store.get_survey_record(quote_id)
    except NotFound:
        raise service.ServiceError("NOT_FOUND", "그 문의를 찾지 못했습니다.", 404) from None
    if q.get("kind") != "range_quote" or viewer != q["orgId" if accept else "vendorOrgId"]:
        raise service.ServiceError("FORBIDDEN", "이 문의를 변경할 권한이 없습니다.", 403)
    if body.get("revision") != q["revision"]:
        raise service.ServiceError(
            "VERSION_CONFLICT", "문의가 바뀌었습니다. 새로 불러와 주세요.", 409
        )
    try:
        _stream, profile, _criterion = _quote_parts(app, q)
        if q["status"] == "accepted":
            raise ValueError("수락한 제안입니다. 조건을 바꾸려면 새로 문의해 주세요.")
        if accept:
            if q["low"] != q["high"] or set(q["confirmed"]) != set(FACTS):
                raise ValueError("단가와 재질·품질·운반 확인이 끝나지 않았습니다.")
            q["status"] = "accepted"
            q["acceptedAt"] = app.store.now()
        else:
            low, high = number(body.get("low")), number(body.get("high"))
            if low < q["low"] or high > q["high"] or low > high:
                raise ValueError("앞선 범위 안에서만 좁힐 수 있습니다.")
            reason = str(body.get("reason") or "").strip()
            checked = body.get("confirmed")
            if (
                not reason
                or len(reason) > 500
                or not isinstance(checked, list)
                or any(k not in FACTS for k in checked)
            ):
                raise ValueError("변경 사유와 확인한 항목을 적어 주세요.")
            confirmed = sorted(set(q["confirmed"]) | set(checked))
            if low == high and set(confirmed) != set(FACTS):
                raise ValueError("단일 단가 제안 전 재질·품질·운반 조건을 모두 확인해 주세요.")
            guard = profile.get("guardrails") or {}
            if low == high and (
                (guard.get("floorPriceKrwPerKg") is not None and low < guard["floorPriceKrwPerKg"])
                or (
                    guard.get("ceilingPriceKrwPerKg") is not None
                    and high > guard["ceilingPriceKrwPerKg"]
                )
            ):
                raise ValueError("내부 협상 한도를 벗어난 제안입니다. 업체 설정을 확인해 주세요.")
            q["history"].append(
                {
                    "low": q["low"],
                    "high": q["high"],
                    "confirmed": q["confirmed"],
                    "at": app.store.now(),
                    "reason": reason,
                }
            )
            ev = app.registry.put(
                Evidence(
                    id=ids.human_id(viewer, f"{q['id']}-r{q['revision'] + 1}"),
                    kind="human",
                    label="업체 담당자가 확인한 거래 단가 범위",
                    value=low if low == high else [low, high],
                    unit="원/kg",
                    excerpt=reason,
                    source="업체 담당자 확인",
                    as_of=app.today,
                    access=(q["orgId"], viewer),
                    meta={"ownerOrgId": viewer},
                )
            )
            q.update(
                low=low,
                high=high,
                confirmed=confirmed,
                evidence=[ev.id],
                status="offered" if low == high else "range",
            )
        q["revision"] += 1
        saved = app.store.put_survey_record(q, expected_revision=body["revision"])
        service._recheck(app, q["orgId"])
        return outward(saved)
    except VersionConflict:
        raise service.ServiceError(
            "VERSION_CONFLICT", "문의가 바뀌었습니다. 새로 불러와 주세요.", 409
        ) from None
    except ValueError as error:
        raise service.ServiceError("INVALID_VALUE", str(error), 422) from None


def apply_deals(app, org_id: str) -> None:
    """수락과 진단 사이 재시작에도 안전하게, 현재 자료와 맞는 제안만 적용한다."""
    for stream in app.store.list_streams(org_id):
        deals = {}
        for q in sorted(
            app.store.list_survey_records(),
            key=lambda r: (
                r.get("acceptedAt", ""),
                int(r["id"].split("_")[-1]) if r.get("kind") == "range_quote" else 0,
            ),
        ):
            if (
                q.get("kind") != "range_quote"
                or q.get("orgId") != org_id
                or q.get("streamId") != stream["id"]
                or q.get("status") != "accepted"
            ):
                continue
            try:
                _quote_parts(app, q)
            except (ValueError, _service().ServiceError):
                continue
            deals[q["vendorOrgId"]] = {
                "criterionId": q["criterionId"],
                "price": q["low"],
                "streamHash": q["streamHash"],
                "criterionHash": q["criterionHash"],
                "evidence": q["evidence"],
            }
        if deals != stream.get("rangeDeals", {}):
            stream["rangeDeals"] = deals
            app.store.put_stream(stream)


def route_preview(app, profile: dict, stream: dict) -> dict:
    from app.v3.judge.economics import route_extra_cost
    from app.v3.judge.route import check_loading

    org = app.store.get_org(stream["orgId"])
    stops = []
    groups: dict[str, set[str]] = {}
    for thread in app.store.list_threads(org_id=profile["orgId"]):
        eid = thread.get("emitterOrgId")
        if thread.get("status") != "confirmed" or eid == stream["orgId"]:
            continue
        assigned = (
            app.store.get_session(thread["sessionId"])["work"]
            .get("assigned", {})
            .get(thread["id"], [])
        )
        groups.setdefault(eid, set()).update(assigned)
    pickups = (profile.get("guardrails") or {}).get("minPickupsPerMonth")
    if not isinstance(pickups, (int, float)) or pickups <= 0:
        return {
            "cost": {},
            "loading": {},
            "checks": [
                {
                    "status": "conditional",
                    "text": "월 수거 횟수를 알아야 회당 물량과 추가 운행을 비교할 수 있습니다.",
                }
            ],
            "note": "수거 횟수 확인 전에는 추가 운행비를 계산하지 않습니다.",
        }
    for eid, assigned in groups.items():
        other = app.store.get_org(eid)
        flows = [app.store.get_stream(s) for s in assigned]
        kg = sum(s.get("monthlyKg") or 0 for s in flows) / pickups
        if other.get("location") and kg:
            stops.append({"location": other["location"], "kg": kg})
    extra = route_extra_cost(
        app.registry,
        org_id=profile["orgId"],
        today=app.today,
        vendor=profile,
        stops=stops,
        newStop={
            "location": org.get("location"),
            "kg": stream.get("monthlyKg") / pickups if stream.get("monthlyKg") else None,
        },
        distance_provider=app.distance_provider,
    )
    loading = check_loading(
        app.registry, org_id=profile["orgId"], today=app.today, streams=[stream], vendor=profile
    )

    def claims(result):
        return {c["id"]: c for c in result.get("claims", [])}

    from app.v3.judge.common import Recorder

    rec = Recorder(
        "pickup_capacity_preview",
        app.registry,
        profile["orgId"],
        app.today,
        {
            "existingKg": sum(s["kg"] for s in stops),
            "newKg": stream.get("monthlyKg"),
            "pickups": pickups,
        },
    )
    capacity = (profile.get("fleet") or {}).get("capacityKg")
    additions = {}
    if capacity is not None and stream.get("monthlyKg") is not None:
        remaining = capacity - sum(s["kg"] for s in stops) - stream["monthlyKg"] / pickups
        rec.number(
            "remainingKg",
            remaining,
            "kg",
            "같은 회차 가정: 차량 적재 중량 − 기존 확정 고객 회당 물량 − 새 공장 회당 물량",
            (profile.get("fleet") or {}).get("evidence", []),
        )
        additions = claims(rec.result("conditional", {}))
    return {
        "cost": claims(extra),
        "loading": {**claims(loading), **additions},
        "checks": extra.get("checks", []) + loading.get("checks", []),
        "note": "앱에서 확정된 다른 공장과 같은 회차에 수거한다고 가정한 추가 운행 추정입니다. 월 물량을 월 수거 횟수로 나눕니다. 앱 밖 실제 노선은 포함하지 않으며 도로 자료가 없으면 거리도 가정값입니다.",
    }


def import_weights(app, viewer: str, filename: str, data: bytes, month: str) -> dict:
    service = _service()
    service._owner(app, viewer, viewer, "emitter")
    try:
        date.fromisoformat(month + "-01")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            text = data.decode("cp949")
    except (ValueError, UnicodeError):
        raise service.ServiceError(
            "INVALID_VALUE", "합산할 월을 YYYY-MM로 고르고 UTF-8 또는 CP949 CSV를 올려 주세요.", 422
        ) from None
    aliases = {
        "폐기물 이름": "name",
        "폐기물": "name",
        "계량일": "date",
        "날짜": "date",
        "측정일": "date",
        "중량(kg)": "kg",
        "중량": "kg",
        "무게(kg)": "kg",
        "계량번호": "ticket",
    }
    reader = csv.DictReader(io.StringIO(text))
    columns = [aliases.get(k.strip()) for k in reader.fieldnames or []]
    known = [k for k in columns if k]
    if not {"name", "date", "kg"}.issubset(known) or len(set(known)) != len(known):
        raise service.ServiceError(
            "INVALID_VALUE", "폐기물 이름·계량일·중량(kg) 열을 하나씩 넣어 주세요.", 422
        )
    rows = []
    stream_rows = app.store.list_streams(viewer)
    streams = {s["name"]: s for s in stream_rows}
    if len(streams) != len(stream_rows):
        raise service.ServiceError(
            "INVALID_VALUE",
            "이름이 같은 폐기물이 있습니다. 자료에서 이름을 구분한 뒤 올려 주세요.",
            422,
        )
    existing = {
        r["id"]: r
        for r in app.store.list_survey_records()
        if r.get("kind") == "weight" and r["orgId"] == viewer
    }
    try:
        for i, raw in enumerate(reader, 2):
            row = {
                aliases[k.strip()]: str(v or "").strip()
                for k, v in raw.items()
                if k and k.strip() in aliases
            }
            if not any(row.values()):
                continue
            if i > 5002:
                raise ValueError("한 번에 오천 행까지 올릴 수 있습니다.")
            when = date.fromisoformat(row.get("date", ""))
            kg = number(float(row.get("kg", "").replace(",", "")))
            if kg <= 0 or kg > 1_000_000 or row.get("name") not in streams:
                raise ValueError("등록한 폐기물 이름과 양수 중량을 확인해 주세요.")
            if when.isoformat()[:7] != month:
                continue
            sid = streams[row["name"]]["id"]
            ticket = row.get("ticket") or None
            key = ids.digest([viewer, ticket] if ticket else [viewer, sid, when.isoformat(), kg])
            record = {
                "id": f"weigh_{key}",
                "kind": "weight",
                "orgId": viewer,
                "streamId": sid,
                "date": when.isoformat(),
                "kg": kg,
                "ticket": ticket,
                "filename": filename[:160],
            }
            if record["id"] in existing and any(
                existing[record["id"]].get(k) != record[k] for k in ("streamId", "date", "kg")
            ):
                raise ValueError("같은 계량번호에 다른 값이 있습니다. 원본 기록을 확인해 주세요.")
            if any(r["id"] == record["id"] for r in rows):
                raise ValueError(
                    "계량 기록이 중복됩니다. 같은 날 같은 중량의 다른 반입이면 서로 다른 계량번호를 적어 주세요."
                )
            rows.append(record)
        if not rows:
            raise ValueError("선택한 월의 계량 기록이 없습니다.")
    except (ValueError, TypeError) as error:
        raise service.ServiceError(
            "INVALID_VALUE", f"계량 CSV를 확인해 주세요: {error}", 422
        ) from None
    for row in rows:
        app.store.put_survey_record(row)
    totals = {}
    for row in {**existing, **{r["id"]: r for r in rows}}.values():
        if row["date"][:7] == month:
            totals[row["streamId"]] = totals.get(row["streamId"], 0) + row["kg"]
    for sid, kg in totals.items():
        stream = _owned_stream(app, sid, viewer)
        ev = app.registry.put(
            Evidence(
                id=f"measure:{viewer}:{sid}:계량합산-{month}-{ids.digest(kg)}",
                kind="measure",
                label=f"{month} 계량 기록 월 합계",
                excerpt="담당자가 올린 계량 기록을 선택한 월로 합산했습니다.",
                value=kg,
                unit="kg/월",
                source="담당자가 올린 계량 CSV",
                as_of=app.today,
                access=(viewer,),
                meta={"ownerOrgId": viewer, "month": month},
            )
        )
        stream.update(monthlyKg=kg, monthlyKgBasis="measured", monthlyKgEvidence=[ev.id])
        # 계약 원액을 새 월 물량으로 다시 환산한다.
        service.update_stream(
            app, viewer, sid, {"monthlyKg": kg, "monthlyKgBasis": "measured"}, viewer
        )
        fresh = app.store.get_stream(sid, viewer)
        fresh["monthlyKgEvidence"] = [ev.id]
        app.store.put_stream(fresh)
    service._recheck(app, viewer)
    return {
        "month": month,
        "accepted": sum(r["id"] not in existing for r in rows),
        "duplicates": sum(r["id"] in existing for r in rows),
        "totals": totals,
        "note": "선택한 월의 합계로 월 발생량을 갱신했습니다. 오염도·수분은 만들지 않습니다. 같은 날 같은 중량의 다른 반입은 계량번호로 구분해 주세요.",
    }


def upload_photo(app, viewer: str, stream_id: str, data: bytes) -> dict:
    from PIL import Image, ImageOps, UnidentifiedImageError

    service = _service()
    _owned_stream(app, stream_id, viewer)
    service._owner(app, viewer, viewer, "emitter")
    if len(data) > 5 * 1024 * 1024:
        raise service.ServiceError("INVALID_VALUE", "사진은 오 메가바이트 이하로 올려 주세요.", 422)
    try:
        with Image.open(io.BytesIO(data)) as src:
            if src.format not in ("PNG", "JPEG", "WEBP") or src.width * src.height > 24_000_000:
                raise ValueError("지원하지 않는 사진입니다.")
            im = ImageOps.exif_transpose(src).convert("RGB")
            im.thumbnail((1600, 1600))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise service.ServiceError(
            "INVALID_VALUE", "JPG·PNG·WEBP 사진을 확인해 주세요.", 422
        ) from None
    mine = [
        r
        for r in app.store.list_survey_records()
        if r.get("kind") == "photo" and r["orgId"] == viewer and r["streamId"] == stream_id
    ]
    if len(mine) >= 8:
        raise service.ServiceError(
            "INVALID_VALUE", "폐기물마다 사진은 여덟 장까지 보관합니다.", 422
        )
    record = {
        "id": app.store.next_id("photo"),
        "kind": "photo",
        "orgId": viewer,
        "streamId": stream_id,
        "data": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
        "sharedWith": [],
        "createdAt": app.store.now(),
    }
    saved = app.store.put_survey_record(record)
    return {k: v for k, v in saved.items() if k != "data"}


def photo(app, viewer: str, photo_id: str, *, shared_with=None) -> dict:
    service = _service()
    try:
        record = app.store.get_survey_record(photo_id)
    except NotFound:
        raise service.ServiceError("NOT_FOUND", "사진을 찾지 못했습니다.", 404) from None
    if record.get("kind") != "photo" or (
        record["orgId"] != viewer and viewer not in record["sharedWith"]
    ):
        raise service.ServiceError("NOT_FOUND", "사진을 찾지 못했습니다.", 404)
    if shared_with is not None:
        if viewer != record["orgId"] or not isinstance(shared_with, list):
            raise service.ServiceError(
                "FORBIDDEN", "사진 소유자만 공유 대상을 정할 수 있습니다.", 403
            )
        valid = {
            q["vendorOrgId"]
            for q in app.store.list_survey_records()
            if q.get("kind") == "range_quote"
            and q["orgId"] == viewer
            and q["streamId"] == record["streamId"]
        }
        valid.update(
            t["vendorOrgId"]
            for t in app.store.list_threads(org_id=viewer)
            if record["streamId"] in t.get("streamIds", [])
            and t.get("emitterOrgId") == viewer
            and t.get("status") in SHARED_THREAD_STATUSES
        )
        if any(v not in valid for v in shared_with):
            raise service.ServiceError(
                "INVALID_VALUE", "이 폐기물의 문의 상대만 선택해 주세요.", 422
            )
        record["sharedWith"] = sorted(set(shared_with))
        app.store.put_survey_record(record)
    return record
