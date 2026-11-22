"""조건 변경 후 새 진단, 격리된 조건 비교. 기존 합의·문서·검수는 보존한다."""

from __future__ import annotations

import copy
import math

from app.v3 import ids, service
from app.v3.a2a.thread import Thread
from app.v3.agents.orchestrator import Orchestrator, discoverable_profiles
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore


def profile_basis(profile):
    return ids.digest(
        {
            k: profile.get(k)
            for k in (
                "criteria",
                "permits",
                "permitDocuments",
                "fleet",
                "transport",
                "guardrails",
                "process",
            )
        }
    )


def owned(app, session_id, viewer):
    session, owner = service._session_for(app, session_id, viewer)
    if not owner or session["emitterOrgId"] != viewer:
        raise service.ServiceError(
            "FORBIDDEN", "배출 기업 담당자만 조건을 비교할 수 있습니다.", 403
        )
    return session


def clean(app, session, body):
    if not isinstance(body, dict) or set(body) - {
        "streamId",
        "monthlyKg",
        "maxPickupsPerMonth",
        "token",
    }:
        raise service.ServiceError("INVALID_VALUE", "비교할 조건을 확인해 주세요.", 422)
    stream_id = body.get("streamId")
    streams = app.store.list_streams(session["emitterOrgId"])
    if stream_id not in {s["id"] for s in streams}:
        raise service.ServiceError("STREAM_NOT_FOUND", "비교할 폐기물을 선택해 주세요.", 404)
    amount, limit = body.get("monthlyKg"), body.get("maxPickupsPerMonth")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (float, int))
        or not math.isfinite(amount)
        or not 0 < amount <= 10_000_000
    ):
        raise service.ServiceError(
            "INVALID_VALUE", "월 발생량은 0보다 크고 천만 kg 이하로 입력해 주세요.", 422
        )
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 31
    ):
        raise service.ServiceError(
            "INVALID_VALUE", "월 최대 수거 횟수는 1~31회로 입력해 주세요.", 422
        )
    return {"streamId": stream_id, "monthlyKg": amount, "maxPickupsPerMonth": limit}


def fingerprint(app, session, options):
    org = app.store.get_org(session["emitterOrgId"])
    return ids.digest(
        {
            "session": session,
            "org": org,
            "options": options,
            "streams": app.store.list_streams(org["id"]),
            "vendors": discoverable_profiles(app.store, org),
            "today": app.today,
        }
    )


def eligible(profiles, maximum):
    """업체가 밝힌 수거 횟수와 최소 횟수를 모두 지킨다. 미확인은 임의로 줄이지 않는다."""
    if maximum is None:
        return profiles, []
    kept, excluded = {}, []
    for vendor_id, p in profiles.items():
        transport = p.get("transport") or {}
        values = [
            transport.get("pickupsPerMonth"),
            (transport.get("offRoute") or {}).get("pickupsPerMonth"),
            (p.get("guardrails") or {}).get("minPickupsPerMonth"),
        ]
        counts = [
            v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        ]
        if counts and max(counts) <= maximum:
            kept[vendor_id] = p
        else:
            excluded.append(
                {
                    "vendorOrgId": vendor_id,
                    "reason": "요청한 최대 수거 횟수를 넘습니다."
                    if counts
                    else "업체의 수거 횟수를 먼저 확인해야 합니다.",
                }
            )
    return kept, excluded


def _new(app, source, reason, options=None, token=None):
    session = Orchestrator(app).create(source["emitterOrgId"])
    session["work"]["adaptive"] = {
        "sourceSessionId": source["id"],
        "reason": reason,
        "state": "queued",
        "before": copy.deepcopy(source["work"].get("result")),
        "options": options,
        "token": token,
    }
    if options:
        session["work"]["scenario"] = options
    app.store.put_session(session)
    previous = app.store.get_session(source["id"])
    previous["work"]["replacementSessionId"] = session["id"]
    app.store.put_session(previous)
    return session


