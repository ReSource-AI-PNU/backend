"""도구 등록부. 두 에이전트가 같은 도구를 MCP 서버 하나로 쓴다."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.v3.judge.assign import optimize_assignment
from app.v3.judge.economics import cost_carbon, route_extra_cost
from app.v3.judge.identify import identify_stream
from app.v3.judge.regulation import check_requirements, match_permit, search_clauses
from app.v3.judge.route import check_loading, judge_reuse, judge_route
from app.v3.judge.vendors import find_vendors
from app.v3.textutil import josa


@dataclass(frozen=True)
class ToolSpec:
    name: str
    stage: int
    description: str
    input_schema: dict
    fn: Callable[..., dict]


OBJ, ARR, NUM, STR = {"type": "object"}, {"type": "array"}, {"type": "number"}, {"type": "string"}
POINT = {"type": "array", "items": NUM}


def _schema(required: dict, optional: dict | None = None) -> dict:
    return {
        "type": "object",
        "properties": {**required, **(optional or {})},
        "required": list(required),
        "additionalProperties": False,
    }


_SPECS = [
    ToolSpec(
        "identify_stream",
        1,
        "흐름의 재질·출처·형태를 정리하고 등급을 낸다. 측정값이 사진 추정보다 우선한다.",
        _schema({"stream": OBJ}, {"moisturePct": NUM}),
        identify_stream,
    ),
    ToolSpec(
        "judge_route",
        2,
        "흐름의 경로를 확정·조건부·확인 필요로 판정하고 업체 매입 기준과 대조한다.",
        _schema({"stream": OBJ}, {"vendor": OBJ}),
        judge_route,
    ),
    ToolSpec(
        "judge_reuse",
        2,
        "공정 재투입 후보인지 보고, 신재 단가와 다시 넣을 수 있는 양이 있으면 절감액을 계산한다.",
        _schema({"stream": OBJ}),
        judge_reuse,
    ),
    ToolSpec(
        "check_loading",
        2,
        "같은 업체로 가는 한 공장의 흐름을 합쳐 최소 물량과 적재(무게·부피)를 검사한다.",
        _schema({"streams": ARR, "vendor": OBJ}, {"pickupsPerMonth": NUM}),
        check_loading,
    ),
    ToolSpec(
        "search_clauses",
        3,
        "법령 근거에서 조항을 찾아 원문 그대로 인용한다.",
        _schema({"query": STR}, {"limit": {"type": "integer"}}),
        search_clauses,
    ),
    ToolSpec(
        "check_requirements",
        3,
        "흐름과 경로에 걸리는 조항을 찾아 담당자 확인 항목으로 낸다.",
        _schema({"stream": OBJ, "route": STR}, {"topics": ARR}),
        check_requirements,
    ),
    ToolSpec(
        "match_permit",
        3,
        "업체 허가 품목과 흐름을 대조한다. 맞지 않으면 대화를 열지 않는다.",
        _schema({"stream": OBJ, "vendor": OBJ}),
        match_permit,
    ),
    ToolSpec(
        "find_vendors",
        4,
        "허가 품목, 매입 기준, 거리로 업체를 찾고 걸러진 이유를 함께 돌려준다.",
        _schema({"stream": OBJ, "vendors": ARR}, {"origin": POINT, "maxKm": NUM}),
        find_vendors,
    ),
    ToolSpec(
        "cost_carbon",
        5,
        "매입액, 운송비, 현행 대비 차이와 탄소를 계산한다.",
        _schema(
            {"stream": OBJ, "vendor": OBJ},
            {
                "origin": POINT,
                "pickupsPerMonth": NUM,
                "transportPaidBy": {"type": "string", "enum": ["vendor", "emitter"]},
            },
        ),
        cost_carbon,
    ),
    ToolSpec(
        "route_extra_cost",
        5,
        "업체 노선에 정류장을 더할 때 늘어나는 거리와 비용을 계산한다(무게·부피).",
        _schema({"vendor": OBJ, "stops": ARR, "newStop": OBJ}),
        route_extra_cost,
    ),
    ToolSpec(
        "optimize_assignment",
        6,
        "흐름을 업체나 현행 유지에 배정해 회사 측 월 순비용을 최소화한다.",
        _schema({"streams": ARR, "vendors": ARR}, {"carbonWeight": NUM, "exclude": ARR}),
        optimize_assignment,
    ),
]
TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float)}


def validate_arguments(schema: dict, arguments: Any) -> None:
    if not isinstance(arguments, dict):
        raise ValueError("입력은 객체여야 한다")  # noqa: TRY004
    for key in schema["required"]:
        if key not in arguments:
            raise ValueError(f"필수 입력이 없다: {key}")
    for key, value in arguments.items():
        prop = schema["properties"].get(key)
        if prop is None:
            raise ValueError(f"알 수 없는 입력: {key}")
        if value is None and key not in schema["required"]:
            continue
        expected = _TYPES[prop["type"]]
        if isinstance(value, bool) or not isinstance(value, expected):
            msg = f"입력 형식이 다르다: {josa(key, '은는')} {prop['type']}이어야 한다"
            raise ValueError(msg)  # noqa: TRY004
        if "enum" in prop and value not in prop["enum"]:
            raise ValueError(f"허용하지 않는 값: {key}")


def call_tool(name, arguments, *, registry, org_id, today, distance_provider=None) -> dict:
    spec = TOOLS.get(name)
    if spec is None:
        raise ValueError(f"알 수 없는 도구: {name}")
    validate_arguments(spec.input_schema, arguments)
    extra = {}
    if "distance_provider" in inspect.signature(spec.fn).parameters:
        extra["distance_provider"] = distance_provider
    return spec.fn(registry, org_id=org_id, today=today, **arguments, **extra)


def tool_schemas() -> list[dict]:
    """LLM에 넘기는 도구 정의 (OpenAI function 형식)."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": f"[{s.stage}단계] {s.description}",
                "parameters": s.input_schema,
            },
        }
        for s in _SPECS
    ]
