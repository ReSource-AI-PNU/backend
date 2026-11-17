"""근거의 출처는 화면 이름으로 보인다. 도구 ID(judge_route 등)와 서비스 이름은 따로 둔다.

브라우저 QA에서 근거 패널에 "출처 · 판단 도구 judge_route", "optimize_assignment"가 그대로 보였다.
회사와 수치는 모두 가상이다.
"""

from __future__ import annotations

import json
import re

from app.v3 import names, service
from tests.v3.runtime_fixtures import HANBIT, make_app, to_approval

TOOL_ID = re.compile(r"\b[a-z]+_[a-z_]+\b")


def test_every_tool_has_a_korean_label():
    from app.v3.judge.toolbox import TOOLS

    for tool in TOOLS:
        label = names.tool_label(tool)
        assert label and not TOOL_ID.search(label), tool


def test_source_labels_hide_machine_names():
    assert names.source_label("calc", "판단 도구 judge_route", {"tool": "judge_route"}) == (
        "경로 판정·매입 기준 대조"
    )
    assert names.source_label("calc", "판단 도구 optimize_assignment") == "최적 배정 계산"
    assert names.source_label("doc", "price.pdf (Document Intelligence 추출, org_x 확인)") == (
        "price.pdf · 문서에서 읽고 담당자가 확인한 값"
    )
    assert names.source_label("model", "탐지 모델 resource-ai-detector v1 · a.jpg") == (
        "사진 탐지 모델 · a.jpg"
    )
    assert names.source_label("ref", "한국환경공단 가격조사") == "한국환경공단 가격조사"


def test_evidence_payloads_carry_labels_and_keep_ids_apart():
    app = make_app()
    view = to_approval(app, HANBIT)
    cited = {
        evidence_id
        for thread in view["threads"]
        for message in thread["messages"]
        for claim in message["claims"]
        for evidence_id in claim["evidence"]
    }
    for row in view["result"]["assignments"]:
        for key in ("unitPrice", "monthlyAfter", "difference"):
            if row.get(key):
                cited.update(row[key]["evidence"])
    calc = 0
    for evidence_id in sorted(cited):
        shown = service.get_evidence(app, evidence_id, HANBIT)
        for item in (shown["evidence"], *shown["chain"]):
            if item.get("hidden") or item.get("missing"):
                continue
            assert not TOOL_ID.search(item["source"]), item["source"]
            assert "Document Intelligence" not in item["source"]
            if item["kind"] == "calc":
                calc += 1
                assert item["tool"] in names.TOOL_LABELS
                assert item["toolLabel"] == names.TOOL_LABELS[item["tool"]]
                assert item["sourceRaw"] == f"판단 도구 {item['tool']}"
    assert calc > 0
    # 실행 기록의 도구 호출에도 화면 이름이 붙는다
    events = json.dumps(view["events"], ensure_ascii=False)
    assert '"toolLabel": "최적 배정 계산"' in events