def vendor_changed(app, vendor_id, before):
    if app.read_only or before == profile_basis(app.store.get_vendor_profile(vendor_id)):
        return
    # 한 회사의 최신 진행만 갱신한다. 이전 거래마다 중복 진단을 만들지 않는다.
    latest = {}
    for s in app.store.list_sessions():
        key = (
            s.get("createdAt", ""),
            int(s["id"].rsplit("_", 1)[-1]) if s["id"].rsplit("_", 1)[-1].isdigit() else 0,
        )
        if s["emitterOrgId"] not in latest or key > latest[s["emitterOrgId"]][0]:
            latest[s["emitterOrgId"]] = (key, s)
    for _, source in latest.values():
        if source["status"] not in ("awaiting_approval", "confirmed") or source["work"].get(
            "replacementSessionId"
        ):
            continue
        if not any(
            t.get("vendorOrgId") == vendor_id and t["status"] in ("agreed", "confirmed")
            for t in app.store.list_threads(session_id=source["id"])
        ):
            continue
        changed = False
        orchestrator = Orchestrator(app)
        for row in app.store.list_threads(session_id=source["id"]):
            if row.get("vendorOrgId") != vendor_id:
                continue
            thread = Thread.from_dict(row)
            agreed = source["work"].get("agreedTerms", {}).get(thread.id)
            if not agreed:
                continue
            now = orchestrator._vendor_terms(source, thread, agreed.get("inputs"))
            assigned = source["work"]["assigned"].get(thread.id) or []
            if (
                now["common"] != agreed["common"]
                or now.get("pickup") != agreed.get("pickup")
                or any(now["streams"].get(sid) != agreed["streams"].get(sid) for sid in assigned)
            ):
                changed = True
        if not changed:
            continue
        _new(
            app,
            source,
            "업체의 매입·수거 조건이 바뀌어 대안을 다시 찾습니다.",
            source["work"].get("scenario"),
        )


def state(app, session_id, viewer):
    source = owned(app, session_id, viewer)
    successor = source["work"].get("replacementSessionId")
    active = app.store.get_session(successor) if successor else source
    info = active["work"].get("adaptive")
    if not info:
        return {"change": None}
    return {
        "change": {
            "sessionId": active["id"],
            "sourceSessionId": info["sourceSessionId"],
            "reason": info["reason"],
            "state": info["state"],
            "status": service._shown_status(app, active),
            "before": info.get("before"),
            "after": active["work"].get("result"),
        }
    }


def preview(app, session_id, body, viewer):
    source = owned(app, session_id, viewer)
    if service._live_run(app, session_id):
        raise service.ServiceError("RUN_IN_PROGRESS", "찾기가 끝난 뒤 조건을 비교해 주세요.", 409)
    options = clean(app, source, body)
    token = fingerprint(app, source, options)
    # 별도 메모리 저장소: 미리보기는 원본 근거·승인·단가·메시지를 쓰지 않는다.
    store = InMemoryStore(app.store.now)
    registry = InMemoryRegistry(app.registry.list())
    org = app.store.get_org(viewer)
    store.put_org(org)
    for p in discoverable_profiles(app.store, org).values():
        store.put_org(app.store.get_org(p["orgId"]))
        store.put_vendor_profile(p)
    for s in app.store.list_streams(viewer):
        store.put_stream(s)
    sandbox = service.App(
        store,
        registry,
        today=app.today,
        negotiate=app.negotiate,
        distance_provider=app.distance_provider,
    )
    service.update_stream(
        sandbox, viewer, options["streamId"], {"monthlyKg": options["monthlyKg"]}, viewer
    )
    candidate = Orchestrator(sandbox).create(viewer)
    candidate["work"]["scenario"] = options
    store.put_session(candidate)
    execute(sandbox, candidate["id"], source_questions=app.store.list_questions(source["id"]))
    view = service.get_view(sandbox, candidate["id"], viewer)
    result = view["result"]
    wanted = set()
    service._evidence_ids_in(result or {}, wanted)
    evidence = {
        key: service.get_evidence(sandbox, key, viewer)
        for key in wanted
        if registry.exists(key) and registry.get(key).visible_to(viewer)
    }
    return {
        "token": token,
        "options": options,
        "result": result,
        "questions": [q for q in view["questions"] if not q["answered"]],
        "excluded": store.get_session(candidate["id"])["work"].get("scenarioExcluded", []),
        "evidence": evidence,
        "basis": "매입 기준표와 계산 도구로 비교한 예상 결과입니다. 실제 AI 재협상 후 조건이 달라질 수 있습니다.",
    }


