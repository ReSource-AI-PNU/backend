"""독립 검토: 구현 지시문 3장의 동작 규칙을 "나쁜 모델"과 나쁜 입력으로 깨뜨려 본다.

ScriptedLLM이 규칙 정책의 답을 받아 일부러 망가뜨린다(숫자를 지어내는 모델, 풀에 없는 claim을
쓰는 모델, 가드레일 밖 조건을 내는 모델, 다른 배출 기업을 흘리는 모델, 승인 없이 확정하려는
모델, 끝없이 묻는 모델, 허가 없는 품목을 받겠다는 모델). 회사와 수치는 모두 가상이다.
결과 정리는 docs/v3/review-rules.md에 있다.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.v3 import service
from app.v3.a2a import guardrails as rails
from app.v3.a2a.message import Claim, Message
from app.v3.a2a.policy import check_disclosure, evidence_owner
from app.v3.agents import draft as draft_module
from app.v3.agents import orchestrator as orchestrator_module
from app.v3.agents import questions
from app.v3.agents.company import SYSTEM_PROMPT as COMPANY_PROMPT
from app.v3.agents.llm import LLMError
from app.v3.agents.vendor import SYSTEM_PROMPT as VENDOR_PROMPT
from app.v3.agents.vendor import VendorAgent, public_profile
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.evidence.validator import validate_message, validate_text
from app.v3.intake import csv_measure
from app.v3.intake.shields import PromptShieldsClient
from app.v3.judge.toolbox import TOOLS, call_tool
from tests.v3.runtime_fixtures import (
    DAON,
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    TODAY,
    answer_all,
    approve_all,
    briefing_of,
    draft,
    make_app,
    only_for,
    rule_fallback,
    scripted,
    start,
    thread_of,
    to_approval,
)

ROOT = Path(__file__).resolve().parents[2]
VENDORS = (SAEGIL, NAMHAE, MISO)
PRICE = "pc_saegil_pp_a_unitPrice"
# 모델이 지어낸 값의 표식. 시연 데이터와 근거 어디에도 없는 수다
FAKE = ("13,579", "13579", "１３５７９", "만삼천")
SENTENCE = "검토자가심은문장"


# ── 도우미 ───────────────────────────────────────────────────────────


def mutating(change, *, times=None, role_task=None):
    """규칙 정책의 답(도구 호출이 아닌 최종 답)을 change로 망가뜨리는 모델.

    times가 있으면 그 횟수만 망가뜨리고 그 뒤로는 규칙 정책 그대로 답한다.
    """
    left = {"n": times}

    def respond(messages, tools, schema):
        response = rule_fallback(messages, tools, schema)
        output = json.loads(response["content"])
        briefing = briefing_of(messages)
        if output.get("toolCalls"):
            return response
        if role_task and briefing.get("task") not in role_task:
            return response
        if left["n"] is not None:
            if left["n"] <= 0:
                return response
            left["n"] -= 1
        changed = change(copy.deepcopy(output), briefing)
        return {"content": json.dumps(changed, ensure_ascii=False)}

    return respond


def bad_app(role, org, change, *, times=None, role_task=None, **options):
    llm = scripted(*[mutating(change, times=times, role_task=role_task)] * 200, pad=0)
    return make_app(only_for(role, org, llm), **options), llm


def all_views(app, session_id, emitter=HANBIT):
    views = {emitter: service.get_view(app, session_id, emitter)}
    for vendor in VENDORS:
        try:
            views[vendor] = service.get_view(app, session_id, vendor)
        except service.ServiceError as error:
            assert error.status == 404
    return views


def dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def everything(app, session_id, emitter=HANBIT) -> str:
    """화면으로 나가는 것(모든 조직의 뷰)과 저장된 기록(스레드, 실행 기록, 문서, 진단 상태) 전부."""
    parts = [dump(view) for view in all_views(app, session_id, emitter).values()]
    parts.append(dump(app.store.list_threads(session_id=session_id)))
    parts.append(dump(app.store.list_events(session_id)))
    parts.append(dump(app.store.list_documents(session_id)))
    parts.append(dump(app.store.get_session(session_id)))
    return "\n".join(parts)


def claims_in(node, out=None):
    """뷰 어디에 있든 claim 모양({id, kind, value, evidence})인 것을 모두 모은다."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if {"id", "kind", "value", "evidence"} <= set(node) and isinstance(node["evidence"], list):
            out.append(node)
        for value in node.values():
            claims_in(value, out)
    elif isinstance(node, list):
        for value in node:
            claims_in(value, out)
    return out


