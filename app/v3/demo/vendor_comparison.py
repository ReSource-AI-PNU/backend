"""Run the MVP example using the entered waste and a disposable rule-based diagnosis."""

from __future__ import annotations

from app.v3 import live
from app.v3.agents.orchestrator import Orchestrator
from app.v3.demo import negotiation_test
from app.v3.demo.seed import stream_evidence_ids
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, InMemoryRegistry
from app.v3.store import InMemoryStore
from app.v3.untrusted import stream_label


def compare(source, emitter_id: str, stream_ids: set[str]) -> dict:
    """Use scratch stores. The example offers never become live candidates or approvals."""
    from app.v3.service import App

    store, registry = InMemoryStore(), InMemoryRegistry()
    store.put_org(
        {
            "id": emitter_id,
            "name": "배출 기업",
            "role": "emitter",
            "simulation": True,
            "synthetic": True,
            "region": negotiation_test.REGION,
            "location": negotiation_test.LOCATION,
        }
    )
    streams = [
        stream
        for stream in source.store.list_streams(emitter_id)
        if stream["id"] in stream_ids and stream.get("material") in negotiation_test.MATERIALS
    ]
    if not streams:
        return {"status": "ready", "items": []}

    for stream in streams:
        evidence = stream_evidence_ids(stream) + list(
            (stream.get("answers") or {}).get("evidence") or []
        )
        for evidence_id in dict.fromkeys(evidence):
            try:
                registry.put(source.registry.get(evidence_id, emitter_id))
            except (EvidenceNotFound, EvidenceForbidden):
                continue
        store.put_stream(stream)

    negotiation_test.ensure_vendors(store, registry, source.today)
    negotiation_test.request_policy(registry, emitter_id, source.today)
    trial = App(store, registry, today=source.today)
    runner = Orchestrator(trial, feed=live.Recorder(store, hub=live.Hub()))
    session_id = runner.create(emitter_id)["id"]
    run = runner.run(session_id)
    while run["status"] == "awaiting_answers":
        # A live answer has already been collected where available. Leave other values unknown.
        for question in store.list_questions(session_id):
            if not question.get("answered"):
                runner.answer(session_id, question["id"], "unknown")
        run = runner.run(session_id)
    return {
        "status": "ready",
        "items": [
            _item(stream, run["work"], store.list_threads(session_id=session_id))
            for stream in streams
        ],
    }


def _item(stream: dict, work: dict, threads: list[dict]) -> dict:
    sid = stream["id"]
    by_vendor = {thread["vendorOrgId"]: thread for thread in threads if sid in thread["streamIds"]}
    reasons = {
        item["vendorOrgId"]: item["reason"] for item in work["excluded"] if item["streamId"] == sid
    }
    selected = next(
        (
            item.get("vendorOrgId")
            for item in (work.get("result") or {}).get("assignments", [])
            if item["streamId"] == sid
        ),
        None,
    )
    quotes = []
    for index, (name, _, minimum, _, _, _, day, fee) in enumerate(
        negotiation_test.VENDOR_OPTIONS, 1
    ):
        vendor_id = negotiation_test.vendor_id(index)
        thread = by_vendor.get(vendor_id, {})
        thread_id = thread.get("id")
        final = work["offers"].get(thread_id, {}).get(sid, {}).get("unitPriceKrwPerKg")
        round_result = work.get("negotiation", {}).get(thread_id, {}).get("result", {}).get(sid, {})
        requested = _requested_price(thread, sid)
        quotes.append(
            {
                "name": name,
                "minimumKg": minimum,
                "pickupDay": day,
                "pickupFee": fee or 0,
                "firstPrice": round_result.get("before", final),
                "requestedPrice": requested,
                "finalPrice": final,
                "selected": vendor_id == selected,
                "reason": (
                    reasons.get(vendor_id)
                    or thread.get("statusReason")
                    or "입력한 조건으로 견적을 받지 못했습니다."
                )
                if final is None
                else None,
            }
        )
    return {"streamId": sid, "name": stream_label(stream), "quotes": quotes}


def _requested_price(thread: dict, stream_id: str) -> float | None:
    for message in thread.get("messages", []):
        ask = next(
            (
                item
                for item in (message.get("meta") or {}).get("priceAsks", [])
                if item["streamId"] == stream_id
            ),
            None,
        )
        if ask is not None:
            return next(
                (claim["value"] for claim in message["claims"] if claim["id"] == ask["claimId"]),
                None,
            )
    return None
