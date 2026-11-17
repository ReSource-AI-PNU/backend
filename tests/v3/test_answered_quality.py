"""담당자가 질문에 답한 오염도("대략 5%")를 다루는 방법. 브라우저 QA에서 본 경우를 그대로 옮겼다.

QA: 배출 기업이 PP 오염도를 "대략 5%"로 답했는데, 업체에 간 글에는 "오염도(측정): 5%"로 나갔고, 결과 한
장에는 "오염도 값이 없어 매입 기준과 대조하지 못했습니다"가 적혔으며, PP는 오염도 상한이 2%인 새길자원
A등급 기준으로 배정됐다. 고친 뒤:
- 답한 값은 어디서나 "담당자 답"이고 "측정"이 아니다(claim ID·이름, 문장).
- 매입 기준과 대조할 때 답한 값을 쓴다. 상한 안이면 조건부(반입 검수에서 확인), 상한을 넘으면 그 기준은
  맞지 않는다(새길자원은 A등급 대신 상한 8%인 B등급 기준으로만 볼 수 있다).
회사와 수치는 모두 가상이다.
"""

from __future__ import annotations

import json

from app.v3 import names, service
from app.v3.judge.common import criterion_checks, quality
from tests.v3.runtime_fixtures import SAEGIL, make_app

EMITTER = "org_qa_emitter"
STREAM = {
    "id": "st_qa_pp",
    "orgId": EMITTER,
    "material": "PP",
    "answers": {"contaminationPct": 5.0, "evidence": ["human:org_qa_emitter:st_qa_pp-오염도"]},
}


def test_answered_contamination_is_compared_with_the_criterion():
    q = quality(STREAM)
    assert q["contaminationPct"] == 5.0 and q["contaminationBasis"] == "answered"
    over = criterion_checks(STREAM, {"maxContaminationPct": 2}, q)
    assert [(key, status) for key, status, *_ in over] == [("contaminationPct", "unmet")]
    assert over[0][2] == "오염도(담당자가 답한 값)가 매입 기준 상한을 넘습니다."
    under = criterion_checks(STREAM, {"maxContaminationPct": 8}, q)
    assert [(key, status) for key, status, *_ in under] == [
        ("contaminationPct", "met"),
        ("contamination_basis", "conditional"),  # 측정값이 아니므로 반입 검수에서 확인한다
    ]
    # 측정값이 있으면 그것이 먼저다
    measured = {**STREAM, "measure": {"contaminationPct": 1.0, "evidence": ["measure:x"]}}
    assert quality(measured)["contaminationBasis"] == "measured"


def test_answered_values_are_named_as_answers():
    assert names.describe("st_qa_pp_answeredContaminationPct", 5.0) == (
        "오염도(담당자 답)",
        "contamination",
    )
    assert names.describe("st_qa_pp_answeredMoisturePct", 1.0)[0] == "수분(담당자 답)"
    assert names.describe("st_qa_pp_contaminationPct", 5.0)[0] == "오염도(측정)"


def _qa_run():
    app = make_app()
    app.store.put_org({"id": EMITTER, "name": "(주)QA정밀2202", "role": "emitter", "demo": False})
    service.update_stream(
        app,
        EMITTER,
        "st_qa_pp",
        {
            "name": "PP 사출 불량품",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "가공 전",
            "monthlyKg": 400,
        },
        EMITTER,
    )
    session_id = service.create_session(app, EMITTER, EMITTER)["session"]["id"]
    view = service.run_session(app, session_id, EMITTER)
    for _ in range(3):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if question["answered"]:
                continue
            value = "대략 5%" if question["field"] == "contaminationPct" else "모름"
            service.answer_question(app, session_id, question["id"], value, EMITTER)
        view = service.run_session(app, session_id, EMITTER)
    return app, view


def test_qa_case_answered_five_percent():
    app, view = _qa_run()
    assert view["session"]["status"] == "awaiting_approval"
    # 배출 기업 위치가 없어도 업체 측이 노선 비용 도구를 거듭 부르다 넘기지 않는다
    assert all(t["status"] != "handoff" for t in view["threads"])
    stream = view["streams"][0]
    assert stream["contamination"]["id"] == "st_qa_pp_answeredContaminationPct"
    assert stream["contamination"]["label"] == "오염도(담당자 답)"

    # 업체에 간 글: 답한 값을 "측정"이라고 하지 않는다
    for thread in view["threads"]:
        for message in thread["messages"]:
            claims = {c["id"]: c for c in message["claims"]}
            for claim_id, claim in claims.items():
                if "ContaminationPct" in claim_id and claim.get("role") == "contamination":
                    assert claim_id.endswith("answeredContaminationPct"), claim_id
                    assert claim["label"] == "오염도(담당자 답)"
            assert "answeredContaminationPct}}(측정)" not in message["text"]

    # 새길자원(A등급 상한 2%, B등급 상한 8%)은 A등급 기준으로 대조하지 않는다
    session = app.store.get_session(view["session"]["id"])
    routes = [
        entry["result"]
        for memo in session["work"]["threadTools"].values()
        for entry in memo
        if entry["name"] == "judge_route"
    ]
    saegil = [r for r in routes if r["output"]["vendorOrgId"] == SAEGIL]
    assert saegil and all(r["output"]["criterionId"] == "pc_saegil_pp_b" for r in saegil)
    assert all(r["status"] != "calculated" for r in routes)  # 답한 값만으로는 확정하지 않는다

    # 결과 한 장: 오염도는 대조했고(값이 없다는 말이 없다), 측정값이 아니므로 조건부다
    result = view["result"]
    row = next(a for a in result["assignments"] if a["streamId"] == "st_qa_pp")
    assert row["status"] == "conditional"
    text = json.dumps(row, ensure_ascii=False)
    assert "오염도 값이 없어" not in text
    if row["vendorOrgId"] == SAEGIL:
        assert row["unitPrice"]["value"] == 180  # B등급 단가(A등급 320이 아니다)