def ungrounded(app, view, viewer):
    """뷰의 claim 가운데 '뷰를 보는 조직' 기준으로 다시 검증했을 때 통과하지 못하는 것."""
    bad = []
    for claim in claims_in(view):
        one = {k: claim.get(k) for k in ("id", "kind", "value", "unit", "evidence")}
        result = validate_text(
            "{{" + claim["id"] + "}}", [one], app.registry, org_id=viewer, today=TODAY
        )
        if not result.ok:
            bad.append((claim["id"], result.codes))
    return bad


def names_of(app, thread):
    return orchestrator_module.thread_names(app.store, thread)


def unvalidated_messages(app, session_id):
    """저장된 스레드의 메시지를 다시 검증해 통과하지 못하는 것(본문 밖 문장 포함)."""
    bad = []
    for raw in app.store.list_threads(session_id=session_id):
        for item in raw["messages"]:
            message = Message.from_dict({**item, "threadId": raw["id"]})
            result = validate_message(
                message, app.registry, today=TODAY, names=names_of(app, raw)
            )
            if not result.ok or (item.get("meta") or {}).get("validation", {}).get("ok") is not True:
                bad.append((raw["id"], item["seq"], item["type"], result.codes))
    return bad


def assert_clean(app, session_id, *marks, emitter=HANBIT):
    """지어낸 값이 어디에도 없고, 나간 메시지·문서·뷰의 claim이 모두 근거 검증을 통과한다."""
    text = everything(app, session_id, emitter)
    for mark in (*FAKE, SENTENCE, *marks):
        assert mark not in text, mark
    assert unvalidated_messages(app, session_id) == []
    for viewer, view in all_views(app, session_id, emitter).items():
        assert ungrounded(app, view, viewer) == [], viewer
        for document in view["documents"]:
            for org in (emitter, viewer):
                checked = validate_text(
                    document["text"],
                    document["claims"],
                    app.registry,
                    org_id=org,
                    today=TODAY,
                    allowed_tokens=_doc_tokens(app, document),
                )
                assert checked.ok, (document["kind"], org, checked.codes)


def _doc_tokens(app, document):
    from app.v3.evidence.validator import ALLOWED_TOKENS, name_tokens

    return (*ALLOWED_TOKENS, *name_tokens(names_of(app, app.store.get_thread(document["threadId"]))))


def run_to_end(app, emitter=HANBIT):
    """승인 대기까지 가고, 승인할 대화가 있으면 양쪽 모두 승인한다. (진단 ID, 배출 기업 뷰)."""
    view = to_approval(app, emitter)
    session_id = view["session"]["id"]
    vendors = {t["id"]: t["vendorOrgId"] for t in view["threads"]}
    for item in view["approvals"]:
        for org in (emitter, vendors[item["threadId"]]):
            try:
                service.approve(app, session_id, item["threadId"], org)
            except service.ServiceError as error:
                assert error.status in (404, 409)
    return session_id, service.get_view(app, session_id, emitter)


def rejections_in(app, session_id):
    found = []
    for event in app.store.list_events(session_id):
        found += (event.get("detail") or {}).get("rejections") or []
    for raw in app.store.list_threads(session_id=session_id):
        for item in raw["messages"]:
            found += ((item.get("meta") or {}).get("validation") or {}).get("rejections") or []
    return found


def raises(code, call, *args):
    with pytest.raises(service.ServiceError) as error:
        call(*args)
    assert error.value.code == code, error.value.to_dict()
    return error.value


# ── 규칙 1. 근거 강제 ────────────────────────────────────────────────


