"""시연 시나리오를 규칙 정책으로 처음부터 끝까지 돌린다. 회사와 수치는 모두 가상이다."""

import copy
from dataclasses import replace

import pytest

from app.v3 import service
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.validator import validate_text
from app.v3.store import InMemoryStore
from tests.v3.runtime_fixtures import (
    FILM_MOISTURE,
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    TODAY,
    answer_all,
    approve_all,
    make_app,
    recheck,
    start,
    thread_of,
    to_approval,
)


@pytest.fixture(scope="module")
def demo():
    """(앱, 질문에서 멈춘 뷰, 승인 대기 뷰, 확정 뷰)"""
    app = make_app()
    asked = start(app)
    waiting = answer_all(app, asked)
    if waiting["session"]["status"] == "awaiting_answers":
        # 두 번째 묶음: 현행 처리비 질문(모름으로 답하면 혼합 소각 가정값을 쓴다)
        waiting = answer_all(app, waiting)
    done = approve_all(app, waiting)
    return app, asked, waiting, done


def value(claim):
    return None if claim is None else claim["value"]


def test_first_stop_is_a_batch_of_questions(demo):
    _, asked, _, _ = demo
    assert asked["session"]["status"] == "awaiting_answers"
    assert [s["id"] for s in asked["streams"]] == ["st_defect", "st_film", "st_purge", "st_sprue"]
    questions = asked["questions"]
    assert [(q["streamId"], q["field"], q["batch"]) for q in questions] == [
        ("st_defect", "moisturePct", 1),
        ("st_film", "moisturePct", 1),
        ("st_purge", "moisturePct", 1),
    ]
    assert all(not q["answered"] and q["text"] and q["id"].startswith("q_") for q in questions)
    assert asked["threads"] == [] and asked["result"] is None
    assert asked["stages"][0]["status"] == "running"
    assert {s["status"] for s in asked["stages"][1:]} == {"waiting"}
    sprue = asked["streams"][-1]
    assert sprue["grade"] == {
        "value": "A",
        "basis": "measured",
        "label": "측정 등급",
        "evidence": ["model:rsai-grade:v1:st_sprue-measured"],
    }
    assert value(sprue["monthlyKg"]) == 600 and sprue["monthlyKg"]["evidence"]
    assert sprue["currentTreatment"]["assumed"] is False
    film = asked["streams"][1]
    assert film["grade"]["value"] is None and film["currentTreatment"]["assumed"] is True
    assert film["currentTreatment"]["unitCost"]["evidence"][0].startswith("assumption:")


def test_film_does_not_open_with_miso_and_says_why(demo):
    _, _, waiting, _ = demo
    closed = thread_of(waiting, MISO, "not_opened")
    assert closed["status"] == "not_opened" and "st_film" in closed["streamIds"]
    assert "허가 품목" in closed["statusReason"] and "포장 필름" in closed["statusReason"]
    assert closed["reasonEvidence"] == ["doc:org_miso:허가증:폐플라스틱"]
    assert closed["messages"] == [] and closed["turns"] == 0
    # 필름을 받는 업체는 새길자원뿐이고, 남해열분해도 필름으로는 열리지 않는다
    assert "st_film" in thread_of(waiting, SAEGIL)["streamIds"]
    assert "st_film" in thread_of(waiting, NAMHAE, "not_opened")["streamIds"]
    edges = [e for e in waiting["graph"]["edges"] if (e["from"], e["to"]) == ("st_film", MISO)]
    assert (
        edges and edges[0]["status"] == "unmet" and edges[0]["evidence"] == closed["reasonEvidence"]
    )


