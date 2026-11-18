"""두 번째 실제 브라우저 QA(2026-11-18): 화면에서 가입한 배출 기업(주소만 있고 위치가 없다)이 PP 불량품의
오염도를 담당자 답 5%로 넣었을 때 업체 측 차례. 회사와 수치는 모두 가상이다.

- 위치가 없어 노선 비용 도구의 입력을 만들 수 없으면, 그 실패를 대화의 도구 기록(memo)에 남긴다. 다음
  차례의 안내가 같은 도구를 다시 부르게 하지 않는다(모델 호출 한 번이 줄고, 그 호출에서 모델이 길게
  생각하다 잘리는 일이 없어진다). 위치가 생기면 다시 부른다.
- 조건을 내는 차례의 안내는 측정값이 아닌 값(담당자 답, 추정값)의 claim ID를 짚고, 그 값은 {{id}}로만
  쓰며 terms.conditions에는 안내의 조건 문장만 쓰라고 적는다.
"""

from __future__ import annotations

from app.v3.agents import vendor
from app.v3.agents.draft import AgentTools, Trace
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.registry import InMemoryRegistry

NO_LOCATION = "배출 기업의 위치가 없어 노선을 계산할 수 없습니다."
ARGS = {"streamIds": ["st_a"]}


def _tools(memo: list, located: bool) -> AgentTools:
    def resolver(name, arguments):
        if not located:
            raise ValueError(NO_LOCATION)
        return {"streamIds": ["st_a"], "located": True}, "extra_a", {}, False

    registry = InMemoryRegistry()
    return AgentTools(
        registry=registry,
        org_id="org_v",
        today="2026-11-18",
        actor="vendor_agent",
        pool=ClaimPool(registry, "org_v"),
        trace=Trace(),
        resolver=resolver,
        allowed=("route_extra_cost",),
        memo=memo,
    )


def test_a_tool_that_cannot_get_its_inputs_is_remembered_for_the_next_turn():
    memo: list = []
    first = _tools(memo, located=False)
    assert first.run("route_extra_cost", ARGS, "model:x")["status"] == "error"
    first.run("route_extra_cost", ARGS, "model:x")
    assert len(memo) == 1 and memo[0]["failed"] and memo[0]["result"] is None
    # 다음 차례: 아직 위치가 없다. 부른 것으로 보이고(안내가 다시 부르게 하지 않는다), 실행 기록은 없다
    second = _tools(memo, located=False)
    assert vendor._tried(second.results, "route_extra_cost", streamIds=["st_a"])
    assert vendor._result(second.results, "route_extra_cost", streamIds=["st_a"]) is None
    assert second.trace.events == [] and len(memo) == 1
    # 위치가 생기면 기억을 버리고 다시 부른다
    third = _tools(memo, located=True)
    assert third.results == [] and memo == []


def _guide(results, stream, facts):
    return vendor.guide(
        types=("question", "answer", "conditional", "decline"),
        stream_ids=("st_a",),
        streams={"st_a": stream},
        auto_declined={},
        results=results,
        criteria={"pc_b": {"maxContaminationPct": "pc_b_maxContaminationPct"}},
        offered=[],
        asked=[],
        days=["월"],
        facts=facts,
    )


def test_the_offer_turn_points_at_values_that_were_answered_or_estimated():
    one = {"streamId": "st_a"}
    ready = [
        {
            "tool": "match_permit",
            "arguments": one,
            "status": "calculated",
            "output": {"matched": True},
        },
        {
            "tool": "judge_route",
            "arguments": one,
            "status": "conditional",
            "output": {"acceptable": True, "criterionId": "pc_b"},
            "claims": {"unitPrice": "route_a_unitPrice"},
            "checks": [
                {"key": "contamination_basis", "status": "conditional"},
                {"key": "monthly_kg_basis", "status": "conditional"},
            ],
        },
        {"tool": "check_loading", "arguments": ARGS, "status": "calculated", "claims": {}},
    ]
    stream = {"id": "st_a", "name": "PP 사출 불량품", "monthlyKgBasis": "estimated"}
    facts = {"contaminationPct": "st_a_answeredContaminationPct", "monthlyKg": "st_a_monthlyKg"}
    # 노선 비용만 아직 부르지 않았으면 그것만 부르게 한다
    last = _guide(ready, stream, {"st_a": facts})[-1]
    assert last.startswith("받을 수 있는 흐름을 모아 route_extra_cost을 부른다")
    failed = {
        "tool": "route_extra_cost",
        "arguments": ARGS,
        "status": "error",
        "error": NO_LOCATION,
    }
    text = "\n".join(_guide([*ready, failed], stream, {"st_a": facts}))
    assert "오염도({{st_a_answeredContaminationPct}})는 배출 기업 담당자가 답한 값" in text
    assert "월 발생량({{st_a_monthlyKg}})은 추정값" in text
    assert "terms.conditions에는 위 조건 문장만 쓴다" in text
    assert (
        "반입 검수에서 오염도가 매입 기준 상한 {{pc_b_maxContaminationPct}} 이하로 확인될 것"
        in text
    )
    # 측정값이면 짚지 않는다
    measured = {"contaminationPct": "st_a_contaminationPct", "monthlyKg": "st_a_monthlyKg"}
    plain = "\n".join(
        _guide([*ready, failed], {**stream, "monthlyKgBasis": "measured"}, {"st_a": measured})
    )
    assert "담당자가 답한 값" not in plain and "추정값)" not in plain