def _number_in_text(output, briefing):
    output["text"] += f" {SENTENCE} 추가로 kg당 13,579원을 더 드립니다."
    return output


def _hangul_number(output, briefing):
    output["text"] += f" {SENTENCE} 단가는 만삼천 원입니다."
    return output


def _fullwidth_number(output, briefing):
    output["text"] += f" {SENTENCE} 단가는 １３５７９원입니다."
    return output


def _number_as_identifier(output, briefing):
    output["text"] += f" {SENTENCE} 단가는 st_13579원입니다."
    return output


def _claim_outside_pool(output, briefing):
    output["text"] += f" {SENTENCE} 특별 단가는 {{{{special_13579}}}}입니다."
    output["claimRefs"] = [*(output.get("claimRefs") or []), "special_13579"]
    return output


def _claims_written_by_model(output, briefing):
    # 모델이 claims를 직접 지어 넣는다(값도 근거 ID도 그럴듯하게)
    output["claims"] = [
        {
            "id": "fake",
            "kind": "number",
            "value": 13579,
            "unit": "원/kg",
            "evidence": ["doc:org_saegil:매입기준표:pc_saegil_pp_a-단가"],
        }
    ]
    output["text"] += f" {SENTENCE} 특별 단가는 {{{{fake}}}}입니다."
    return output


def _invented_clause(output, briefing):
    output["text"] += (
        f' {SENTENCE} 폐기물관리법은 "누구든지 kg당 13,579원 이상을 지급하여야 한다"고 정합니다.'
    )
    return output


def _numbers_beside_the_text(output, briefing):
    # 본문은 그대로 두고, 화면에 함께 나가는 다른 칸에 값을 싣는다
    sid = (output.get("streamIds") or briefing["thread"]["streamIds"])[0]
    name = next(s["name"] for s in briefing["streams"] if s["id"] == sid)
    output["reason"] = f"{SENTENCE} 실제 단가는 13,579원입니다"
    output["excluded"] = [
        {"streamId": sid, "reason": "kg당 13,579원 미만이라 제외", "memo": "13579원", "n": 13579}
    ]
    if isinstance(output.get("terms"), dict):
        output["terms"].setdefault("conditions", []).append(f"{name}: 단가를 13,579원으로 낮출 것")
        output["terms"]["days"] = [*(output["terms"].get("days") or []), "13,579원 요일"]
    return output


BAD_DRAFTS = {
    "number_in_text": _number_in_text,
    "hangul_number": _hangul_number,
    "fullwidth_number": _fullwidth_number,
    "number_as_identifier": _number_as_identifier,
    "claim_outside_pool": _claim_outside_pool,
    "claims_written_by_model": _claims_written_by_model,
    "invented_clause": _invented_clause,
    "numbers_beside_the_text": _numbers_beside_the_text,
}


@pytest.mark.parametrize("kind", sorted(BAD_DRAFTS))
@pytest.mark.parametrize("role, org", [("vendor", SAEGIL), ("company", HANBIT)])
def test_rule1_a_stubborn_bad_model_never_gets_a_number_out(kind, role, org):
    """나쁜 모델이 끝까지 고치지 않으면 그 대화는 담당자에게 넘어가고, 지어낸 값은 어디에도 없다."""
    app, llm = bad_app(role, org, BAD_DRAFTS[kind])
    session_id, view = run_to_end(app)
    assert llm.calls, "나쁜 모델이 실제로 불렸어야 한다"
    assert_clean(app, session_id)
    saegil = thread_of(view, SAEGIL)
    assert saegil["status"] == "handoff"
    assert saegil["messages"][-1]["type"] == "handoff"
    assert all(a["vendorOrgId"] != SAEGIL for a in view["result"]["assignments"])
    # 실행 기록에는 거부한 사실(코드와 일반 문장)만 남는다
    rejected = rejections_in(app, session_id)
    assert rejected
    for item in rejected:
        assert item["detail"] == {} and item["claimId"] is None
        assert not re.search(r"\d", item["message"]), item


