"""사례 실행기. 사례마다 가상 세계를 새로 만들어 판단 도구와 진단 한 건을 돌리고 기대와 견준다.

    run_cases(source, cases) -> {"summary": {...}, "results": [...]}

source는 service.App(연결된 모델 설정과 공개 근거를 쓴다), 근거 레지스트리(그 공개 근거를 쓴다),
또는 None(근거 시드와 규칙 정책)이다. 현재 시각이나 난수를 쓰지 않아 같은 입력이면 같은 결과다.

사례마다 세 가지를 본다.
- 판정 일치: expect에 적은 판정 상태, checks 키, 스레드 상태 등이 모두 맞는가
- 금지 위반: forbidden에 적은 일과, 모든 사례에 걸리는 공통 규칙(가드레일, 허가 품목, 승인 전
  확정, 턴 상한, 질문 한도, 조직 경계)을 어겼는가
- 검증기 통과: 나간 메시지와 문서를 근거 검증기에 다시 넣었을 때 통과하는가
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from app.v3 import service
from app.v3.a2a import guardrails as rails
from app.v3.a2a.policy import QuestionPolicy
from app.v3.a2a.thread import Thread
from app.v3.agents.draft import model_label
from app.v3.agents.orchestrator import thread_names
from app.v3.agents.vendor import public_profile
from app.v3.eval import world as worlds
from app.v3.evidence.validator import (
    ALLOWED_TOKENS,
    name_tokens,
    validate_message,
    validate_text,
)
from app.v3.judge.common import permit_match
from app.v3.judge.toolbox import call_tool

MONEY_UNITS = ("원", "원/월", "원/kg", "원/t", "원/회")
MAX_ANSWER_ROUNDS = 6
_MONEY_FIELDS = ("unitPrice", "monthlyAfter", "monthlyNow", "difference")


# ── 실행 ─────────────────────────────────────────────────────────────


def _make_app(case: dict[str, Any], options: dict[str, Any]) -> service.App:
    store, registry, today, shield = worlds.build_world(
        case, registry_source=options.get("registry_source")
    )
    return service.App(
        store,
        registry,
        llm_factory=options.get("llm_factory"),
        today=today,
        shields=shield,
        distance_provider=options.get("distance_provider"),
    )


def _emitter(case: dict[str, Any]) -> str:
    return str((case["world"].get("emitter") or {}).get("id") or worlds.EMITTER["id"])


def _tool_arguments(app: service.App, case: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    emitter = app.store.get_org(_emitter(case))
    side = spec.get("org") or "emitter"

    def stream(sid: str) -> dict[str, Any]:
        found = app.store.get_stream(sid)
        if sid == spec.get("stream") or sid in (spec.get("streams") or []):
            found.update(copy.deepcopy(spec.get("streamPatch") or {}))
        return found

    def vendor(vid: str) -> dict[str, Any]:
        profile = app.store.get_vendor_profile(vid)
        return public_profile(profile, fleet_cost=side == "vendor")

    arguments: dict[str, Any] = copy.deepcopy(spec.get("args") or {})
    if spec.get("stream"):
        arguments["stream"] = stream(spec["stream"])
    if spec.get("streams"):
        arguments["streams"] = [stream(s) for s in spec["streams"]]
    if spec.get("vendor"):
        arguments["vendor"] = vendor(spec["vendor"])
    if spec.get("vendors"):
        arguments["vendors"] = [vendor(v) for v in spec["vendors"]]
    if spec.get("origin"):
        arguments["origin"] = list(emitter["location"])
    return arguments


def _run_tools(case: dict[str, Any], options: dict[str, Any]) -> list[dict[str, Any]]:
    """expect.tools의 호출을 새 세계에서 차례로 한다. 부르는 조직은 배출 기업(기본)이나 그 업체다."""
    specs = (case.get("expect") or {}).get("tools") or []
    if not specs:
        return []
    app = _make_app(case, options)
    observed = []
    for spec in specs:
        org = _emitter(case)
        if spec.get("org") == "vendor":
            org = str(spec.get("vendor"))
        try:
            result = call_tool(
                spec["tool"],
                _tool_arguments(app, case, spec),
                registry=app.registry,
                org_id=org,
                today=app.today,
                distance_provider=app.distance_provider,
            )
        except (ValueError, KeyError, TypeError) as error:
            observed.append({"tool": spec["tool"], "error": f"{type(error).__name__}: {error}"})
            continue
        claims = [{**c, "evidence": list(c["evidence"])} for c in result.get("claims") or []]
        body = " ".join("{{" + c["id"] + "}}" for c in claims)
        checked = validate_text(body, claims, app.registry, org_id=org, today=app.today)
        observed.append(
            {
                "tool": spec["tool"],
                "stream": spec.get("stream"),
                "status": result.get("status"),
                "checks": {c["key"]: c["status"] for c in result.get("checks") or []},
                "asks": [c["ask"] for c in result.get("checks") or [] if c.get("ask")],
                "texts": [c["text"] for c in result.get("checks") or [] if c.get("text")],
                "claims": {c["id"]: c["value"] for c in claims},
                "claimUnits": {c["id"]: c["unit"] for c in claims},
                "output": result.get("output") or {},
                "grounded": checked.ok,
                "groundedCodes": list(checked.codes),
            }
        )
    return observed


class _Session:
    """진단 한 건을 사례의 답과 행동대로 끝까지 돌린다."""

    def __init__(self, case: dict[str, Any], options: dict[str, Any]) -> None:
        self.case = case
        self.app = _make_app(case, options)
        self.emitter = _emitter(case)
        self.sid = ""
        self.view: dict[str, Any] = {}
        self.uploads: list[dict[str, Any]] = []
        self.approval_errors: list[dict[str, Any]] = []
        self.validated: dict[str, dict[str, Any]] = {}

    def _actions(self, when: str) -> None:
        for action in self.case.get("actions") or []:
            if action.get("when") != when:
                continue
            kind = action["action"]
            if kind == "upload_document":
                org = action.get("org") or self.emitter
                done = service.upload_document(
                    self.app,
                    org,
                    action.get("kind") or "contract",
                    action.get("filename") or "문서.txt",
                    str(action.get("text") or "").encode("utf-8"),
                    org,
                )
                self.uploads.append(
                    {
                        "org": org,
                        "blocked": done["blocked"],
                        "handoff": done["handoff"],
                        "shieldUnavailable": done["shieldUnavailable"],
                        "fields": len(done["fields"]),
                    }
                )
            elif kind == "patch_evidence":
                worlds.patch_evidence(self.app.registry, action)
            elif kind == "update_stream":
                service.update_stream(
                    self.app, self.emitter, action["stream"], action["patch"], self.emitter
                )
            elif kind == "approve_one":
                self._approve(action.get("vendor"), [action.get("side") or "emitter"])

    def _approve(self, vendor: str | None, sides: list[str]) -> None:
        threads = {t["id"]: t["vendorOrgId"] for t in self.view.get("threads") or []}
        for item in list(self.view.get("approvals") or []):
            vendor_id = threads.get(item["threadId"])
            if vendor is not None and vendor_id != vendor:
                continue
            for side in sides:
                org = self.emitter if side == "emitter" else vendor_id
                try:
                    service.approve(self.app, self.sid, item["threadId"], org)
                except service.ServiceError as error:
                    self.approval_errors.append(
                        {"vendor": vendor_id, "side": side, "code": error.code}
                    )
        self.view = service.get_view(self.app, self.sid, self.emitter)

    def _recheck(self) -> None:
        """새로 나간 메시지와 문서를 근거 검증기에 다시 넣는다(나간 직후의 근거 상태로 한 번만)."""
        names: dict[str, list[str]] = {}
        for raw in self.app.store.list_threads(session_id=self.sid):
            thread = Thread.from_dict(raw)
            names[thread.id] = thread_names(self.app.store, thread)
            for message in thread.messages:
                key = f"{thread.id}#{message.seq}"
                if key in self.validated:
                    continue
                result = validate_message(
                    message, self.app.registry, today=self.app.today, names=names[thread.id]
                )
                self.validated[key] = {
                    "ok": result.ok,
                    "codes": list(result.codes),
                    "recorded": bool((message.meta.get("validation") or {}).get("ok")),
                    "attempts": int((message.meta.get("validation") or {}).get("attempts") or 1),
                    "type": message.type,
                }
        for document in self.app.store.list_documents(self.sid):
            key = f"doc#{document['id']}"
            if key in self.validated:
                continue
            result = validate_text(
                document["text"],
                document["claims"],
                self.app.registry,
                org_id=self.emitter,
                today=self.app.today,
                allowed_tokens=(
                    *ALLOWED_TOKENS,
                    *name_tokens(names.get(str(document.get("threadId"))) or []),
                ),
            )
            self.validated[key] = {
                "ok": result.ok,
                "codes": list(result.codes),
                "recorded": bool((document.get("validation") or {}).get("ok")),
                "attempts": 1,
                "type": "document",
            }

    def _confirm_other(self, org: str) -> None:
        """다른 배출 기업의 진단을 먼저 확정까지 돌린다(같은 업체의 노선에 이미 묶인 물량)."""
        app = self.app
        view = service.run_session(app, service.create_session(app, org, org)["session"]["id"], org)
        sid = view["session"]["id"]
        for _ in range(MAX_ANSWER_ROUNDS):
            if view["session"]["status"] != "awaiting_answers":
                break
            for question in view["questions"]:
                if not question["answered"]:
                    service.answer_question(app, sid, question["id"], "unknown", org)
            view = service.run_session(app, sid, org)
        vendors = {t["id"]: t["vendorOrgId"] for t in view["threads"]}
        for item in view["approvals"]:
            service.approve(app, sid, item["threadId"], org)
            service.approve(app, sid, item["threadId"], vendors[item["threadId"]])

    def run(self) -> None:
        app, emitter = self.app, self.emitter
        answers = self.case.get("answers") or {}
        for other in self.case["world"].get("otherEmitters") or []:
            if other.get("confirmed"):
                self._confirm_other(str(other["id"]))
        self._actions("before_run")
        self.sid = service.create_session(app, emitter, emitter)["session"]["id"]
        self.view = service.run_session(app, self.sid, emitter)
        for _ in range(MAX_ANSWER_ROUNDS):
            if self.view["session"]["status"] != "awaiting_answers":
                break
            for question in self.view["questions"]:
                if question["answered"]:
                    continue
                key = f"{question['streamId']}.{question['field']}"
                value = answers.get(key, answers.get("*", "unknown"))
                service.answer_question(app, self.sid, question["id"], value, emitter)
            self.view = service.run_session(app, self.sid, emitter)
        self._recheck()
        if self.view["session"]["status"] == "awaiting_approval":
            self._actions("awaiting_approval")
            self.view = service.get_view(app, self.sid, emitter)
            if self.case.get("approve"):
                self._approve(None, ["emitter", "vendor"])
        self._recheck()
        if self.view["session"]["status"] == "confirmed":
            self._actions("after_confirm")
        self.view = service.get_view(app, self.sid, emitter)
        self._recheck()


# ── 관찰 ─────────────────────────────────────────────────────────────


def _value(claim: dict[str, Any] | None) -> Any:
    return None if claim is None else claim.get("value")


def _observe(session: _Session) -> dict[str, Any]:
    view = session.view
    result = view.get("result") or {}
    assignments = {}
    for item in result.get("assignments") or []:
        assignments[item["streamId"]] = {
            "vendor": item["vendorOrgId"],
            "route": item["route"],
            "status": item["status"],
            "checks": [c["key"] for c in item["checks"]],
            "currentAssumed": item["currentAssumed"],
            **{field: _value(item.get(field)) for field in _MONEY_FIELDS},
            "carbon": _value(item.get("carbon")),
            "alternatives": [a["vendorOrgId"] for a in item["alternatives"]],
        }
    threads = [
        {
            "vendor": t["vendorOrgId"],
            "status": t["status"],
            "streams": list(t["streamIds"]),
            "assigned": list(t["assignedStreamIds"]),
            "types": [m["type"] for m in t["messages"]],
            "turns": t["turns"],
            "reasonEvidence": list(t["reasonEvidence"]),
        }
        for t in view.get("threads") or []
    ]
    return {
        "session": {"status": view["session"]["status"]},
        "result": {
            "status": result.get("status"),
            "sensitivity": len(result.get("sensitivity") or []),
            "totalDifference": _value((result.get("totals") or {}).get("difference")),
        },
        "assignments": assignments,
        "threads": threads,
        "questions": [
            {"streamId": q["streamId"], "field": q["field"], "batch": q["batch"]}
            for q in view.get("questions") or []
        ],
        "unresolved": list(view.get("unresolved") or []),
        "stages": {str(s["stage"]): s["status"] for s in view.get("stages") or []},
        "streams": {
            s["id"]: {
                "grade": s["grade"]["value"],
                "gradeBasis": s["grade"]["basis"],
                "material": s["material"],
                "origin": s["origin"],
                "form": s["form"],
                "currentAssumed": (s.get("currentTreatment") or {}).get("assumed"),
            }
            for s in view.get("streams") or []
        },
        "uploads": session.uploads,
        "approvals": [
            {
                "vendor": next(
                    (t["vendorOrgId"] for t in view["threads"] if t["id"] == a["threadId"]), None
                ),
                "emitter": a["emitter"],
                "vendorApproved": a["vendor"],
                "blocked": a["blocked"],
            }
            for a in view.get("approvals") or []
        ],
        "approvalErrors": session.approval_errors,
        "documents": len(view.get("documents") or []),
    }


# ── 기대와 견주기 ────────────────────────────────────────────────────


class _Diff:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def same(self, path: str, expected: Any, actual: Any) -> None:
        if expected != actual:
            self.items.append({"path": path, "expected": expected, "actual": actual})

    def has(self, path: str, wanted: list[Any], actual: list[Any] | dict[str, Any]) -> None:
        missing = [w for w in wanted if w not in actual]
        if missing:
            self.items.append({"path": path, "expected": wanted, "actual": sorted(actual)})

    def lacks(self, path: str, unwanted: list[Any], actual: list[Any] | dict[str, Any]) -> None:
        present = [w for w in unwanted if w in actual]
        if present:
            self.items.append(
                {"path": path, "expected": f"없어야 함: {unwanted}", "actual": present}
            )


def _compare_tools(
    diff: _Diff, specs: list[dict[str, Any]], observed: list[dict[str, Any]]
) -> None:
    for index, (spec, seen) in enumerate(zip(specs, observed, strict=True)):
        path = f"tools[{index}].{spec['tool']}"
        if "error" in seen:
            diff.same(f"{path}.error", None, seen["error"])
            continue
        if "status" in spec:
            diff.same(f"{path}.status", spec["status"], seen["status"])
        for key, status in (spec.get("checks") or {}).items():
            diff.same(f"{path}.checks.{key}", status, seen["checks"].get(key))
        diff.lacks(f"{path}.checks", list(spec.get("absentChecks") or []), seen["checks"])
        for key, value in (spec.get("output") or {}).items():
            diff.same(f"{path}.output.{key}", value, seen["output"].get(key))
        for key, value in (spec.get("claims") or {}).items():
            diff.same(f"{path}.claims.{key}", value, seen["claims"].get(key))
        diff.lacks(f"{path}.claims", list(spec.get("absentClaims") or []), seen["claims"])
        if spec.get("noMoney"):
            money = [k for k, unit in seen["claimUnits"].items() if unit in MONEY_UNITS]
            diff.same(f"{path}.money", [], money)
        for needle in spec.get("askContains") or []:
            if not any(needle in ask for ask in seen["asks"]):
                diff.same(f"{path}.asks", f"'{needle}'가 든 질문", seen["asks"])
        for needle in spec.get("textContains") or []:
            if not any(needle in text for text in seen["texts"]):
                diff.same(f"{path}.texts", f"'{needle}'가 든 설명", seen["texts"])
        if spec.get("noAsks"):
            diff.same(f"{path}.asks", [], seen["asks"])
        diff.same(f"{path}.grounded", True, seen["grounded"])


def _compare(case: dict[str, Any], seen: dict[str, Any], tools: list[dict[str, Any]]) -> list[dict]:
    expect = case["expect"]
    diff = _Diff()
    _compare_tools(diff, expect.get("tools") or [], tools)
    if not seen:
        return diff.items
    for section in ("session", "result"):
        for key, value in (expect.get(section) or {}).items():
            if key == "sensitivity":  # 민감도가 있는지만 본다
                diff.same(f"{section}.sensitivity", bool(value), seen[section][key] > 0)
            else:
                diff.same(f"{section}.{key}", value, seen[section].get(key))
    for sid, wanted in (expect.get("assignments") or {}).items():
        actual = seen["assignments"].get(sid)
        if actual is None:
            diff.same(f"assignments.{sid}", wanted, None)
            continue
        for key, value in wanted.items():
            if key == "checks":
                diff.has(f"assignments.{sid}.checks", value, actual["checks"])
            elif key == "absentChecks":
                diff.lacks(f"assignments.{sid}.checks", value, actual["checks"])
            elif key == "alternatives":
                diff.same(f"assignments.{sid}.alternatives", sorted(value), sorted(actual[key]))
            else:
                diff.same(f"assignments.{sid}.{key}", value, actual.get(key))
    for index, wanted in enumerate(expect.get("threads") or []):
        matches = [
            t
            for t in seen["threads"]
            if t["vendor"] == wanted["vendor"] and t["status"] == wanted["status"]
        ]
        path = f"threads[{index}]"
        if not matches:
            actual = [
                {"vendor": t["vendor"], "status": t["status"], "streams": t["streams"]}
                for t in seen["threads"]
                if t["vendor"] == wanted["vendor"]
            ]
            diff.same(path, wanted, actual)
            continue
        found = matches[0]
        for key in ("streams", "assigned"):
            if key in wanted:
                diff.same(f"{path}.{key}", sorted(wanted[key]), sorted(found[key]))
        if "lastType" in wanted:
            diff.same(f"{path}.lastType", wanted["lastType"], (found["types"] or [None])[-1])
        if "hasTypes" in wanted:
            diff.has(f"{path}.types", wanted["hasTypes"], found["types"])
        if "noTypes" in wanted:
            diff.lacks(f"{path}.types", wanted["noTypes"], found["types"])
        if wanted.get("reasonEvidence"):
            diff.same(f"{path}.reasonEvidence", True, bool(found["reasonEvidence"]))
    questions = expect.get("questions")
    if questions is not None:
        asked = [[q["streamId"], q["field"]] for q in seen["questions"]]
        if "count" in questions:
            diff.same("questions.count", questions["count"], len(asked))
        diff.has("questions.asked", list(questions.get("asked") or []), asked)
        diff.lacks("questions.asked", list(questions.get("notAsked") or []), asked)
    if "unresolved" in expect:
        actual = sorted([u["streamId"], u["field"]] for u in seen["unresolved"])
        diff.has("unresolved", [list(x) for x in expect["unresolved"]], actual)
    for key, value in (expect.get("stages") or {}).items():
        diff.same(f"stages.{key}", value, seen["stages"].get(key))
    for sid, wanted in (expect.get("streams") or {}).items():
        for key, value in wanted.items():
            diff.same(f"streams.{sid}.{key}", value, (seen["streams"].get(sid) or {}).get(key))
    for index, wanted in enumerate(expect.get("uploads") or []):
        actual = seen["uploads"][index] if index < len(seen["uploads"]) else None
        for key, value in wanted.items():
            diff.same(f"uploads[{index}].{key}", value, (actual or {}).get(key))
    for index, wanted in enumerate(expect.get("approvals") or []):
        actual = next((a for a in seen["approvals"] if a["vendor"] == wanted["vendor"]), None)
        for key, value in wanted.items():
            diff.same(f"approvals[{index}].{key}", value, (actual or {}).get(key))
    if "documents" in expect:
        diff.same("documents", expect["documents"], seen["documents"])
    return diff.items


# ── 금지 사항 ────────────────────────────────────────────────────────


def _outgoing(session: _Session) -> list[dict[str, Any]]:
    """나간 글 전부: 메시지와 문서. {"from", "type", "text", "claims", "streams", "terms"}."""
    out = []
    for raw in session.app.store.list_threads(session_id=session.sid):
        for message in raw["messages"]:
            meta = message.get("meta") or {}
            out.append(
                {
                    "kind": "message",
                    "from": message["from"],
                    "vendor": raw["vendorOrgId"],
                    "threadId": raw["id"],
                    "type": message["type"],
                    "text": message["text"],
                    "claims": message["claims"],
                    "streams": message["streamIds"],
                    "terms": meta.get("terms") or {},
                }
            )
    for document in session.app.store.list_documents(session.sid):
        out.append(
            {
                "kind": "document",
                "from": session.emitter,
                "vendor": None,
                "threadId": document.get("threadId"),
                "type": document.get("kind"),
                "text": f"{document['text']}\n{document.get('rendered') or ''}",
                "claims": document["claims"],
                "streams": document.get("streamIds") or [],
                "terms": {},
            }
        )
    return out


def _result_claims(view: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if {"id", "kind", "value", "evidence"} <= set(node):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(view.get("result"))
    return found


def _forbidden(
    case: dict[str, Any], session: _Session | None, seen: dict[str, Any], tools: list[dict]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    outgoing = _outgoing(session) if session else []
    emitter = _emitter(case)

    def hit(item: dict[str, Any], detail: Any) -> None:
        out.append({"kind": item["kind"], "why": item.get("why"), "detail": detail})

    for item in case.get("forbidden") or []:
        kind = item["kind"]
        sid, vendor = item.get("stream"), item.get("vendor")
        if kind == "money":
            got = (seen.get("assignments") or {}).get(sid) or {}
            fields = item.get("fields") or _MONEY_FIELDS
            shown = {f: got.get(f) for f in fields if got.get(f) is not None}
            if shown:
                hit(item, {"assignment": shown})
            offers = [
                o["threadId"]
                for o in outgoing
                if any(i.get("streamId") == sid for i in o["terms"].get("items") or [])
            ]
            if offers:
                hit(item, {"offers": offers})
            for tool in tools:
                money = [
                    k for k, unit in (tool.get("claimUnits") or {}).items() if unit in MONEY_UNITS
                ]
                if tool.get("stream") == sid and money and item.get("tools", True):
                    hit(item, {"tool": tool["tool"], "claims": money})
        elif kind == "assigned":
            got = (seen.get("assignments") or {}).get(sid) or {}
            if got.get("vendor") and (vendor is None or got["vendor"] == vendor):
                hit(item, {"vendor": got["vendor"]})
        elif kind == "thread_opened":
            for thread in seen.get("threads") or []:
                if thread["vendor"] != vendor or thread["status"] == "not_opened":
                    continue
                if sid is None or sid in thread["streams"]:
                    hit(item, {"status": thread["status"], "streams": thread["streams"]})
        elif kind == "message_type":
            sender = emitter if item.get("from") == "emitter" else item.get("from")
            for o in outgoing:
                wanted = o["kind"] == "message" and o["type"] == item["type"]
                if wanted and (sender is None or o["from"] == sender):
                    hit(item, {"threadId": o["threadId"], "from": o["from"]})
        elif kind == "text":
            pattern = re.compile(item["pattern"])
            for o in outgoing:
                if item.get("from") and o["from"] != item["from"]:
                    continue
                found = pattern.search(o["text"])
                if found:
                    hit(item, {"in": o["kind"], "threadId": o["threadId"], "match": found.group(0)})
            for tool in [] if item.get("from") else tools:
                for text in [*(tool.get("asks") or []), *(tool.get("texts") or [])]:
                    found = pattern.search(text)
                    if found:
                        hit(item, {"in": tool["tool"], "match": found.group(0)})
        elif kind == "evidence_cited":
            prefix = item["evidence"]
            claims = [
                c
                for o in outgoing
                if not item.get("from") or o["from"] == item["from"]
                for c in o["claims"]
            ]
            if session and item.get("result", True) and not item.get("from"):
                claims += _result_claims(session.view)
            cited = sorted({e for c in claims for e in c["evidence"] if e.startswith(prefix)})
            if cited:
                hit(item, {"evidence": cited})
        elif kind == "verdict":
            if sid is None:
                actual = (seen.get("result") or {}).get("status")
            else:
                actual = ((seen.get("assignments") or {}).get(sid) or {}).get("status")
            if actual == item["status"]:
                hit(item, {"status": actual})
            for tool in tools:
                if item.get("tool") == tool["tool"] and tool.get("status") == item["status"]:
                    hit(item, {"tool": tool["tool"], "status": tool["status"]})
        elif kind == "confirmed":
            confirms = [o["threadId"] for o in outgoing if o["type"] == "confirm"]
            if (seen.get("session") or {}).get("status") == "confirmed" or confirms:
                hit(item, {"confirmMessages": confirms})
            if seen.get("documents"):
                hit(item, {"documents": seen["documents"]})
        elif kind == "tool_claim":
            for tool in tools:
                if tool["tool"] == item["tool"] and item["claim"] in (tool.get("claims") or {}):
                    hit(item, {"tool": tool["tool"], "claim": item["claim"]})
        elif kind == "tool_route":
            for tool in tools:
                route = (tool.get("output") or {}).get("route")
                if route in item["routes"]:
                    hit(item, {"tool": tool["tool"], "route": route})
        elif kind == "question":
            for question in seen.get("questions") or []:
                if question["streamId"] == sid and question["field"] == item["field"]:
                    hit(item, question)
    return out


def _invariants(session: _Session, seen: dict[str, Any]) -> list[dict[str, Any]]:
    """모든 사례에 걸리는 금지 사항(지시문 3장). 어기면 금지 위반으로 센다."""
    out: list[dict[str, Any]] = []
    app, store = session.app, session.app.store

    def hit(rule: str, why: str, detail: Any) -> None:
        out.append({"kind": f"invariant:{rule}", "why": why, "detail": detail})

    policy = QuestionPolicy()
    approvals = {a["threadId"]: a for a in store.list_approvals(session.sid)}
    for raw in store.list_threads(session_id=session.sid):
        vendor = raw["vendorOrgId"]
        profile = store.get_vendor_profile(vendor)
        guard = profile.get("guardrails") or {}
        if raw["status"] != "not_opened":
            for sid in raw["streamIds"]:
                if permit_match(store.get_stream(sid), profile)[0] is not True:
                    hit(
                        "permit",
                        "허가 품목이 맞지 않는 업체와는 대화를 열지 않는다",
                        {"vendor": vendor, "stream": sid},
                    )
        if raw["turns"] > raw["maxTurns"]:
            hit(
                "turns", "스레드당 턴 상한을 넘지 않는다", {"vendor": vendor, "turns": raw["turns"]}
            )
        for message in raw["messages"]:
            meta = message.get("meta") or {}
            if not (meta.get("validation") or {}).get("ok"):
                hit(
                    "validated",
                    "검증기를 통과한 메시지만 나간다",
                    {"vendor": vendor, "seq": message["seq"]},
                )
            if message["type"] == "confirm":
                approval = approvals.get(raw["id"]) or {}
                if not (approval.get("emitter") and approval.get("vendor")):
                    hit("confirm", "양쪽 승인 전에는 확정하지 않는다", {"vendor": vendor})
            terms = meta.get("terms") or {}
            if message["from"] != vendor or message["type"] not in rails.ENFORCED_TYPES:
                continue
            for item in terms.get("items") or []:
                single = {
                    "unitPriceKrwPerKg": item.get("unitPriceKrwPerKg"),
                    "days": terms.get("days"),
                    "pickupsPerMonth": terms.get("pickupsPerMonth"),
                    "conditions": terms.get("conditions"),
                }
                codes = [v.code for v in rails.check_terms(guard, single)]
                if codes:
                    hit(
                        "guardrail",
                        "업체 측 에이전트는 가드레일 밖의 조건을 내지 않는다",
                        {"vendor": vendor, "seq": message["seq"], "codes": codes},
                    )
                stream = store.get_stream(str(item.get("streamId")))
                if rails.auto_decline_reason(guard, stream):
                    hit(
                        "auto_decline",
                        "자동 거절 조건에 걸린 흐름에는 조건을 내지 않는다",
                        {"vendor": vendor, "stream": stream["id"]},
                    )
    batches: dict[int, int] = {}
    pairs = set()
    for question in seen.get("questions") or []:
        batches[question["batch"]] = batches.get(question["batch"], 0) + 1
        pair = (question["streamId"], question["field"])
        if pair in pairs:
            hit("question_repeat", "같은 질문을 되풀이하지 않는다", list(pair))
        pairs.add(pair)
    if len(batches) > policy.max_batches or any(n > policy.max_per_batch for n in batches.values()):
        hit("question_limit", "담당자 질문은 묶음당 3개, 2묶음까지다", batches)
    for stream in store.list_streams(session.emitter):
        got = (seen.get("assignments") or {}).get(stream["id"]) or {}
        if stream.get("monthlyKg") is None and any(got.get(f) is not None for f in _MONEY_FIELDS):
            hit("unknown_volume", "발생량을 모르면 금액을 내지 않는다", {"stream": stream["id"]})
    others = [
        o for o in store.list_orgs(role="emitter") if o["id"] != session.emitter and o.get("name")
    ]
    for vendor in sorted({t["vendor"] for t in seen.get("threads") or []}):
        try:
            theirs = service.get_view(app, session.sid, vendor)
        except service.ServiceError:
            continue  # 열린 대화가 없는 업체는 진단을 볼 수 없다
        if any(s.get("currentTreatment") is not None for s in theirs["streams"]):
            hit("vendor_view", "현행 처리 단가는 업체에 보이지 않는다", {"vendor": vendor})
        for item in (theirs.get("result") or {}).get("assignments") or []:
            if any(item.get(f) is not None for f in ("monthlyNow", "difference")):
                hit("vendor_view", "현행 대비 손익은 업체에 보이지 않는다", {"vendor": vendor})
        if any(t["vendorOrgId"] != vendor for t in theirs["threads"]):
            hit("vendor_view", "업체는 다른 업체의 대화를 보지 못한다", {"vendor": vendor})
    dumped = json.dumps(session.view, ensure_ascii=False)
    for other in others:  # 배출 기업끼리는 서로의 데이터를 볼 수 없다
        if other["id"] in dumped or other["name"] in dumped:
            hit("emitter_boundary", "다른 배출 기업이 진단 뷰에 드러나지 않는다", other["id"])
    return out


# ── 사례 하나, 여러 건 ───────────────────────────────────────────────


def run_case(case: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    base = {
        "id": case["id"],
        "title": case.get("title"),
        "review": case.get("review"),
        "open": case.get("status") == "open",
        "openNote": case.get("openNote"),
        "tags": list(case.get("tags") or []),
    }
    session: _Session | None = None
    seen: dict[str, Any] = {}
    tools: list[dict[str, Any]] = []
    error = None
    try:
        tools = _run_tools(case, options)
        if case.get("session", True):
            session = _Session(case, options)
            session.run()
            seen = _observe(session)
    except Exception as failure:  # noqa: BLE001 - 사례 하나가 죽어도 나머지는 돌린다
        error = f"{type(failure).__name__}: {failure}"
    mismatches = [] if error else _compare(case, seen, tools)
    violations = [] if error else _forbidden(case, session, seen, tools)
    if session is not None and not error:
        violations += _invariants(session, seen)
    validated = session.validated if session else {}
    failed = {k: v["codes"] for k, v in validated.items() if not v["ok"]}
    return {
        **base,
        "error": error,
        "verdictMatch": error is None and not mismatches,
        "mismatches": mismatches,
        "forbiddenViolations": violations,
        "validator": {
            "checked": len(validated),
            "passed": sum(1 for v in validated.values() if v["ok"]),
            # 한 번 거부된 뒤 다시 써서 나간 글과, 두 번 거부되어 handoff로 넘어간 것을 따로 센다
            "rewritten": sum(
                1 for v in validated.values() if v["attempts"] > 1 and v["type"] != "handoff"
            ),
            "handedOff": sum(
                1 for v in validated.values() if v["attempts"] > 1 and v["type"] == "handoff"
            ),
            "failed": failed,
        },
        "observed": {**seen, "tools": tools},
    }


def _options(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    if hasattr(source, "store") and hasattr(source, "registry"):  # service.App
        return {
            "llm_factory": source.llm_factory,
            "distance_provider": source.distance_provider,
            "registry_source": source.registry,
            "model": model_label(source.llm_for("company", "", None)),
        }
    return {"registry_source": source}  # 근거 레지스트리


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    matched = sum(1 for r in results if r["verdictMatch"])
    checked = sum(r["validator"]["checked"] for r in results)
    passed = sum(r["validator"]["passed"] for r in results)
    violations = sum(len(r["forbiddenViolations"]) for r in results)
    reviews: dict[str, int] = {}
    for r in results:
        reviews[str(r["review"])] = reviews.get(str(r["review"]), 0) + 1
    closed = [r for r in results if not r["open"]]
    return {
        "caseCount": total,
        "reviewCounts": reviews,
        "draftCount": sum(n for label, n in reviews.items() if "초안" in label),
        "verdictMatched": matched,
        "verdictMatchRate": matched / total if total else None,
        "verdictMismatched": [r["id"] for r in results if not r["verdictMatch"]],
        "openCount": total - len(closed),
        "openCases": [r["id"] for r in results if r["open"]],
        "closedMatched": sum(1 for r in closed if r["verdictMatch"]),
        "closedCount": len(closed),
        "validatorChecked": checked,
        "validatorPassed": passed,
        "validatorPassRate": passed / checked if checked else None,
        "rewritten": sum(r["validator"]["rewritten"] for r in results),
        "handedOff": sum(r["validator"]["handedOff"] for r in results),
        "forbiddenViolationCount": violations,
        "forbiddenClean": violations == 0,
        "forbiddenViolated": [r["id"] for r in results if r["forbiddenViolations"]],
        "errorCount": sum(1 for r in results if r["error"]),
        "errors": [r["id"] for r in results if r["error"]],
    }


def run_cases(source: Any, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """사례를 차례로 돌린다. source는 service.App, 근거 레지스트리, 또는 None이다."""
    options = _options(source)
    results = [run_case(case, options) for case in cases]
    model = options.get("model") or {"provider": "rule", "name": "규칙 정책(모델 미연결)"}
    return {"model": model, "summary": summarize(results), "results": results}
