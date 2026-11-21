"""업체가 보는 뷰와 문서에 배출 기업의 현행 처리 방법·현행 단가·그것에서 나온 값이 없다.

회사와 수치는 모두 가상이다. "탄소 변화(현행 혼합 소각 대비…)"처럼 현행 처리에 견준 값은 배출
기업만 보는 결과 한 장에만 둔다.
"""

from __future__ import annotations

import json
import re

import pytest

from app.v3 import service
from app.v3.agents.pool import is_private
from tests.v3.runtime_fixtures import (
    HANBIT,
    MISO,
    NAMHAE,
    SAEGIL,
    approve_all,
    make_app,
    to_approval,
)

VENDORS = (SAEGIL, NAMHAE, MISO)
METHOD = "위탁 열분해(가상)"  # 시연 데이터에 없는 현행 처리 방법 이름
COST = 173.5  # 담당자가 넣은 현행 단가(가상). 필름 월 300kg이면 현행 월 손익은 -52,050원이다
# 그 단가와 거기서 나온 금액의 숫자. 근거 ID의 16진 지문과 우연히 겹치지 않게 앞뒤를 본다
ENTERED = re.compile(r"(?<![0-9a-f.])(173\.5|52,?050|97,?050)(?![0-9a-f])")
CURRENT_ROLES = (
    "currentUnitCost",
    "monthlyNow",
    "difference",
    "sensitivity",
    "carbon",
    "carbonValue",
)
CURRENT_ID = re.compile(
    r"assumption:current\.|위탁계약서|처리단가|현행처리단가|"
    r"calc:[a-z_]+:[0-9a-f]+:(currentUnitCost|monthlyNow|difference|sensitivity\w*|"
    r"carbonDifference|kauValue|s\d+(MonthlyNow|Difference|Carbon)|total(Now|Difference|Carbon))"
)


def claims_in(node):
    """뷰 안의 claim 꼴 객체(id, kind, value, evidence)를 모두 찾는다."""
    if isinstance(node, dict):
        if {"id", "kind", "value", "evidence"} <= set(node):
            yield node
        for value in node.values():
            yield from claims_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from claims_in(value)


def private_claims(view):
    """배출 기업 뷰에서 현행 처리와 그것에서 나온 값의 claim."""
    found = []
    for stream in view["streams"]:
        current = stream["currentTreatment"] or {}
        if current.get("unitCost"):
            found.append(current["unitCost"])
    result = view["result"] or {}
    for item in result.get("assignments") or []:
        found += [item[k] for k in ("monthlyNow", "difference", "carbon", "carbonValue") if item[k]]
    totals = result.get("totals") or {}
    found += [totals[k] for k in ("monthlyNow", "difference", "carbon") if totals.get(k)]
    found += [s["difference"] for s in result.get("sensitivity") or []]
    return found


def prepared(entered: bool):
    app = make_app()
    if entered:
        # 담당자가 현행 처리를 직접 넣은 흐름(방법 이름과 단가가 시연 데이터와 다르다)
        service.update_stream(
            app,
            HANBIT,
            "st_film",
            {"currentTreatment": {"method": METHOD, "unitCostKrwPerKg": COST}},
            HANBIT,
        )
    return app


@pytest.mark.parametrize("entered", [True, False])
@pytest.mark.parametrize("confirmed", [False, True])
def test_vendor_view_has_nothing_from_the_emitters_current_treatment(entered, confirmed):
    app = prepared(entered)
    view = to_approval(app)
    if confirmed:
        view = approve_all(app, view)
        assert view["session"]["status"] == "confirmed" and len(view["documents"]) == 9
    session_id = view["session"]["id"]
    mine = service.get_view(app, session_id, HANBIT)
    private = private_claims(mine)
    # 값의 출처가 되는 근거(첫 번째 근거)의 ID. 업체 뷰 어디에도 실리면 안 된다
    sources = {c["evidence"][0] for c in private}
    methods = {s["currentTreatment"]["method"] for s in mine["streams"]}
    assert "혼합 소각" in methods and (METHOD in methods) == entered
    assert len(sources) > 8
    if entered:
        assert ENTERED.search(json.dumps(mine, ensure_ascii=False))  # 배출 기업은 본다
    for vendor in VENDORS:
        theirs = service.get_view(app, session_id, vendor)
        text = json.dumps(theirs, ensure_ascii=False)
        # 방법 이름과 "현행"이라는 말 자체가 없다
        assert "현행" not in text and "소각" not in text and METHOD not in text
        assert not CURRENT_ID.search(text), CURRENT_ID.search(text).group(0)
        assert not ENTERED.search(text), ENTERED.search(text).group(0)
        assert not [source for source in sources if source in text]
        claims = list(claims_in(theirs))
        assert claims
        for claim in claims:
            assert claim.get("role") not in CURRENT_ROLES, claim
            assert not sources & set(claim["evidence"]), claim
            assert not any(e.startswith("assumption:current.") for e in claim["evidence"])
            for evidence_id in claim["evidence"]:
                assert not (
                    evidence_id.startswith("calc:") and is_private(evidence_id.rsplit(":", 1)[-1])
                )
        assert all(s["currentTreatment"] is None for s in theirs["streams"])
        totals = (theirs["result"] or {}).get("totals") or {}
        assert all(value is None for value in totals.values())
        if confirmed:
            assert theirs["documents"], vendor
            for document in theirs["documents"]:
                assert "탄소" not in document["text"] and "탄소" not in document["rendered"]
                assert all("carbon" not in c["id"].lower() for c in document["claims"])


def test_plan_keeps_its_other_lines_and_the_emitter_still_sees_carbon_on_the_result():
    app = prepared(False)
    view = approve_all(app, to_approval(app))
    plans = [d for d in view["documents"] if d["kind"] == "plan"]
    assert len(plans) == 3
    for plan in plans:
        assert "수거 횟수: " in plan["rendered"] and "반출" in plan["rendered"]
        assert "탄소" not in plan["rendered"] and "현행" not in plan["rendered"]
        assert plan["validation"]["ok"] is True
    assert any("반입 전 확인: " in plan["rendered"] for plan in plans)
    # 탄소 판단은 배출 기업만 보는 결과 한 장에 남는다. 원문 경계를 확인한 배출계수가 없어 지금은 값 대신
    # 산정 보류와 그 까닭이다(2026-11-21). 0이나 가정값으로 채우지 않는다
    for item in view["result"]["assignments"]:
        assert item["carbon"] is None and item["carbonValue"] is None
        held = next(c for c in item["checks"] if c["key"] == "carbon")
        assert held["status"] == "conditional" and held["text"]
    sid = view["session"]["id"]
    for vendor in {t["vendorOrgId"] for t in view["threads"] if t["status"] == "confirmed"}:
        theirs = service.get_view(app, sid, vendor)["result"]
        assert all(c["key"] != "carbon" for a in theirs["assignments"] for c in a["checks"])


def test_a_vendor_cannot_quote_values_compared_with_the_current_treatment():
    # 업체 측과 회사 측의 claim 풀에는 현행 처리에 견준 값(탄소 변화, 그 환산액)이 들어가지 않는다
    assert is_private("carbonDifference") and is_private("kauValue")
    assert is_private("currentUnitCost") and is_private("sensitivityA")
    assert not is_private("unitPrice") and not is_private("monthlyAfter")
    app = prepared(False)
    view = to_approval(app)
    for thread in view["threads"]:
        for message in thread["messages"]:
            for claim in message["claims"]:
                assert claim.get("role") not in CURRENT_ROLES
