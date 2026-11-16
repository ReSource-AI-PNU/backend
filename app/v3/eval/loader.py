"""사례 세트 불러오기와 형식 검사. 사례는 app/v3/eval/cases/*.json에 있고 수치는 모두 가상이다.

사례 하나의 모양(자세한 설명은 cases/README.md):

    {"id": "C11", "title": "...", "review": "초안" | "제품 정책 초안", "synthetic": true,
     "tags": ["PE", "PIR"], "world": {"streams": [...], "vendors": [...]},
     "answers": {"st_x.moisturePct": 2.0, "*": "unknown"}, "approve": false, "actions": [...],
     "expect": {"result": {"status": "conditional"}, "assignments": {...}, "threads": [...],
                "tools": [{"tool": "judge_route", "status": "conditional", "checks": {...}}]},
     "forbidden": [{"kind": "money", "stream": "st_x"}],
     "status": "open", "openNote": "..."}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASE_DIR = Path(__file__).with_name("cases")
REVIEW_DRAFT = "초안"  # 전문가 검토 전
REVIEW_POLICY_DRAFT = "제품 정책 초안"  # docs/c-track/pp-validation-cases.md의 원문 상태
REVIEWS = (REVIEW_DRAFT, REVIEW_POLICY_DRAFT, "검토 완료")
VERDICTS = ("calculated", "conditional", "needs_information")
THREAD_STATUSES = ("not_opened", "open", "agreed", "declined", "handoff", "confirmed")
SESSION_STATUSES = ("idle", "running", "awaiting_answers", "awaiting_approval", "confirmed")
CHECK_STATUSES = ("met", "conditional", "unmet")
FORBIDDEN_KINDS = (
    "money",  # 그 흐름에 금액 claim(단가·월 손익)이 나오면 안 된다
    "assigned",  # 그 흐름이 (그 업체에) 배정되면 안 된다
    "thread_opened",  # 그 업체와 (그 흐름으로) 대화가 열리면 안 된다
    "message_type",  # 그 조직이 그 종류의 메시지를 보내면 안 된다
    "text",  # 나간 메시지·문서 본문에 그 글이 있으면 안 된다
    "evidence_cited",  # 나간 메시지·문서·결과가 그 근거를 인용하면 안 된다
    "verdict",  # 그 흐름(또는 전체)의 판정이 그 상태면 안 된다
    "confirmed",  # 확정되거나 문서가 만들어지면 안 된다
    "tool_claim",  # 도구 결과에 그 claim이 있으면 안 된다
    "tool_route",  # 도구가 그 경로를 내면 안 된다
    "question",  # 담당자에게 그 항목을 물으면 안 된다
)
ACTIONS = ("upload_document", "patch_evidence", "approve_one", "update_stream")
WHEN = ("before_run", "awaiting_approval", "after_confirm")
_TOP = {
    "id",
    "title",
    "review",
    "synthetic",
    "tags",
    "source",
    "note",
    "world",
    "answers",
    "approve",
    "actions",
    "session",
    "expect",
    "forbidden",
    "status",
    "openNote",
}
_EXPECT = {
    "session",
    "result",
    "assignments",
    "threads",
    "questions",
    "unresolved",
    "stages",
    "tools",
    "uploads",
    "approvals",
    "documents",
    "streams",
}


class CaseError(ValueError):
    """사례 형식이 틀렸다."""


def load_cases(directory: str | Path | None = None) -> list[dict[str, Any]]:
    """폴더의 *.json을 이름 순서로 읽어 사례 목록을 돌려준다. 파일은 {"cases": [...]} 모양이다."""
    folder = CASE_DIR if directory is None else Path(directory)
    cases: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise CaseError(f"{path.name}: JSON을 읽을 수 없다 ({error})") from error
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise CaseError(f"{path.name}: 최상위는 cases 배열을 가진 객체여야 한다")
        if data.get("synthetic") is not True:
            raise CaseError(f"{path.name}: 가상 수치라는 표시(synthetic: true)가 없다")
        for case in data["cases"]:
            cases.append({**case, "file": path.name})
    return cases


def _need(condition: bool, case_id: str, message: str, problems: list[str]) -> None:
    if not condition:
        problems.append(f"{case_id}: {message}")


def check_case(case: dict[str, Any]) -> list[str]:
    """사례 하나의 형식 문제 목록. 문제가 없으면 빈 목록."""
    cid = str(case.get("id") or "?")
    out: list[str] = []
    _need(isinstance(case.get("id"), str) and bool(case.get("id")), cid, "id가 없다", out)
    _need(isinstance(case.get("title"), str) and bool(case.get("title")), cid, "title이 없다", out)
    _need(case.get("review") in REVIEWS, cid, f"review는 {REVIEWS} 가운데 하나다", out)
    _need(case.get("synthetic") is True, cid, "synthetic: true(가상 수치) 표시가 없다", out)
    _need(isinstance(case.get("tags"), list) and bool(case.get("tags")), cid, "tags가 없다", out)
    unknown = set(case) - _TOP - {"file"}
    _need(not unknown, cid, f"모르는 키: {sorted(unknown)}", out)
    world = case.get("world")
    if not isinstance(world, dict):
        return [*out, f"{cid}: world가 없다"]
    streams = world.get("streams")
    vendors = world.get("vendors") or []
    _need(isinstance(streams, list) and bool(streams), cid, "world.streams가 없다", out)
    stream_ids = [s.get("id") for s in streams or []]
    vendor_ids = [v.get("orgId") for v in vendors]
    _need(len(set(stream_ids)) == len(stream_ids), cid, "흐름 ID가 겹친다", out)
    _need(len(set(vendor_ids)) == len(vendor_ids), cid, "업체 ID가 겹친다", out)
    for sid in stream_ids:
        _need(
            isinstance(sid, str) and sid.startswith("st_"), cid, f"흐름 ID는 st_로 시작: {sid}", out
        )
    for vid in vendor_ids:
        _need(
            isinstance(vid, str) and vid.startswith("org_"),
            cid,
            f"업체 ID는 org_로 시작: {vid}",
            out,
        )
    for action in case.get("actions") or []:
        _need(action.get("action") in ACTIONS, cid, f"모르는 action: {action.get('action')}", out)
        _need(action.get("when") in WHEN, cid, f"모르는 when: {action.get('when')}", out)
    expect = case.get("expect")
    if not isinstance(expect, dict) or not expect:
        return [*out, f"{cid}: expect(기대 판정)가 없다"]
    unknown = set(expect) - _EXPECT
    _need(not unknown, cid, f"expect에 모르는 키: {sorted(unknown)}", out)
    verdict_keys = {"result", "assignments", "tools", "threads"}
    _need(bool(verdict_keys & set(expect)), cid, "기대 판정 상태가 없다", out)
    status = (expect.get("session") or {}).get("status")
    _need(status is None or status in SESSION_STATUSES, cid, f"모르는 진단 상태: {status}", out)
    status = (expect.get("result") or {}).get("status")
    _need(status is None or status in VERDICTS, cid, f"모르는 판정 상태: {status}", out)
    for sid, item in (expect.get("assignments") or {}).items():
        _need(sid in stream_ids, cid, f"expect.assignments에 없는 흐름: {sid}", out)
        _need(
            item.get("status") is None or item["status"] in VERDICTS,
            cid,
            f"모르는 판정 상태: {item.get('status')}",
            out,
        )
        vendor = item.get("vendor")
        _need(vendor is None or vendor in vendor_ids, cid, f"없는 업체: {vendor}", out)
    for item in expect.get("threads") or []:
        _need(item.get("vendor") in vendor_ids, cid, f"expect.threads에 없는 업체: {item}", out)
        _need(item.get("status") in THREAD_STATUSES, cid, f"모르는 스레드 상태: {item}", out)
    for item in expect.get("tools") or []:
        _need(isinstance(item.get("tool"), str), cid, "expect.tools에 tool이 없다", out)
        _need(
            item.get("status") is None or item["status"] in VERDICTS,
            cid,
            f"모르는 판정 상태: {item.get('status')}",
            out,
        )
        for key, value in (item.get("checks") or {}).items():
            _need(value in CHECK_STATUSES, cid, f"모르는 check 상태: {key}={value}", out)
    forbidden = case.get("forbidden")
    _need(isinstance(forbidden, list) and bool(forbidden), cid, "forbidden(금지 사항)이 없다", out)
    for item in forbidden or []:
        _need(
            item.get("kind") in FORBIDDEN_KINDS, cid, f"모르는 금지 종류: {item.get('kind')}", out
        )
        _need(bool(item.get("why")), cid, "금지 사항에 이유(why)가 없다", out)
    _need(case.get("status") in (None, "open"), cid, "status는 open만 쓴다", out)
    if case.get("status") == "open":
        _need(bool(case.get("openNote")), cid, "open 사례에는 openNote가 있어야 한다", out)
    return out


def check_cases(cases: list[dict[str, Any]]) -> list[str]:
    problems = [p for case in cases for p in check_case(case)]
    seen: set[str] = set()
    for case in cases:
        cid = str(case.get("id"))
        if cid in seen:
            problems.append(f"{cid}: ID가 겹친다")
        seen.add(cid)
    return problems