@pytest.mark.parametrize("kind", sorted(BAD_DRAFTS))
@pytest.mark.parametrize("role, org", [("vendor", SAEGIL), ("company", HANBIT)])
def test_rule1_a_rejected_draft_leaves_only_the_fact_of_rejection(kind, role, org):
    """한 번 틀리고 다시 쓴 경우: 나간 것은 다시 쓴 메시지뿐이고, 거부된 초안은 기록에 본문이 없다."""
    app, llm = bad_app(role, org, BAD_DRAFTS[kind], times=1)
    session_id, view = run_to_end(app)
    assert_clean(app, session_id)
    rewritten = [
        m
        for t in view["threads"]
        for m in t["messages"]
        if m["from"] == org and m["validation"]["attempts"] == 2
    ]
    assert len(rewritten) == 1 and rewritten[0]["validation"]["ok"] is True
    assert rewritten[0]["validation"]["rejections"]
    assert view["session"]["status"] == "confirmed"
    refused = [
        e
        for e in view["events"]
        if e["kind"] == "validation" and e["detail"].get("ok") is False
    ]
    assert refused and all("text" not in e["detail"] for e in refused)


def test_rule1_model_text_in_tool_arguments_does_not_reach_the_record():
    """도구 인자에 실은 값, 없는 도구 이름, 검색어의 숫자가 실행 기록으로 나가지 않는다."""
    state = {"done": False}

    def smuggle(messages, tools, schema):
        if state["done"]:
            return rule_fallback(messages, tools, schema)
        state["done"] = True
        calls = [
            {"name": "judge_route", "arguments": {"streamId": "st_sprue", "price": "13,579원"}},
            {"name": "set_price_13579", "arguments": {"unitPrice": 13579}},
            {"name": "search_clauses", "arguments": {"query": "단가 13,579원 조항"}},
            {"name": "optimize_assignment", "arguments": {"memo": "13579"}},
        ]
        return {"content": dump(draft("answer", "", toolCalls=calls))}

    app = make_app(only_for("vendor", SAEGIL, scripted(*[smuggle] * 200, pad=0)))
    session_id, view = run_to_end(app)
    assert state["done"]
    assert_clean(app, session_id)
    failed = [e for e in view["events"] if e["detail"].get("status") == "error"]
    assert any(e["detail"]["tool"] is None for e in failed)  # 없는 도구는 이름도 옮기지 않는다
    assert view["session"]["status"] == "confirmed"


def test_rule1_values_without_evidence_stay_null_and_cannot_be_filled_in():
    """담당자도 모르는 값은 null(확인 필요)로 남고, 모델이 그 자리를 채워 보내지 못한다."""

    def fill_in(output, briefing):
        if any("moisturePct" in (s.get("unknownFields") or []) for s in briefing["streams"]):
            output["text"] += " 불량 사출품의 수분은 0.5%입니다."
        return output

    app, _ = bad_app("company", HANBIT, fill_in, times=1, role_task=("inquiry",))
    session_id, view = run_to_end(app)
    assert "0.5%" not in everything(app, session_id)
    by_id = {s["id"]: s for s in view["streams"]}
    for sid in ("st_defect", "st_purge"):
        assert by_id[sid]["moisture"] is None and "moisturePct" in by_id[sid]["unknownFields"]
        assert by_id[sid]["grade"]["value"] is None and by_id[sid]["grade"]["label"] == "확인 필요"
        assert {"streamId": sid, "field": "moisturePct"} in view["unresolved"]
    assert view["stages"][0]["status"] == "needs_check"
    # 모르는 값이 걸린 배정은 확정이 아니다
    for item in view["result"]["assignments"]:
        if item["streamId"] in ("st_defect", "st_purge"):
            assert item["status"] != "calculated"
    assert_clean(app, session_id)