def test_vendors_answer_with_offer_conditional_and_fee(demo):
    _, _, waiting, _ = demo
    saegil = thread_of(waiting, SAEGIL)
    assert [m["type"] for m in saegil["messages"]] == [
        "inquiry",
        "offer",
        "question",
        "conditional",
    ]
    offer = saegil["messages"][1]
    assert offer["streamIds"] == ["st_sprue"] and offer["stage"] == 5
    price = next(
        c for c in offer["claims"] if c["id"] == offer["meta"]["terms"]["items"][0]["claimId"]
    )
    assert (price["value"], price["unit"]) == (320, "원/kg")
    assert offer["meta"]["terms"]["days"] == ["화", "목"]
    assert saegil["messages"][3]["streamIds"] == ["st_defect", "st_film"]

    namhae = thread_of(waiting, NAMHAE)
    assert [m["type"] for m in namhae["messages"]] == [
        "inquiry",
        "offer",
        "question",
        "conditional",
    ]
    fee = namhae["messages"][1]
    fee_claim = next(c for c in fee["claims"] if c["unit"] == "원/kg")
    assert fee_claim["value"] < 0 and "처리비" in fee["text"]  # 음수 단가는 처리비다
    assert fee["meta"]["excluded"][0]["streamId"] == "st_defect"
    assert "금속 인서트" in fee["meta"]["excluded"][0]["reason"]
    conditional = namhae["messages"][3]
    assert conditional["streamIds"] == ["st_purge"] and conditional["meta"]["terms"]["conditions"]
    assert namhae["vendorModel"]["configured"] == {"provider": "ollama", "name": "qwen2.5:7b"}
    assert (
        namhae["vendorModel"]["name"] == "규칙 정책(모델 미연결)" and namhae["vendorModel"]["used"]
    )
    for thread in (saegil, namhae, thread_of(waiting, MISO)):
        assert thread["turns"] == 4 and thread["maxTurns"] == 12
        assert thread["vendorName"].startswith("(주)")


def test_result_sheet_assigns_every_stream(demo):
    app, _, waiting, _ = demo
    assert waiting["session"]["status"] == "awaiting_approval"
    result = waiting["result"]
    by_stream = {a["streamId"]: a for a in result["assignments"]}
    assert {s: a["vendorOrgId"] for s, a in by_stream.items()} == {
        "st_sprue": SAEGIL,
        "st_film": SAEGIL,
        "st_defect": MISO,
        "st_purge": NAMHAE,
    }
    assert {s: a["route"] for s, a in by_stream.items()} == {
        "st_sprue": "sell",
        "st_film": "sell",
        "st_defect": "sell",
        "st_purge": "treat",
    }
    assert by_stream["st_sprue"]["status"] == "calculated"
    assert by_stream["st_purge"]["status"] == "conditional" and result["status"] == "conditional"
    sprue = by_stream["st_sprue"]
    assert value(sprue["unitPrice"]) == 320 and value(sprue["monthlyAfter"]) == 320 * 600
    assert value(sprue["difference"]) == value(sprue["monthlyAfter"]) - value(sprue["monthlyNow"])
    assert {a["vendorOrgId"] for a in sprue["alternatives"]} == {MISO, NAMHAE}
    totals = result["totals"]
    assert value(totals["monthlyAfter"]) == sum(
        value(a["monthlyAfter"]) for a in result["assignments"]
    )
    assert value(totals["difference"]) == value(totals["monthlyAfter"]) - value(
        totals["monthlyNow"]
    )
    # 탄소는 공식 배출계수(환경성적표지)로 낸다. 열분해는 계수가 없어 그 흐름만 빠진다
    assert by_stream["st_purge"]["carbon"] is None
    assert value(totals["carbon"]) == pytest.approx(
        sum(value(by_stream[s]["carbon"]) for s in ("st_sprue", "st_film", "st_defect"))
    )
    assert value(by_stream["st_sprue"]["carbon"]) == pytest.approx(
        (0.0186 - 3.412554039) * 600 / 1000, abs=1e-6
    )
    # 순비용의 내역: 매각 수익(처리비면 음수) − 운반비 − 전처리 인건비
    assert sprue["transportPaidBy"] == "vendor" and value(sprue["transport"]) == 0
    assert value(sprue["revenue"]) == 320 * 600 and sprue["labor"] is None
    purge = by_stream["st_purge"]
    assert purge["transportPaidBy"] == "emitter" and value(purge["transport"]) == 30000
    assert value(purge["revenue"]) == -80 * 250
    assert value(purge["monthlyAfter"]) == -80 * 250 - 30000
    assert by_stream["st_purge"]["currentAssumed"] is True
    assumed = {a["id"]: a for a in result["assumptions"]}
    assert all(set(a) == {"id", "label", "value", "unit"} for a in result["assumptions"])
    assert "assumption:등급기준@v1" in assumed
    cost = next(a for key, a in assumed.items() if key.startswith("assumption:current.cost."))
    assert cost["label"] and cost["unit"] == "원/kg" and cost["value"] > 0
    assert cost["value"] == app.registry.get(cost["id"], HANBIT).value
    assert {s["streamId"] for s in result["sensitivity"]} == {"st_film", "st_purge"}
    # 참고 시세는 보여 주기만 한다
    assert [p["form"] for p in result["referencePrices"]] == ["압축", "펠렛", "플레이크"]
    assert all("손익 계산에는 쓰지" in p["note"] for p in result["referencePrices"])

    # 결과 한 장의 모든 claim이 검증기를 통과한다
    claims = []
    for item in result["assignments"]:
        claims += [
            item[k]
            for k in (
                "unitPrice",
                "monthlyAfter",
                "monthlyNow",
                "difference",
                "carbon",
                "carbonValue",
            )
        ]
        claims += [alt[k] for alt in item["alternatives"] for k in ("unitPrice", "monthlyAfter")]
        claims += [c for check in item["checks"] for c in check.get("claims") or []]
    claims += list(totals.values()) + [s["difference"] for s in result["sensitivity"]]
    claims += [p["price"] for p in result["referencePrices"]]
    claims = list({c["id"]: c for c in claims if c}.values())
    body = " ".join("{{" + c["id"] + "}}" for c in claims)
    checked = validate_text(body, claims, app.registry, org_id=HANBIT, today=TODAY)
    assert len(claims) > 20 and checked.ok, checked.to_dict()
    assert [
        (a["threadId"], a["emitter"], a["vendor"], a["blocked"]) for a in waiting["approvals"]
    ] == [
        (thread_of(waiting, MISO)["id"], False, False, False),
        (thread_of(waiting, NAMHAE)["id"], False, False, False),
        (thread_of(waiting, SAEGIL)["id"], False, False, False),
    ]
    assert waiting["documents"] == []
    assert all(m["type"] != "confirm" for t in waiting["threads"] for m in t["messages"])


