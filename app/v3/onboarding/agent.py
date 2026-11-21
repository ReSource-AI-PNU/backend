"""온보딩 대화. 업체 측 AI가 담당자와 대화하며 tools.py의 도구로 업체 정보 초안을 만든다.

상태는 업체 프로필의 onboarding 칸에 둔다: {"messages": [...], "draft": {키: 항목}, "updatedAt"}.
업체 측 협상 에이전트는 이 칸을 읽지 않는다(허가·기준·노선·가드레일만 읽는다).

- 첫 인사는 모델 없이 코드가 쓴다. 회사 이름으로 공공 명부를 찾아 후보를 보여 준다(모델이 없어도 시작된다).
- 담당자 메시지마다 모델은 정해진 JSON(EXTRACT_SCHEMA)만 낸다: 명부 후보 확인·다시 찾기, 말에서 읽은 값과
  그 인용, 모른다고 한 항목, 되물을 말. 도구 실행(초안 쓰기)과 답 문장은 코드가 한다. 그래서 모델이 하지 않은
  일을 했다고 말할 수 없다(도구 호출 방식에서 qwen2.5:32b가 도구를 부르지 않고 "넣었다"고 답한 일이 있었다).
- 모델 호출이 실패하면 그 사실만 답하고 담당자 메시지와 초안은 그대로 남는다.
- 확정은 confirm()만 한다. 담당자가 고른 키만 service.update_vendor로 저장하고, 저장한 키는 초안에서 지운다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.v3.agents.llm import LLMError
from app.v3.onboarding import tools as T
from app.v3.textutil import josa

log = logging.getLogger(__name__)

KEEP_MESSAGES = 40
MAX_TEXT = 1_000
HISTORY = 16  # 모델에 넣는 지난 메시지 수

SYSTEM = """너는 폐기물 처리 업체 "{company}" 한 곳만 대변하는 업체 측 AI다. 이 업체가 막 가입해서,
배출 기업의 문의를 받을 수 있도록 업체 정보 초안을 담당자와 함께 채운다.

담당자의 맨 마지막 말을 읽고 정해진 JSON으로만 답한다. 답 문장은 프로그램이 쓴다.

- registry: 공공 허가 명부.
  - 담당자가 [지금 상태]의 명부 후보 가운데 자기 사업장이 맞다고 하면 action "confirm"과 그 registryId.
  - 다른 상호나 지역을 말하면 action "search"와 query(상호), region(시·군·구, 없으면 null).
  - 그 밖에는 action "none".
- fields: 맨 마지막 말에 담긴 값. 항목마다 key, value(글), quote(담당자의 말 그대로, 고치지 않고 일부만 옮겨도 됨).
  - 숫자 항목의 value는 숫자만 쓴다. 톤은 kg로 바꾼다(5톤 → 5000).
  - 요일, 수거 가능 지역, 받지 않는 조건은 쉼표로 나눈 글로 쓴다(예: "월, 수, 금").
  - 처리 방식은 재활용, 물질 재활용, 열분해, 소각, 매립 가운데 하나다.
  - 단가는 업체가 돈을 주고 사는 매입이면 양수, 업체가 처리비를 받으면 음수다. 말로 어느 쪽인지 분명하지
    않으면 넣지 말고 ask에서 매입 단가인지 처리비인지 묻는다.
  - 담당자가 말하지 않은 값은 절대 넣지 않는다. 맨 마지막 말에 없는 값은 넣지 않는다(지난 말의 값은 이미 초안에 있다).
- skipped: 담당자가 모른다, 없다, 해당 없다고 한 항목의 key(예: "최대 거리는 잘 모르겠어요" →
  ["fleet.maxPickupKm"], "받지 않는 조건은 없어요" → ["guardrails.autoDecline"]).
