"""Foundry 평가(근거 충실도, groundedness)에 넣을 입력을 실행 기록에서 만든다.

    python3 -m app.v3.eval.foundry_eval --out eval-groundedness.jsonl

행 하나는 나간 메시지나 문서 하나다.
- query: 그 글이 답한 것(받은 메시지를 값으로 풀어 쓴 글, 첫 문의와 문서는 해야 할 일의 설명)
- response: 자리표시자를 값으로 바꾼 글(상대가 실제로 읽는 글)
- context: 그 글이 인용한 근거의 원문 발췌, 값, 출처, 기준일

여기서는 입력만 만든다. 평가는 scripts/v3/eval/run_foundry_eval.py가 Azure AI Evaluation SDK로
돌리고, 이 저장소에서는 돌려 보지 못했다(리포트에는 "미측정"으로 적는다).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.v3.eval import runner
from app.v3.eval.loader import load_cases
from app.v3.evidence.registry import EvidenceNotFound
from app.v3.evidence.render import render_text

TASKS = {
    "inquiry": "업체에 흐름을 받을 수 있는지와 조건을 묻는 첫 문의를 쓴다.",
    "request": "합의한 조건으로 수거·처리 요청서를 쓴다.",
    "plan": "확정한 조건의 이행 계획서를 쓴다.",
}


def _context(registry: Any, claims: list[dict[str, Any]]) -> str:
    """claim이 인용한 근거를 글로 늘어놓는다. 같은 근거는 한 번만 적는다."""
    lines: list[str] = []
    seen: set[str] = set()
    for claim in claims:
        for evidence_id in claim.get("evidence") or []:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            try:
                found = registry.get(evidence_id, None)
            except EvidenceNotFound:
                lines.append(f"[{evidence_id}] (근거 저장소에 없음)")
                continue
            value = ""
            if found.value is not None:
                value = f" 값: {found.value}{' ' + found.unit if found.unit else ''}."
            lines.append(
                f"[{found.id}] {found.label or found.kind}.{value} 원문: {found.excerpt} "
                f"(출처: {found.source}, 기준일: {found.as_of})"
            )
    return "\n".join(lines)


def build_rows(app: Any, session_id: str) -> list[dict[str, Any]]:
    """진단 한 건의 실행 기록(스레드의 메시지와 문서)에서 평가 행을 만든다."""
    rows: list[dict[str, Any]] = []
    for thread in app.store.list_threads(session_id=session_id):
        previous = ""
        for message in thread["messages"]:
            rendered = render_text(message["text"], message["claims"])
            query = previous or TASKS.get(message["type"]) or "앞선 대화에 이어 답한다."
            rows.append(
                {
                    "id": f"{session_id}:{thread['id']}:m{message['seq']}",
                    "query": query,
                    "response": rendered,
                    "context": _context(app.registry, message["claims"]),
                    "kind": "message",
                    "type": message["type"],
                    "from": message["from"],
                    "claimCount": len(message["claims"]),
                    "model": (message.get("meta") or {}).get("model"),
                }
            )
            previous = rendered
    for document in app.store.list_documents(session_id):
        rows.append(
            {
                "id": f"{session_id}:{document['id']}",
                "query": TASKS.get(document.get("kind"), "문서를 쓴다."),
                "response": document.get("rendered")
                or render_text(document["text"], document["claims"]),
                "context": _context(app.registry, document["claims"]),
                "kind": "document",
                "type": document.get("kind"),
                "from": None,
                "claimCount": len(document["claims"]),
                "model": None,
            }
        )
    return rows


def rows_from_cases(source: Any = None, cases: list[dict[str, Any]] | None = None) -> list[dict]:
    """평가 사례를 돌린 실행 기록에서 행을 모은다. 수치가 든 글(claim이 있는 글)만 남긴다."""
    options = runner._options(source)
    rows: list[dict[str, Any]] = []
    for case in cases if cases is not None else load_cases():
        if not case.get("session", True):
            continue
        session = runner._Session(case, options)
        session.run()
        for row in build_rows(session.app, session.sid):
            if row["claimCount"]:
                rows.append({**row, "id": f"{case['id']}:{row['id']}", "caseId": case["id"]})
    return rows


def write_jsonl(rows: list[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foundry 평가(근거 충실도) 입력 JSONL 만들기")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    count = write_jsonl(rows_from_cases(), args.out)
    sys.stdout.write(f"{args.out}: {count}행 (평가 사례의 실행 기록, 수치는 모두 가상)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