def test_stage_bar_and_graph(demo):
    _, _, waiting, _ = demo
    assert [(s["stage"], s["key"]) for s in waiting["stages"]] == [
        (1, "identify"),
        (2, "feasibility"),
        (3, "regulation"),
        (4, "matching"),
        (5, "cost-carbon"),
        (6, "proposal"),
    ]
    status = {s["stage"]: s["status"] for s in waiting["stages"]}
    assert status[1] == "needs_check"  # 모른다고 답한 수분이 남아 있다
    assert status[3] == "needs_check"  # 조항 적용 여부는 담당자 확인으로 남긴다
    assert status[2] == status[4] == status[5] == status[6] == "done"
    graph = waiting["graph"]
    kinds = {n["type"] for n in graph["nodes"]}
    assert kinds == {"stream", "criterion", "vendor", "evidence"}
    assert {e["status"] for e in graph["edges"]} == {"met", "conditional", "unmet"}
    node_ids = {n["id"] for n in graph["nodes"]}
    assert all(e["from"] in node_ids and e["to"] in node_ids for e in graph["edges"])
    assert any(n["id"].startswith("law:") for n in graph["nodes"])


def test_both_approvals_confirm_and_make_two_documents(demo):
    *_, done = demo
    assert done["session"]["status"] == "confirmed"
    agreed = [t for t in done["threads"] if t["status"] == "confirmed"]
    assert {t["vendorOrgId"] for t in agreed} == {SAEGIL, NAMHAE, MISO}
    for thread in agreed:
        last = thread["messages"][-1]
        assert last["type"] == "confirm" and last["from"] == HANBIT and last["stage"] == 6
        assert last["streamIds"] == thread["assignedStreamIds"] and last["claims"]
        assert thread["turns"] == 4  # confirm은 턴으로 세지 않는다
        docs = [d for d in done["documents"] if d["threadId"] == thread["id"]]
        assert [d["kind"] for d in docs] == ["request", "plan"]
        assert all(d["validation"]["ok"] and d["claims"] and "{{" in d["text"] for d in docs)
        assert "요청서" in docs[0]["title"] and "이행 계획서" in docs[1]["title"]
        assert "계약서" not in docs[0]["title"] and "{{" not in docs[0]["rendered"]
    assert all(a["emitter"] and a["vendor"] for a in done["approvals"])
    # 열려 있던 다른 제안은 대안으로 남는다
    assert {t["status"] for t in done["threads"]} == {"confirmed", "not_opened"}


def test_validator_pass_rate_is_one_hundred_percent(demo):
    app, _, waiting, done = demo
    for view in (waiting, done):
        checked, passed = recheck(app, view)
        assert checked == passed
    checked, passed = recheck(app, done)
    assert checked == 12 + 3 + 6 and passed == checked  # 대화 12, confirm 3, 문서 6
    for thread in done["threads"]:
        for message in thread["messages"]:
            assert message["validation"]["ok"] is True and message["validation"]["attempts"] == 1


