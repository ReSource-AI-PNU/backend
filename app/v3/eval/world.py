"""사례 하나의 가상 세계를 만든다. 회사, 업체, 좌표, 수치는 모두 가상이다.

사례의 world를 저장소와 근거 레지스트리로 옮긴다. 흐름과 업체의 값은 시연 데이터와 같은 방식으로
근거(measure, model, doc, human)를 붙여 등록하므로, 판단 도구와 검증기가 시연과 똑같이 돈다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from app.v3.demo.seed import SYNTHETIC_NOTE, _Seeder
from app.v3.evidence.model import PUBLIC
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import load_seed_dir
from app.v3.intake.shields import PromptShieldsClient
from app.v3.judge.defaults import register_defaults
from app.v3.store import InMemoryStore

DEFAULT_TODAY = "2026-11-17"
EMITTER = {
    "id": "org_eval",
    "name": "(주)가상정밀",
    "region": "부산 강서구(가상)",
    "location": [128.8260, 35.0871],
}
VENDOR_LOCATION = [128.8721, 35.1493]
STREAM_DEFAULTS = {
    "process": "사출",
    "wasteItem": "폐플라스틱",
    "lot": None,
    "bulkDensityKgPerM3": 300,
    "contaminants": [],
}
FLEET_DEFAULTS = {"capacityKg": 5000, "capacityM3": 20, "costPerKm": 1200}
GUARD_DEFAULTS = {
    "days": ["화", "목"],
    "minPickupsPerMonth": 2,
    "autoDecline": [],
}


def keyword_shield(keywords: list[str]) -> PromptShieldsClient:
    """Prompt Shields 자리에 넣는 결정적 가짜. 정해 둔 글귀가 든 문서만 탐지로 답한다.

    실제 Content Safety 호출이 아니다. 진행기가 탐지 결과를 받았을 때의 동작만 본다.
    """

    def transport(method, url, headers, body):
        documents = json.loads(body.decode("utf-8"))["documents"]
        flags = [{"attackDetected": any(k in doc for k in keywords)} for doc in documents]
        return 200, {}, json.dumps({"documentsAnalysis": flags}).encode("utf-8")

    return PromptShieldsClient("https://shield.invalid", "eval", transport)


def _stream(seeder: _Seeder, org: str, spec: dict[str, Any]) -> dict[str, Any]:
    sid = spec["id"]
    stream = {**STREAM_DEFAULTS, **spec, "orgId": org}
    stream.setdefault("name", sid)
    stream.setdefault("material", None)
    stream.setdefault("origin", None)
    stream.setdefault("form", None)
    stream["contaminants"] = [
        {"name": c} if isinstance(c, str) else dict(c) for c in stream.get("contaminants") or []
    ]
    kg = stream.get("monthlyKg")
    if isinstance(kg, (int, float)) and not isinstance(kg, bool):
        measured = stream.get("monthlyKgBasis", "measured") == "measured"
        stream["monthlyKgBasis"] = "measured" if measured else "estimated"
        stream["monthlyKgEvidence"] = seeder.monthly(org, sid, kg, measured)
    else:
        stream.update(monthlyKg=None, monthlyKgBasis=None, monthlyKgEvidence=[])
    measure = stream.get("measure")
    if measure:
        stream["measure"] = seeder.measure(
            org, sid, measure["contaminationPct"], measure["moisturePct"]
        )
    else:
        stream["measure"] = None
    photo = stream.get("photo")
    if photo:
        found = photo.get("found") or ", ".join(c["name"] for c in stream["contaminants"])
        stream["photo"] = seeder.photo(org, sid, photo["contaminationPct"], found or "이물")
    else:
        stream["photo"] = None
    current = stream.get("currentTreatment")
    if current:
        done = seeder.current(org, sid, current["unitCostKrwPerKg"])
        stream["currentTreatment"] = {**done, "method": current.get("method") or done["method"]}
    else:
        stream["currentTreatment"] = None
    for key in ("evidenceFrom",):  # 다른 조직의 근거를 끌어다 붙인 흐름(조직 경계 사례)
        borrowed = stream.pop(key, None)
        if borrowed and stream["measure"]:
            stream["measure"]["evidence"] = [*stream["measure"]["evidence"], *borrowed]
    return {**stream, "synthetic": True}


def _vendor(seeder: _Seeder, spec: dict[str, Any]) -> dict[str, Any]:
    org = spec["orgId"]
    permits = [
        seeder.permit(org, p["wasteItem"], list(p.get("materials") or []))
        for p in spec.get("permits") or []
    ]
    criteria = []
    for item in spec.get("criteria") or []:
        item = {"form": None, "grade": "B", **item}
        cid = item.pop("id")
        criteria.append(seeder.criterion(org, cid, **item))
    fleet_spec = spec.get("fleet")
    if fleet_spec is None:
        fleet_spec = FLEET_DEFAULTS
    fleet: dict[str, Any] = {}
    if fleet_spec:
        merged = {**FLEET_DEFAULTS, **fleet_spec}
        fleet = seeder.fleet(
            org,
            list(spec.get("location") or VENDOR_LOCATION),
            merged["capacityKg"],
            merged["capacityM3"],
            merged["costPerKm"],
        )
    return {
        "orgId": org,
        "permits": permits,
        "criteria": criteria,
        "fleet": fleet,
        "guardrails": {**GUARD_DEFAULTS, **(spec.get("guardrails") or {})},
        "model": spec.get("model") or {"provider": "foundry", "name": "gpt-5-mini"},
        "synthetic": True,
    }


def base_registry(source: Any = None) -> InMemoryRegistry:
    """공개 근거(법령, 기준값, 가정값)만 든 새 레지스트리. source가 있으면 거기서 옮긴다."""
    registry = InMemoryRegistry()
    if source is not None:
        for item in source.list(org_id=None):
            if PUBLIC in item.access:
                registry.put(item)
    else:
        register_defaults(registry, DEFAULT_TODAY)
        load_seed_dir(registry)
    return registry


def build_world(case: dict[str, Any], *, registry_source: Any = None):
    """(store, registry, today, shield). 사례마다 새로 만든다."""
    world = case["world"]
    today = str(world.get("today") or DEFAULT_TODAY)
    store = InMemoryStore(lambda: f"{today}T00:00:00+00:00")
    registry = base_registry(registry_source)
    seeder = _Seeder(registry, today)
    emitter = {**EMITTER, **(world.get("emitter") or {}), "role": "emitter"}
    store.put_org({**emitter, "synthetic": True, "note": SYNTHETIC_NOTE})
    for other in world.get("otherEmitters") or []:
        org = {**EMITTER, **{k: v for k, v in other.items() if k != "streams"}, "role": "emitter"}
        store.put_org({**org, "synthetic": True, "note": SYNTHETIC_NOTE})
        for spec in other.get("streams") or []:
            store.put_stream(_stream(seeder, org["id"], spec))
    for spec in world["streams"]:
        store.put_stream(_stream(seeder, emitter["id"], spec))
    for spec in world.get("vendors") or []:
        store.put_org(
            {
                "id": spec["orgId"],
                "name": spec.get("name") or f"(주)가상업체-{spec['orgId'].removeprefix('org_')}",
                "role": "vendor",
                "region": spec.get("region") or "부산(가상)",
                "location": list(spec.get("location") or VENDOR_LOCATION),
                "synthetic": True,
                "note": SYNTHETIC_NOTE,
            }
        )
        store.put_vendor_profile(_vendor(seeder, spec))
    for patch in world.get("evidencePatches") or []:
        patch_evidence(registry, patch)
    shield = None
    if world.get("shieldKeywords"):
        shield = keyword_shield(list(world["shieldKeywords"]))
    return store, registry, today, shield


def patch_evidence(registry: Any, patch: dict[str, Any]) -> None:
    """근거 하나의 유효 기간이나 값을 바꾼다(만료, 대화 뒤 변경 사례)."""
    found = registry.get(patch["id"], None)
    changes: dict[str, Any] = {}
    if "validUntil" in patch:
        changes["valid_until"] = patch["validUntil"]
    if "value" in patch:
        changes["value"] = patch["value"]
    if "access" in patch:
        changes["access"] = tuple(patch["access"])
    registry.put(replace(found, **changes))