def apply(app, session_id, body, viewer):
    if app.read_only:
        raise service.ServiceError("DEMO_READ_ONLY", "체험 중에는 조건을 저장할 수 없습니다.", 403)
    source = owned(app, session_id, viewer)
    options = clean(app, source, body)
    token = body.get("token")
    existing = source["work"].get("replacementSessionId")
    if existing:
        next_session = app.store.get_session(existing)
        if token and next_session["work"].get("adaptive", {}).get("token") == token:
            return {"sessionId": existing}
        raise service.ServiceError(
            "STALE_PREVIEW", "새 대안이 준비되고 있습니다. 최신 찾기에서 다시 비교해 주세요.", 409
        )
    with service._exclusive(app, session_id, "scenario"):
        source = owned(app, session_id, viewer)
        if not token or token != fingerprint(app, source, options):
            raise service.ServiceError(
                "STALE_PREVIEW", "자료나 업체 조건이 바뀌었습니다. 다시 비교해 주세요.", 409
            )
        service.update_stream(
            app, viewer, options["streamId"], {"monthlyKg": options["monthlyKg"]}, viewer
        )
        candidate = _new(
            app,
            source,
            "선택한 조건으로 대안을 다시 찾습니다. 양쪽 승인이 필요합니다.",
            options,
            token,
        )
    return {"sessionId": candidate["id"]}


def execute(app, session_id, source_questions=None):
    """기존 담당자 답만 재사용한다. 새로 필요한 자료를 임의로 채우지 않는다."""
    session = app.store.get_session(session_id)
    info = session["work"].get("adaptive")
    if source_questions is None:
        source_questions = app.store.list_questions(info["sourceSessionId"]) if info else []
    answers = {
        (q.get("streamId"), q.get("field")): q.get("answer")
        for q in source_questions
        if q.get("answered")
    }
    for _ in range(5):
        Orchestrator(app).run(session_id)
        pending = [q for q in app.store.list_questions(session_id) if not q.get("answered")]
        reusable = [q for q in pending if (q.get("streamId"), q.get("field")) in answers]
        if not pending or not reusable:
            break
        for q in reusable:
            Orchestrator(app).answer(
                session_id, q["id"], answers[(q.get("streamId"), q.get("field"))]
            )
    current = app.store.get_session(session_id)
    if info:
        current["work"]["adaptive"]["state"] = (
            "ready" if current["status"] == "awaiting_approval" else "needs_input"
        )
        app.store.put_session(current)


def launch_pending(app, launch):
    if app.read_only:
        return
    for s in app.store.list_sessions():
        info = s["work"].get("adaptive") or {}
        if info.get("state") not in ("queued", "running") or service._live_run(app, s["id"]):
            continue
        if info.get("attempts", 0) >= 3:
            info["state"] = "failed"
            app.store.put_session(s)
            continue
        try:
            started = service.start_run(app, s["id"], s["emitterOrgId"])
        except service.ServiceError:
            continue
        current = app.store.get_session(s["id"])
        current["work"]["adaptive"].update(state="running", attempts=info.get("attempts", 0) + 1)
        app.store.put_session(current)
        try:
            launch(s["id"], started["runId"])
        except Exception:
            service._release(app, s["id"], started["runId"])
            raise