- ask: 담당자의 말이 애매해서 되물어야 할 때만 짧은 질문 한 문장(존댓말). 아니면 null.
"""

_SAVED_WORDS = (
    "저장했",
    "등록했",
    "확정했",
    "저장되었",
    "등록되었",
    "확정되었",
    "저장됐",
    "등록됐",
)


# ── 상태 ──────────────────────────────────────────────────────────────
def _profile(app: Any, org_id: str) -> dict[str, Any]:
    try:
        return app.store.get_vendor_profile(org_id)
    except Exception:  # noqa: BLE001 - 업체 프로필이 아직 없으면 빈 프로필로 시작한다
        return {"orgId": org_id, "permits": [], "criteria": [], "fleet": {}, "guardrails": {}}


def _state(profile: dict[str, Any]) -> dict[str, Any]:
    state = profile.get("onboarding")
    if not isinstance(state, dict):
        state = {}
    return {
        "messages": list(state.get("messages") or []),
        "draft": dict(state.get("draft") or {}),
        # 모른다고 한 항목(다시 묻지 않는다), 다시 찾은 명부 후보, 파일 안내를 한 번 했는지
        "skipped": list(state.get("skipped") or []),
        "candidates": list(state.get("candidates") or []),
        "fileHint": bool(state.get("fileHint")),
        "asked": list(state.get("asked") or []),
    }


def _save(app: Any, org_id: str, state: dict[str, Any]) -> None:
    """프로필을 다시 읽어 onboarding 칸만 바꾼다(모델이 답하는 동안 담당자가 폼에서 저장한 값을 덮지 않게)."""
    profile = _profile(app, org_id)
    profile["onboarding"] = {
        "messages": state["messages"][-KEEP_MESSAGES:],
        "draft": state["draft"],
        "skipped": state.get("skipped") or [],
        "candidates": state.get("candidates") or [],
        "fileHint": bool(state.get("fileHint")),
        "asked": state.get("asked") or [],
        "updatedAt": app.store.now(),
    }
    app.store.put_vendor_profile(profile)


def _message(
    app: Any, role: str, text: str, tools: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "id": app.store.next_id("om"),
        "role": role,
        "text": text,
        "at": app.store.now(),
        "tools": tools or [],
    }


def greeting(app: Any, org_id: str) -> dict[str, Any]:
    """첫 인사. 회사 이름으로 공공 명부를 찾아 후보를 보여 준다(모델을 부르지 않는다)."""
    name = str(app.store.get_org(org_id).get("name") or "")
    found = T.search_registry(name, org_name=name)
    same = [c for c in found if c["sameNameAsCompany"]]
    head = f"안녕하세요. {name} 전용 업체 측 AI입니다. 배출 기업의 문의를 받으려면 업체 정보를 채워야 해서, 함께 채워 보겠습니다."
    if same:
        lines = [f"- {c['name']} · {c['business']} · {c['address']}" for c in same[:3]]
        body = (
            "공공 허가 명부에서 같은 상호를 찾았습니다.\n"
            + "\n".join(lines)
            + "\n이 가운데 우리 사업장이 맞는 곳을 알려 주시면 허가 품목과 차고지 위치를 초안에 넣겠습니다."
        )
    else:
        body = (
            f"공공 허가 명부에서 '{name}' 상호를 찾지 못했습니다. 허가증에 적힌 상호나 소재지(시·군·구)를 알려 주시면 다시 찾겠습니다. "
            "명부에 없으면 허가증 파일을 아래 '허가증·매입 기준표 올리기'에 올려 주세요."
        )
    tail = "그다음에 차량, 수거 가능 지역, 수거 요일을 여쭙겠습니다."
    return {
        "id": "greeting",
        "role": "assistant",
        "text": f"{head}\n\n{body}\n\n{tail}",
        "at": None,
        "tools": [
            {
                "name": "find_registry_vendor",
                "summary": f"공공 허가 명부에서 '{name}' 찾기: "
                + (f"{len(same)}곳" if same else "없음"),
            }
        ],
        "candidates": same[:3],
    }


def _model_view(llm: Any) -> dict[str, str] | None:
    provider = getattr(llm, "provider", None)
    model = getattr(llm, "model", None)
    if not provider or not model:
        return None
    return {"provider": str(provider), "name": str(model)}


def _llm(app: Any, org_id: str, profile: dict[str, Any]) -> Any | None:
    """이 업체의 업체 측 모델. 규칙 정책(모델 없음)이면 None."""
    llm = app.llm_for("vendor", org_id, profile.get("model"))
    if llm is getattr(app, "_rule", None) or not hasattr(llm, "complete"):
        return None
    return llm


def view(app: Any, org_id: str) -> dict[str, Any]:
    profile = _profile(app, org_id)
    state = _state(profile)
    messages = state["messages"] or [greeting(app, org_id)]
    llm = _llm(app, org_id, profile)
    return {
        "orgId": org_id,
        "model": _model_view(llm) if llm is not None else {"provider": "rule", "name": "모델 없음"},
        "messages": [
            {k: m.get(k) for k in ("id", "role", "text", "at", "tools")} for m in messages
        ],
        "draft": [
            {
                "key": key,
                "section": T.FIELDS[key][1],
                "label": T.FIELDS[key][0],
                "value": entry.get("value"),
                "text": entry.get("text"),
                "source": {
                    "kind": (entry.get("source") or {}).get("kind"),
                    "label": (entry.get("source") or {}).get("label"),
                    "quote": (entry.get("source") or {}).get("quote"),
                },
            }
            for key, entry in sorted(
                state["draft"].items(),
                key=lambda kv: list(T.FIELDS).index(kv[0]) if kv[0] in T.FIELDS else 99,
            )
            if key in T.FIELDS
        ],
        "checks": T.checks(profile),
        "discoverable": T.discoverable(profile),
    }


# ── 대화 한 차례 ─────────────────────────────────────────────────────
def _history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages[-HISTORY:]:
        if m.get("role") in ("user", "assistant") and m.get("text"):
            out.append({"role": m["role"], "content": str(m["text"])})
    return out


# 대화로 물을 항목의 순서. 앞의 것부터 두 가지씩 묻는다
ASK_ORDER = (
    "fleet.vehicleCount",
    "fleet.capacityKg",
    "fleet.serviceAreas",
    "guardrails.days",
    "guardrails.minPickupsPerMonth",
    "guardrails.floorPriceKrwPerKg",
    "guardrails.autoDecline",
    "process",
    "fleet.maxPickupKm",
)


def _saved(profile: dict[str, Any], key: str) -> bool:
    if key == "permits":
        return bool(profile.get("permits"))
    if key == "process":
        return bool(profile.get("process"))
    section, _, name = key.partition(".")
    value = (profile.get(section) or {}).get(name)
    return value not in (None, "", [], {})


def _candidates(app: Any, org_id: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """지금 고를 수 있는 명부 후보. 다시 찾은 것이 있으면 그것, 없으면 회사 이름으로 찾은 같은 상호."""
    name = str(app.store.get_org(org_id).get("name") or "")
    if state.get("candidates"):
        rows = {r["id"]: r for r in public_registry_rows(state["candidates"])}
        return [T._record_view(rows[i], name) for i in state["candidates"] if i in rows]
    return [c for c in T.search_registry(name, org_name=name) if c["sameNameAsCompany"]][:3]


def public_registry_rows(ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(ids)
    return [r for r in T.public_vendors.registry()["vendors"] if r["id"] in wanted]


def _context_note(app: Any, org_id: str, profile: dict[str, Any], state: dict[str, Any]) -> str:
    """모델이 차례마다 맨 끝에서 보는 상태."""
    draft = state["draft"]
    saved = [T.FIELDS[k][0] for k in T.FIELDS if _saved(profile, k)]
    drafted = [
        f"{k} ({T.FIELDS[k][0]}): {e.get('text')}" for k, e in draft.items() if k in T.FIELDS
    ]
    lines = [
        "[지금 상태]",
        "- 업체 정보에 저장된 것: " + (", ".join(saved) or "없음"),
        "- 초안에 있는 것: " + ("; ".join(drafted) or "없음"),
        "- 모른다고 한 것: " + (", ".join(state.get("skipped") or []) or "없음"),
    ]
    if "permits" not in draft and not profile.get("permits"):
        found = _candidates(app, org_id, state)
        lines.append(
            "- 공공 허가 명부 후보: "
            + (
                "; ".join(
                    f"registryId {c['registryId']} = {c['name']} ({c['address']})" for c in found
                )
                or "없음"
            )
        )
    keys = ", ".join(f"{k} ({T.FIELDS[k][0]})" for k in _ASKABLE)
    lines.append(f"- fields에 쓸 수 있는 key: {keys}")
    return "\n".join(lines)


QUESTIONS = {
    "fleet.vehicleCount": "수거 차량은 몇 대인가요?",
    "fleet.capacityKg": "차량 한 대에 몇 kg(또는 몇 톤)까지 싣나요?",
    "fleet.serviceAreas": "어느 지역까지 수거하러 가시나요?",
    "guardrails.days": "수거할 수 있는 요일은 언제인가요?",
    "guardrails.minPickupsPerMonth": "한 달에 최소 몇 번은 수거해야 하나요?",
    "guardrails.floorPriceKrwPerKg": "kg당 가장 낮게 부를 단가는 얼마인가요? 매입 단가인지 처리비인지도 알려 주세요.",
    "guardrails.autoDecline": "받지 않는 폐기물 조건이 있나요?",
    "process": "처리 방식은 무엇인가요? (재활용, 물질 재활용, 열분해, 소각, 매립)",
    "fleet.maxPickupKm": "차고지에서 최대 몇 km까지 수거하러 가시나요?",
}
_ASKABLE = [k for k, f in T.FIELDS.items() if f[2]]
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "registry": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["none", "confirm", "search"]},
                "registryId": {"type": ["string", "null"]},
                "query": {"type": ["string", "null"]},
                "region": {"type": ["string", "null"]},
            },
            "required": ["action", "registryId", "query", "region"],
            "additionalProperties": False,
        },
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": _ASKABLE},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["key", "value", "quote"],
                "additionalProperties": False,
            },
        },
        "skipped": {"type": "array", "items": {"type": "string", "enum": _ASKABLE}},
        "ask": {"type": ["string", "null"]},
    },
    "required": ["registry", "fields", "skipped", "ask"],
    "additionalProperties": False,
}


def _coerce(key: str, text: Any) -> Any:
    """모델이 글로 낸 값 → 도구가 받는 모양(숫자, 목록, 글). 읽을 수 없으면 글 그대로(도구가 거절한다).

    숫자 항목은 숫자가 하나만 든 글이면 그 숫자를 쓴다("2대" → 2). 적재 중량에 톤이 붙으면 kg로 바꾼다.
    """
    raw = str(text if text is not None else "").strip()
    if key in T.NUMBER_KEYS:
        cleaned = raw.replace(",", "").replace("−", "-").replace(" ", "")
        found = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
        if len(found) != 1:
            return raw
        number = float(found[0])
        if key == "fleet.capacityKg" and re.search(r"톤|t(?![a-z])", cleaned, re.IGNORECASE):
            number *= 1_000
        return int(number) if number == int(number) else number
    if key in T.LIST_KEYS:
        return [part.strip() for part in raw.replace("、", ",").split(",") if part.strip()]
    return raw


def reply(app: Any, org_id: str, text: str) -> dict[str, Any]:
    """담당자 메시지 하나에 답한다. 상태를 저장하고 view를 돌려준다."""
    profile = _profile(app, org_id)
    state = _state(profile)
    if not state["messages"]:
        first = greeting(app, org_id)
        first.pop("candidates", None)
        first["at"] = app.store.now()
        state["messages"].append(first)
    state["messages"].append(_message(app, "user", text[:MAX_TEXT]))
    llm = _llm(app, org_id, profile)
    used: list[dict[str, str]] = []
    if llm is None:
        answer = (
            "지금은 업체 측 AI 모델이 연결되어 있지 않아 대화로 채울 수 없습니다. "
            "아래 칸에 직접 적거나 허가증·매입 기준표 파일을 올려 주세요."
        )
    else:
        answer = _run(llm, app, org_id, profile, state, used)
    state["messages"].append(_message(app, "assistant", answer, used))
    _save(app, org_id, state)
    return view(app, org_id)


def _ask_model(
    llm: Any, app: Any, org_id: str, profile: dict[str, Any], state: dict[str, Any]
) -> dict:
    company = str(app.store.get_org(org_id).get("name") or "")
    convo: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM.format(company=company)},
        *_history(state["messages"]),
        {"role": "system", "content": _context_note(app, org_id, profile, state)},
    ]
    out = llm.complete(convo, response_schema=EXTRACT_SCHEMA)
    try:
        parsed = json.loads(out.get("content") or "")
    except (TypeError, json.JSONDecodeError) as error:
        raise LLMError("모델이 정해진 JSON으로 답하지 않았다") from error
    if not isinstance(parsed, dict):
        raise LLMError("모델이 정해진 JSON으로 답하지 않았다")
    return parsed


def _run(llm, app, org_id, profile, state, used) -> str:
    try:
        parsed = _ask_model(llm, app, org_id, profile, state)
    except LLMError as error:
        log.warning("onboarding model failed: %s", error)
        return (
            "업체 측 AI가 지금 답하지 못했습니다. 잠시 뒤에 다시 보내 주세요. "
            "이미 만든 초안은 그대로 있습니다."
        )
    said = [m["text"] for m in state["messages"] if m.get("role") == "user"]
    ctx = T.Context(app=app, org_id=org_id, draft=state["draft"], said=said)
    parts: list[str] = []

    registry = parsed.get("registry") if isinstance(parsed.get("registry"), dict) else {}
    action = registry.get("action")
    if "permits" in state["draft"] or profile.get("permits"):
        action = (
            None  # 허가 품목이 이미 있으면 명부를 다시 다루지 않는다(모델이 같은 확인을 되풀이해도)
        )
    if action == "confirm" and registry.get("registryId"):
        result, summary = T.call_tool(
            ctx, "use_registry_vendor", {"registryId": str(registry["registryId"])}
        )
        used.append({"name": "use_registry_vendor", "summary": summary})
        if "error" in result:
            parts.append("말씀하신 명부 줄을 찾지 못했습니다. 상호나 소재지를 다시 알려 주세요.")
        else:
            parts.append(
                f"공공 허가 명부의 허가 품목({T._permits_text(result['permits'])})과 차고지 위치를 초안에 넣었습니다."
                if result["permits"]
                else "공공 허가 명부의 차고지 위치를 초안에 넣었습니다. 명부의 처리 폐기물 분류는 허가 품목으로 옮기지 못해, 허가증을 올려 주셔야 합니다."
            )
            if not result["sameNameAsCompany"]:
                parts.append("다만 회사 이름이 명부 상호와 달라 명부 대조는 되지 않습니다.")
    elif action == "search" and (registry.get("query") or registry.get("region")):
        args = {"query": registry.get("query") or "", "region": registry.get("region") or ""}
        result, summary = T.call_tool(ctx, "find_registry_vendor", args)
        used.append({"name": "find_registry_vendor", "summary": summary})
        found = result.get("candidates") or []
        state["candidates"] = [c["registryId"] for c in found[:3]]
        if found:
            lines = "\n".join(
                f"- {c['name']} · {c['business']} · {c['address']}" for c in found[:3]
            )
            parts.append(
                f"공공 허가 명부에서 찾은 곳입니다.\n{lines}\n이 가운데 우리 사업장이 맞는 곳을 알려 주세요."
            )
        else:
            parts.append(
                "공공 허가 명부에서 찾지 못했습니다. 명부는 지자체가 올린 곳만 있어 빠진 업체도 있습니다. "
                "허가증 파일을 아래 '허가증·매입 기준표 올리기'에 올려 주세요."
            )

    fields = [
        {
            "key": f.get("key"),
            "value": _coerce(str(f.get("key")), f.get("value")),
            "quote": f.get("quote"),
        }
        for f in parsed.get("fields") or []
        if isinstance(f, dict)
    ]
    # 이번 말에 없는 인용(모델이 지난 말의 값을 되풀이한 것)과 초안에 이미 같은 값이 있는 항목은 조용히 뺀다
    latest = T._squash(state["messages"][-1]["text"])
    fields = [
        f
        for f in fields
        if T._squash(f["quote"]) in latest
        and not (f["key"] in state["draft"] and state["draft"][f["key"]].get("value") == f["value"])
    ]
    if fields:
        result, summary = T.call_tool(ctx, "propose_fields", {"fields": fields})
        used.append({"name": "propose_fields", "summary": summary})
        written = [
            f"{T.FIELDS[k][0]} {state['draft'][k]['text']}" for k in result.get("written") or []
        ]
        if written:
            parts.append("초안에 넣었습니다: " + ", ".join(written) + ".")
        for item in result.get("rejected") or []:
            label = T.FIELDS.get(item.get("key") or "", ("그 값",))[0]
            parts.append(f"{josa(label, '은는')} 말씀과 맞는지 확인하지 못해 넣지 않았습니다.")

    skipped = [k for k in parsed.get("skipped") or [] if k in T.FIELDS]
    if not skipped and not fields and _unknown(state["messages"][-1]["text"]):
        # 모델이 놓쳐도 "모르겠어요"는 코드가 읽는다: 말에 든 항목 이름, 없으면 지난번에 물은 항목
        skipped = _named(state["messages"][-1]["text"]) or list(state.get("asked") or [])
    if skipped:
        state["skipped"] = list(dict.fromkeys([*state.get("skipped", []), *skipped]))
        names = ", ".join(T.FIELDS[k][0] for k in skipped if k in T.FIELDS)
        parts.append(f"알겠습니다. {josa(names, '은는')} 비워 두고 넘어가겠습니다.")

    ask = parsed.get("ask")
    if isinstance(ask, str) and ask.strip() and not any(w in ask for w in _SAVED_WORDS):
        parts.append(ask.strip()[:200])
    else:
        parts.append(_next_questions(profile, state))
    if not parts[:-1] and not used:
        parts.insert(0, "말씀에서 초안에 넣을 값을 찾지 못했습니다.")
    return "\n".join(parts)


def _next_questions(profile: dict[str, Any], state: dict[str, Any]) -> str:
    skipped = set(state.get("skipped") or [])
    missing = [
        k
        for k in ASK_ORDER
        if k not in state["draft"] and not _saved(profile, k) and k not in skipped
    ]
    tail = ""
    if not profile.get("criteria") and not state.get("fileHint"):
        state["fileHint"] = True
        tail = " 매입 기준표는 아래 '허가증·매입 기준표 올리기'에 파일로 올려 주시면 AI가 읽습니다."
    if "permits" not in state["draft"] and not profile.get("permits"):
        return (
            "허가 품목이 아직 없습니다. 공공 허가 명부의 사업장이 맞는지, 아니면 허가증에 적힌 상호를 알려 주세요."
            + tail
        )
    if not missing:
        return (
            "필요한 항목을 모두 여쭈었습니다. 아래 초안을 확인하고 맞는 항목을 저장해 주세요."
            + tail
        )
    state["asked"] = missing[:2]
    return " ".join(QUESTIONS[k] for k in missing[:2]) + tail


_UNKNOWN = ("모르", "몰라", "없어", "없습니다", "없음", "해당 없", "정해지지", "미정")
# 말에 든 낱말 → 항목. 앞의 것부터 본다
_WORDS = (
    ("최대", "fleet.maxPickupKm"),
    ("거리", "fleet.maxPickupKm"),
    ("km", "fleet.maxPickupKm"),
    ("요일", "guardrails.days"),
    ("횟수", "guardrails.minPickupsPerMonth"),
    ("몇 번", "guardrails.minPickupsPerMonth"),
    ("단가", "guardrails.floorPriceKrwPerKg"),
    ("처리비", "guardrails.floorPriceKrwPerKg"),
    ("조건", "guardrails.autoDecline"),
    ("처리 방식", "process"),
    ("적재", "fleet.capacityKg"),
    ("톤", "fleet.capacityKg"),
    ("차량", "fleet.vehicleCount"),
    ("지역", "fleet.serviceAreas"),
)


def _unknown(text: str) -> bool:
    return any(word in text for word in _UNKNOWN)


def _named(text: str) -> list[str]:
    return list(dict.fromkeys(key for word, key in _WORDS if word in text))


# ── 확인·초기화 ──────────────────────────────────────────────────────
def patch_for(draft: dict[str, Any], keys: list[str], profile: dict[str, Any]) -> dict[str, Any]:
    """고른 키의 초안 → PUT /vendors 본문. 허가 품목은 이미 있는 품목을 지우지 않고 더한다."""
    patch: dict[str, Any] = {}
    for key in keys:
        entry = draft.get(key)
        if not entry or key not in T.FIELDS:
            continue
        value = entry.get("value")
        if key == "permits":
            kept = [
                {"wasteItem": p.get("wasteItem"), "materials": list(p.get("materials") or [])}
                for p in profile.get("permits") or []
            ]
            have = {p["wasteItem"] for p in kept}
            patch["permits"] = kept + [p for p in value or [] if p.get("wasteItem") not in have]
        elif key == "process":
            patch["process"] = value
        else:
            section, _, name = key.partition(".")
            patch.setdefault(section, {})[name] = value
    return patch


def confirm(app: Any, org_id: str, keys: list[str], viewer: str) -> dict[str, Any]:
    from app.v3 import service

    profile = _profile(app, org_id)
    state = _state(profile)
    picked = [k for k in dict.fromkeys(keys) if k in state["draft"]]
    if not picked:
        raise service.ServiceError("NOTHING_TO_CONFIRM", "저장할 초안 항목을 골라 주세요.", 422)
    patch = patch_for(state["draft"], picked, profile)
    service.update_vendor(app, org_id, patch, viewer)
    state = _state(_profile(app, org_id))
    for key in picked:
        state["draft"].pop(key, None)
    labels = ", ".join(T.FIELDS[k][0] for k in picked)
    state["messages"].append(
        _message(app, "assistant", f"담당자가 확인한 {labels}을(를) 업체 정보에 저장했습니다.")
    )
    _save(app, org_id, state)
    return {"profile": service.get_vendor(app, org_id, viewer), "onboarding": view(app, org_id)}


def reset(app: Any, org_id: str) -> dict[str, Any]:
    profile = _profile(app, org_id)
    profile.pop("onboarding", None)
    app.store.put_vendor_profile(profile)
    return view(app, org_id)


# ── 서비스 입구(api.py가 부른다) ─────────────────────────────────────
def _vendor_owner(app: Any, org_id: str, viewer: str) -> None:
    from app.v3 import service

    service._owner(app, org_id, viewer, "vendor")


def get_onboarding(app: Any, org_id: str, viewer: str) -> dict[str, Any]:
    _vendor_owner(app, org_id, viewer)
    return view(app, org_id)


def post_message(app: Any, org_id: str, body: dict[str, Any], viewer: str) -> dict[str, Any]:
    from app.v3 import service

    _vendor_owner(app, org_id, viewer)
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise service.ServiceError("EMPTY_MESSAGE", "보낼 말을 적어 주세요.", 422)
    return reply(app, org_id, text.strip())


def post_confirm(app: Any, org_id: str, body: dict[str, Any], viewer: str) -> dict[str, Any]:
    from app.v3 import service

    _vendor_owner(app, org_id, viewer)
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise service.ServiceError("INVALID_BODY", "저장할 항목(keys)을 목록으로 보내 주세요.", 422)
    return confirm(app, org_id, keys, viewer)


def delete_onboarding(app: Any, org_id: str, viewer: str) -> dict[str, Any]:
    _vendor_owner(app, org_id, viewer)
    return reset(app, org_id)