def test_execution_record_shows_what_really_happened(demo):
    _, _, _, done = demo
    events = done["events"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert {e["kind"] for e in events} >= {"message", "tool_call", "validation", "approval"}
    tools = [e for e in events if e["kind"] == "tool_call"]
    assert {e["detail"]["stage"] for e in tools} == {1, 2, 3, 4, 5, 6}
    assert {e["detail"]["tool"] for e in tools} == {
        "identify_stream",
        "judge_route",
        "judge_reuse",
        "check_loading",
        "check_requirements",
        "match_permit",
        "find_vendors",
        "cost_carbon",
        "route_extra_cost",
        "optimize_assignment",
    }
    # 누가 불렀는지 그대로 남는다: 진행기, 규칙 정책(모델 아님)
    assert {e["detail"]["calledBy"] for e in tools} == {"orchestrator", "model:rule"}
    assert {e["actor"] for e in tools} == {"tool", "company_agent", "vendor_agent"}
    sent = [e for e in events if e["kind"] == "message"]
    total = sum(len(t["messages"]) for t in done["threads"])
    assert len(sent) == total
    assert all(e["detail"]["model"]["provider"] == "rule" for e in sent)
    assert sum(1 for e in events if e["kind"] == "approval") == 6


def test_run_is_idempotent_at_every_stop():
    app = make_app()
    asked = start(app)
    sid = asked["session"]["id"]
    again = service.run_session(app, sid, HANBIT)
    assert again["events"] == asked["events"] and again["questions"] == asked["questions"]
    waiting = answer_all(app, asked)
    again = service.run_session(app, sid, HANBIT)
    assert again["events"] == waiting["events"] and again["questions"] == waiting["questions"]
    waiting = answer_all(app, waiting)  # 두 번째 묶음(현행 처리비)
    again = service.run_session(app, sid, HANBIT)
    assert again["events"] == waiting["events"] and again["threads"] == waiting["threads"]
    first = service.approve(app, sid, waiting["approvals"][0]["threadId"], HANBIT)
    repeat = service.approve(app, sid, waiting["approvals"][0]["threadId"], HANBIT)
    assert repeat["approvals"] == first["approvals"]
    assert first["session"]["status"] == "awaiting_approval" and first["documents"] == []
    done = approve_all(app, service.get_view(app, sid, HANBIT))
    again = service.run_session(app, sid, HANBIT)
    assert again["session"]["status"] == "confirmed" and again["events"] == done["events"]
    assert len(again["documents"]) == 6


def test_question_policy_three_per_batch_two_batches():
    app = make_app()
    stream = app.store.get_stream("st_sprue")
    stream.update(measure=None, photo=None)  # 네 번째 후보를 만든다
    app.store.put_stream(stream)
    view = start(app)
    sid = view["session"]["id"]
    assert len(view["questions"]) == 3 and {q["batch"] for q in view["questions"]} == {1}
    service.answer_question(app, sid, view["questions"][0]["id"], "모름", HANBIT)
    half = service.run_session(app, sid, HANBIT)  # 답이 남아 있으면 진행하지 않는다
    assert half["session"]["status"] == "awaiting_answers" and len(half["questions"]) == 3
    with pytest.raises(service.ServiceError) as error:
        service.answer_question(app, sid, view["questions"][1]["id"], "많음", HANBIT)
    assert error.value.code == "INVALID_ANSWER" and error.value.status == 422
    for question in view["questions"][1:]:
        service.answer_question(app, sid, question["id"], "unknown", HANBIT)
    assert service.get_view(app, sid, HANBIT)["session"]["status"] == "idle"
    second = service.run_session(app, sid, HANBIT)
    new = [q for q in second["questions"] if not q["answered"]]
    assert second["session"]["status"] == "awaiting_answers"
    # 품질 질문이 먼저고, 남는 자리에 현행 처리비 질문이 들어간다
    assert [(q["streamId"], q["field"], q["batch"]) for q in new] == [
        ("st_sprue", "contaminationPct", 2),
        ("st_film", "currentCost", 2),
        ("st_purge", "currentCost", 2),
    ]
    for question in new:
        service.answer_question(app, sid, question["id"], "unknown", HANBIT)
    final = service.run_session(app, sid, HANBIT)
    # 두 묶음을 다 썼다. 모르는 값은 확인 필요로 남기고 진행한다
    assert final["session"]["status"] == "awaiting_approval"
    assert len(final["questions"]) == 6 and all(
        q["answered"] and q["unknown"] for q in final["questions"]
    )
    # 현행 처리비는 몰라도 가정값으로 견주므로 "확인 필요"로 남기지 않는다
    assert all(u["field"] != "currentCost" for u in final["unresolved"])
    assert {(u["streamId"], u["field"]) for u in final["unresolved"]} >= {
        ("st_sprue", "contaminationPct")
    }
    assert all(s["grade"]["value"] is None for s in final["streams"])
    sprue = next(a for a in final["result"]["assignments"] if a["streamId"] == "st_sprue")
    assert sprue["status"] == "conditional"
    assert recheck(app, final)[0] == recheck(app, final)[1]


def test_answered_moisture_becomes_evidence_and_grade():
    app = make_app()
    view = to_approval(app)
    film = next(s for s in view["streams"] if s["id"] == "st_film")
    assert value(film["moisture"]) == FILM_MOISTURE
    assert film["moisture"]["evidence"] == ["human:org_hanbit:st_film-수분"]
    assert film["grade"]["value"] == "B" and film["grade"]["basis"] == "photo_estimate"
    defect = next(s for s in view["streams"] if s["id"] == "st_defect")
    assert defect["moisture"] is None and defect["unknownFields"] == ["moisturePct"]
    inquiry = thread_of(view, SAEGIL)["messages"][0]
    assert "모른다고 답했습니다" in inquiry["text"]
    assert any(c["evidence"] == ["human:org_hanbit:st_film-수분"] for c in inquiry["claims"])


def test_failed_evidence_check_blocks_approval():
    app = make_app()
    waiting = to_approval(app)
    sid = waiting["session"]["id"]
    thread = thread_of(waiting, SAEGIL)
    price_id = "doc:org_saegil:매입기준표:pc_saegil_pp_a-단가"
    original = app.registry.get(price_id)
    app.registry.put(replace(original, value=original.value - 100))  # 대화 뒤에 근거가 바뀌었다
    with pytest.raises(service.ServiceError) as error:
        service.approve(app, sid, thread["id"], HANBIT)
    assert error.value.code == "APPROVAL_BLOCKED" and error.value.status == 409
    view = service.get_view(app, sid, HANBIT)
    blocked = next(a for a in view["approvals"] if a["threadId"] == thread["id"])
    assert blocked["blocked"] is True and "근거 확인" in blocked["blockedReason"]
    assert blocked["emitter"] is False and blocked["vendor"] is False
    assert any(e["summary"] == "근거 확인 실패로 승인을 막았습니다." for e in view["events"])
    with pytest.raises(service.ServiceError):
        service.approve(app, sid, thread["id"], SAEGIL)
    # 근거가 다시 맞으면 풀린다
    app.registry.put(replace(app.registry.get(price_id), value=original.value))
    view = service.approve(app, sid, thread["id"], HANBIT)
    cleared = next(a for a in view["approvals"] if a["threadId"] == thread["id"])
    assert cleared["blocked"] is False and cleared["emitter"] is True
    other = thread_of(waiting, MISO)
    assert service.approve(app, sid, other["id"], MISO)["session"]["status"] == "awaiting_approval"


def strip_times(data):
    if isinstance(data, dict):
        return {
            k: strip_times(v) for k, v in data.items() if k not in ("at", "createdAt", "updatedAt")
        }
    if isinstance(data, list):
        return [strip_times(v) for v in data]
    return data


def test_rule_policy_runs_are_deterministic():
    views = []
    for _ in range(2):
        app = service.App(InMemoryStore(), InMemoryRegistry(), today=TODAY)  # 실제 시계
        service.seed(app)
        waiting = to_approval(app)
        views.append((copy.deepcopy(waiting), approve_all(app, waiting)))
    (first_waiting, first_done), (second_waiting, second_done) = views
    assert strip_times(first_waiting) == strip_times(second_waiting)
    assert strip_times(first_done) == strip_times(second_done)
    # reset 뒤에 다시 돌려도 같다
    service.reset_demo(app, "org_admin")
    assert strip_times(to_approval(app)) == strip_times(first_waiting)


def test_no_offer_means_nothing_to_approve():
    app = make_app()
    for vendor in (SAEGIL, NAMHAE, MISO):
        profile = app.store.get_vendor_profile(vendor)
        profile["permits"] = []
        app.store.put_vendor_profile(profile)
    view = to_approval(app)
    assert view["session"]["status"] == "idle" and view["approvals"] == []
    assert {t["status"] for t in view["threads"]} == {"not_opened"}
    assert all("확인하지 못해" in t["statusReason"] for t in view["threads"])
    assert {a["route"] for a in view["result"]["assignments"]} == {"keep"}
    assert next(s for s in view["stages"] if s["stage"] == 4)["status"] == "needs_check"