def test_rule1_every_claim_validates_for_whoever_is_looking(confirmed_demo):
    """배출 기업 뷰와 업체 뷰 각각에서, 뷰의 모든 claim을 보는 조직 기준으로 다시 검증한다."""
    app, session_id = confirmed_demo
    views = all_views(app, session_id)
    assert set(views) == {HANBIT, *VENDORS}
    for viewer, view in views.items():
        claims = claims_in(view)
        assert len(claims) > 20, viewer
        assert ungrounded(app, view, viewer) == [], viewer
        for claim in claims:  # 값이 있는 claim에는 근거가 있다
            assert claim["value"] is None or claim["evidence"], (viewer, claim["id"])
    assert_clean(app, session_id)
    kinds = sorted(d["kind"] for d in views[HANBIT]["documents"])
    assert kinds == ["plan", "plan", "plan", "request", "request", "request"]


def test_rule1_assumptions_are_shown_as_assumptions(confirmed_demo):
    """가정값에 기댄 값은 뷰에서 가정값으로 드러난다(현행 처리, 결과 한 장의 가정값 목록)."""
    app, session_id = confirmed_demo
    view = service.get_view(app, session_id, HANBIT)
    listed = {a["id"] for a in view["result"]["assumptions"]}
    assert listed and all(i.startswith("assumption:") for i in listed)
    for item in view["result"]["assumptions"]:
        assert app.registry.get(item["id"], HANBIT).kind == "assumption" and item["label"]
    # 결과 한 장의 claim이 직접 또는 계산 사슬로 기댄 가정값은 모두 목록에 있다
    used = set()
    for claim in claims_in(view["result"]):
        for evidence_id in claim["evidence"]:
            used |= _assumptions_behind(app, evidence_id)
    assert used and used <= listed, used - listed
    for stream in view["streams"]:
        current = stream["currentTreatment"]
        assumed = current["unitCost"]["evidence"][0].startswith("assumption:")
        assert current["assumed"] is assumed
        if assumed:
            assert current["method"] == "혼합 소각"
    # 근거 패널에서도 가정값이라고 나온다
    panel = service.get_evidence(app, sorted(listed)[0], HANBIT)["evidence"]
    assert panel["kind"] == "assumption" and panel["kindLabel"] == "가정값"


def _assumptions_behind(app, evidence_id, depth=6):
    try:
        found = app.registry.get(evidence_id, None)
    except EvidenceNotFound:
        return set()
    out = {found.id} if found.kind == "assumption" else set()
    if depth:
        for child in (found.meta or {}).get("inputEvidence") or []:
            out |= _assumptions_behind(app, str(child), depth - 1)
    return out


@pytest.fixture(scope="module")
def confirmed_demo():
    app = make_app()
    session_id, view = run_to_end(app)
    assert view["session"]["status"] == "confirmed"
    return app, session_id


# ── 규칙 2. 판단은 판단 도구가 한다 ──────────────────────────────────


def _calc_ids(app, session_id):
    return [
        (e["detail"].get("tool"), e["detail"].get("orgId"), tuple(e["detail"].get("evidence") or []))
        for e in app.store.list_events(session_id)
        if e["kind"] == "tool_call"
    ]


def _judgement(app, session_id):
    """모델이 쓴 글을 뺀 판정과 숫자: 결과 한 장, 흐름의 등급, 스레드 상태, 메시지의 종류와 claims."""
    view = service.get_view(app, session_id, HANBIT)
    return {
        "result": view["result"],
        "grades": [(s["id"], s["grade"]) for s in view["streams"]],
        "stages": view["stages"],
        "threads": [
            (
                t["vendorOrgId"],
                t["status"],
                t["assignedStreamIds"],
                [(m["type"], m["claims"], m["meta"]["terms"]) for m in t["messages"]],
            )
            for t in view["threads"]
        ],
        "documents": [(d["kind"], d["text"], d["claims"]) for d in view["documents"]],
    }


def _reworded(prefix, suffix):
    def change(output, briefing):
        if output.get("text"):
            output["text"] = f"{prefix}\n{output['text']}\n{suffix}"
        return output

    return change


def test_rule2_same_input_gives_the_same_verdict_and_calc_ids():
    runs = []
    for _ in range(2):
        app = make_app()
        session_id, _ = run_to_end(app)
        runs.append((_judgement(app, session_id), _calc_ids(app, session_id)))
    assert runs[0] == runs[1]
    assert sum(len(ids) for _, _, ids in runs[0][1]) > 30  # calc 근거가 실제로 남았다


