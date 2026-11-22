"""Material-specific pilot vendors exercised by the same matching and negotiation rules."""

from __future__ import annotations

from app.v3 import ids, live
from app.v3.agents.orchestrator import Orchestrator
from app.v3.demo import negotiation_test
from app.v3.demo.seed import _Seeder, stream_evidence_ids
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound, InMemoryRegistry
from app.v3.judge.common import num, quality
from app.v3.store import InMemoryStore
from app.v3.untrusted import stream_label

VENDOR_NAMES = {
    "PP": ("강서폴리머", "동남순환수지", "한결재생소재"),
    "PE": ("서부수지자원", "대양재생수지", "동해폴리에틸렌"),
    "LDPE": ("필름자원", "순환소재", "그린원료"),
    "ABS": ("정밀수지리사이클", "남부ABS소재", "새움엔지니어링수지"),
    "PS": ("하나스티렌자원", "동진PS소재", "세움재생스티렌"),
    "PET": ("청명페트자원", "한빛페트소재", "동해페트리사이클"),
    "PP+ABS": ("다온복합수지", "서진혼합소재", "푸른복합자원"),
}


def eligible_vendors(stream: dict) -> dict[str, tuple]:
    material = stream.get("material")
    names = VENDOR_NAMES.get(material)
    monthly_kg = num(stream.get("monthlyKg"))
    if not names or monthly_kg is None:
        return {}
    facts = quality(stream)
    contamination, moisture = facts["contaminationPct"], facts["moisturePct"]
    return {
        f"org_mvp_{ids.slug(material)}_{index}": (name, *terms[1:])
        for index, (name, terms) in enumerate(zip(names, negotiation_test.VENDOR_OPTIONS), 1)
        if monthly_kg >= terms[2]
        and (contamination is None or contamination <= terms[3])
        and (moisture is None or moisture <= terms[4])
    }


def match(source, emitter: dict, stream_ids: set[str]) -> list[dict]:
    """Run each unassigned stream in scratch stores, leaving live trade and approval records alone."""
    matches = []
    for stream in source.store.list_streams(emitter["id"]):
        if stream["id"] in stream_ids:
            result = _match_stream(source, emitter, stream)
            if result is not None:
                matches.append(result)
    return matches


def _match_stream(source, emitter: dict, stream: dict) -> dict | None:
    from app.v3.service import App

    vendors = eligible_vendors(stream)
    if not vendors:
        return None

    org_id, sid = emitter["id"], stream["id"]
    store, registry = InMemoryStore(), InMemoryRegistry()
    store.put_org(
        {
            "id": org_id,
            "name": "배출 기업",
            "role": "emitter",
            "simulation": True,
            "synthetic": True,
            "region": emitter.get("region"),
            "location": emitter.get("location"),
        }
    )
    evidence_ids = stream_evidence_ids(stream) + list(
        (stream.get("answers") or {}).get("evidence") or []
    )
    for evidence_id in dict.fromkeys(evidence_ids):
        try:
            registry.put(source.registry.get(evidence_id, org_id))
        except (EvidenceNotFound, EvidenceForbidden):
            continue

    store.put_stream(stream)
    seeder = _Seeder(registry, source.today)
    for vendor_id, option in vendors.items():
        index = int(vendor_id.rsplit("_", 1)[1])
        negotiation_test.seed_vendor(store, seeder, vendor_id, index, option, (stream["material"],))
    negotiation_test.request_policy(registry, org_id, source.today, vendors=tuple(vendors))
    trial = App(store, registry, today=source.today)
    runner = Orchestrator(trial, feed=live.Recorder(store, hub=live.Hub()))
    session_id = runner.create(org_id)["id"]
    run = runner.run(session_id)
    while run["status"] == "awaiting_answers":
        # The live flow has already asked for inputs. Preserve unknown values in the pilot.
        for question in store.list_questions(session_id):
            if not question.get("answered"):
                runner.answer(session_id, question["id"], "unknown")
        run = runner.run(session_id)

    work = run["work"]
    assignment = next(iter((work.get("result") or {}).get("assignments") or []), {})
    quotes = []
    for thread in store.list_threads(session_id=session_id):
        vendor_id, thread_id = thread["vendorOrgId"], thread["id"]
        offer = work["offers"].get(thread_id, {}).get(sid)
        if not offer:
            continue
        name, _, minimum, contamination, moisture, _, day, fee = vendors[vendor_id]
        round_result = work.get("negotiation", {}).get(thread_id, {}).get("result", {}).get(sid, {})
        final = offer["unitPriceKrwPerKg"]
        quotes.append(
            {
                "name": name,
                "minimumKg": minimum,
                "maxContaminationPct": contamination,
                "maxMoisturePct": moisture,
                "pickupDay": day,
                "pickupFee": fee or 0,
                "firstPrice": round_result.get("before", final),
                "requestedPrice": _requested_price(thread, sid),
                "finalPrice": final,
                "conditional": offer["type"] == "conditional",
                "selected": vendor_id == assignment.get("vendorOrgId"),
            }
        )
    if not quotes:
        return None
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