def test_rule2_different_wording_does_not_change_verdict_or_numbers():
    """문장만 다른 두 모델(ScriptedLLM)과 규칙 정책이 같은 판정, 같은 숫자, 같은 calc 근거를 낸다."""
    plain = make_app()
    baseline_id, _ = run_to_end(plain)
    baseline = (_judgement(plain, baseline_id), _calc_ids(plain, baseline_id))
    wordings = [("안녕하세요.", "확인 부탁드립니다."), ("담당자께 알립니다.", "이상입니다.")]
    texts = []
    for prefix, suffix in wordings:
        llm = scripted(*[mutating(_reworded(prefix, suffix))] * 400, pad=0)
        app = make_app(lambda role, org, model, llm=llm: llm)
        session_id, view = run_to_end(app)
        assert view["session"]["status"] == "confirmed"
        assert (_judgement(app, session_id), _calc_ids(app, session_id)) == baseline
        texts.append([m["text"] for t in view["threads"] for m in t["messages"]])
        assert all(
            m["model"]["provider"] in ("scripted", "code")
            for t in view["threads"]
            for m in t["messages"]
        )
    assert texts[0] != texts[1]  # 문장은 정말로 달랐다


def test_rule2_tools_are_deterministic_under_a_different_hash_seed():
    """딕셔너리·집합 순서에 기대지 않는다: PYTHONHASHSEED를 바꿔 돌려도 판정과 calc 근거 ID가 같다."""
    code = (
        "import json, hashlib\n"
        "from tests.v3.test_rules_adversarial import run_to_end, _judgement, _calc_ids\n"
        "from tests.v3.runtime_fixtures import make_app\n"
        "app = make_app(); sid, _ = run_to_end(app)\n"
        "data = json.dumps([_judgement(app, sid), _calc_ids(app, sid)], ensure_ascii=False,"
        " sort_keys=True, default=str)\n"
        "print(hashlib.sha256(data.encode()).hexdigest())\n"
    )
    digests = set()
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")])
        done = subprocess.run(  # noqa: S603 - 같은 인터프리터로 이 저장소의 코드를 돌린다
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT
        )
        assert done.returncode == 0, done.stderr[-2000:]
        digests.add(done.stdout.strip().splitlines()[-1])
    assert len(digests) == 1


# ── 규칙 3·4. 세 상태, 현행 처리 가정과 민감도 ───────────────────────

STATES = {"calculated", "conditional", "needs_information"}


def test_rule3_three_states_reach_the_view_consistently():
    app = make_app()
    view = to_approval(app)
    session = app.store.get_session(view["session"]["id"])
    result = view["result"]
    statuses = [a["status"] for a in result["assignments"]]
    assert set(statuses) <= STATES and result["status"] in STATES
    order = ["calculated", "conditional", "needs_information"]
    assert result["status"] == max(statuses, key=order.index)  # 가장 나쁜 상태가 전체 상태다
    by_vendor = {t["vendorOrgId"]: t for t in view["threads"] if t["status"] != "not_opened"}
    for item in result["assignments"]:
        thread = by_vendor[item["vendorOrgId"]]
        route = orchestrator_module.memo_result(
            session["work"]["threadTools"][thread["id"]], "judge_route", streamId=item["streamId"]
        )
        assert route["status"] in STATES
        if route["status"] != "calculated":
            # 판단 도구가 확정하지 않은 것을 뷰가 확정으로 보여 주지 않는다
            assert item["status"] != "calculated"
            assert any(c["status"] == "conditional" for c in item["checks"])
        if item["status"] == "calculated":
            assert route["status"] == "calculated"
            assert not [c for c in item["checks"] if c["status"] == "unmet"]
        # 조건부의 "확인할 것"은 업체 측 에이전트에게 보낸 질문에 그대로 들어 있다
        sent = "\n".join(m["text"] for m in thread["messages"] if m["from"] == HANBIT)
        for check in route["checks"]:
            if check["status"] == "conditional" and check.get("ask"):
                assert check["ask"] in sent, (item["streamId"], check["key"])
    # 업체가 보는 뷰에서도 같은 상태다
    for vendor in VENDORS:
        seen = service.get_view(app, view["session"]["id"], vendor)["result"]
        for item in seen["assignments"]:
            mine = next(a for a in result["assignments"] if a["streamId"] == item["streamId"])
            assert item["status"] == mine["status"]


def test_rule3_needs_information_is_not_shown_as_decided():
    """받겠다는 업체가 없으면 그 흐름은 확정이 아니라 needs_information이다."""
    app = make_app()
    for vendor in VENDORS:  # 어느 업체도 필름을 받지 못한다
        profile = app.store.get_vendor_profile(vendor)
        profile["permits"] = [p for p in profile["permits"] if p["wasteItem"] != "포장재"]
        app.store.put_vendor_profile(profile)
    view = to_approval(app)
    film = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_film")
    assert film["vendorOrgId"] is None and film["unitPrice"] is None
    assert view["result"]["status"] in ("conditional", "needs_information")
    assert all(
        "st_film" not in t["streamIds"] for t in view["threads"] if t["status"] != "not_opened"
    )


def test_rule4_missing_current_treatment_uses_mixed_incineration_and_sensitivity():
    app = make_app()
    view = to_approval(app)
    streams = {s["id"]: s for s in view["streams"]}
    assumed = {"st_film", "st_purge"}  # 시연 데이터에서 현행 처리 입력이 없는 흐름
    for sid, stream in streams.items():
        current = stream["currentTreatment"]
        assert current["assumed"] is (sid in assumed)
        if sid in assumed:
            assert current["method"] == "혼합 소각"
            assert current["unitCost"]["evidence"][0].startswith("assumption:current.cost.")
    result = view["result"]
    for item in result["assignments"]:
        assert item["currentAssumed"] is (item["streamId"] in assumed)
        keys = [c["key"] for c in item["checks"]]
        assert ("current_assumed" in keys) is (item["streamId"] in assumed)
    assert {s["streamId"] for s in result["sensitivity"]} == assumed
    for item in result["sensitivity"]:
        assert item["difference"]["value"] is not None and item["difference"]["evidence"]
        assert "현행" in item["label"]
    labels = [a["label"] for a in result["assumptions"]]
    assert any("혼합 소각" in label for label in labels)
    # 업체에게는 현행 처리와 민감도가 보이지 않는다
    for vendor in VENDORS:
        seen = service.get_view(app, view["session"]["id"], vendor)
        assert all(s["currentTreatment"] is None for s in seen["streams"])
        assert seen["result"]["sensitivity"] == [] and seen["result"]["assumptions"] == []


def test_rule4_entered_current_treatment_replaces_the_assumption():
    app = make_app()
    service.update_stream(
        app,
        HANBIT,
        "st_film",
        {"currentTreatment": {"method": "위탁 소각", "unitCostKrwPerKg": 310}},
        HANBIT,
    )
    view = to_approval(app)
    film = next(s for s in view["streams"] if s["id"] == "st_film")
    assert film["currentTreatment"]["assumed"] is False
    assert film["currentTreatment"]["unitCost"]["value"] == 310
    assert film["currentTreatment"]["unitCost"]["evidence"][0].startswith("human:org_hanbit:")
    item = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_film")
    assert item["currentAssumed"] is False
    assert "st_film" not in {s["streamId"] for s in view["result"]["sensitivity"]}


# ── 규칙 5. 최소 물량과 적재 ─────────────────────────────────────────


def _loading(app, streams, vendor=SAEGIL, **profile_patch):
    profile = public_profile(app.store.get_vendor_profile(vendor), fleet_cost=False)
    profile["fleet"] = {**profile["fleet"], **profile_patch}
    result = call_tool(
        "check_loading",
        {"streams": streams, "vendor": profile},
        registry=app.registry,
        org_id=HANBIT,
        today=TODAY,
    )
    checks = {c["key"]: c["status"] for c in result["checks"]}
    values = {c["id"]: c["value"] for c in result["claims"]}
    return result, checks, values


def _stream(app, stream_id, **patch):
    return {**app.store.get_stream(stream_id), **patch}


@pytest.mark.parametrize("monthly_kg", [100, 600])
def test_rule5_minimum_volume_and_loading_ignore_origin(monthly_kg):
    """PIR이든 PCR이든 출처를 모르든, 최소 물량과 적재 검사는 똑같이 걸린다."""
    app = make_app()
    seen = []
    for origin in ("PIR", "PCR", None):
        result, checks, values = _loading(
            app, [_stream(app, "st_sprue", origin=origin, monthlyKg=monthly_kg)]
        )
        seen.append((result["status"], checks, values))
        assert "min_volume" in checks and "loading" in checks
    assert seen[0] == seen[1] == seen[2]
    assert (seen[0][1]["min_volume"] == "met") is (monthly_kg >= 500)


def test_rule5_streams_to_the_same_vendor_are_merged_in_weight_and_volume():
    app = make_app()
    sprue = _stream(app, "st_sprue", monthlyKg=300, origin="PIR")
    defect = _stream(app, "st_defect", monthlyKg=300, origin="PCR")
    for alone in (sprue, defect):  # 따로 보면 둘 다 최소 물량에 못 미친다
        assert _loading(app, [alone])[1]["min_volume"] != "met"
    result, checks, values = _loading(app, [sprue, defect])
    assert checks["min_volume"] == "met"
    assert values["totalKg"] == pytest.approx(600)
    expected_m3 = 300 / sprue["bulkDensityKgPerM3"] + 300 / defect["bulkDensityKgPerM3"]
    assert values["totalM3"] == pytest.approx(expected_m3)
    assert values["perPickupM3"] == pytest.approx(expected_m3 / values["pickupsPerMonth"])
    # 순서를 바꿔도 같은 값이다
    again, again_checks, again_values = _loading(app, [defect, sprue])
    assert (again_checks, again_values) == (checks, values)
    # 다른 공장의 흐름은 합치지 않는다
    with pytest.raises(ValueError, match="한 공장"):
        _loading(app, [sprue, app.store.get_stream("st_daon_pp")])


def test_rule5_volume_can_fail_when_weight_fits():
    app = make_app()
    film = _stream(app, "st_film", monthlyKg=600)  # 가볍고 부피가 큰 흐름
    _, fits, values = _loading(app, [film])
    assert fits["loading"] == "met"
    tight = values["perPickupM3"] / 2
    result, checks, values = _loading(app, [film], capacityM3=tight)
    assert values["perPickupKg"] < values["capacityKg"]  # 무게로는 실린다
    assert values["perPickupM3"] > tight
    assert checks["loading"] != "met" and result["status"] != "calculated"
    # 밀도를 모르면 부피를 확인하지 못했다고 남긴다(충족으로 치지 않는다)
    unknown = _stream(app, "st_film", monthlyKg=600, bulkDensityKgPerM3=None)
    result, checks, values = _loading(app, [unknown])
    assert "loading_volume" in checks and checks["loading_volume"] != "met"
    assert "totalM3" not in values and result["status"] != "calculated"


def test_rule5_result_sheet_carries_the_merged_checks(confirmed_demo):
    app, session_id = confirmed_demo
    view = service.get_view(app, session_id, HANBIT)
    saegil = [a for a in view["result"]["assignments"] if a["vendorOrgId"] == SAEGIL]
    assert {a["streamId"] for a in saegil} == {"st_film", "st_sprue"}  # PCR 필름과 PIR 스프루
    totals = set()
    for item in saegil:
        checks = {c["key"]: c for c in item["checks"]}
        assert {"min_volume", "loading"} <= set(checks)
        total = next(c for c in checks["min_volume"]["claims"] if c["role"] == "totalKg")
        totals.add(total["value"])
    assert totals == {900}  # 두 흐름을 합친 물량으로 검사했다
