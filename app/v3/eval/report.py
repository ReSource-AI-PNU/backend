"""평가 리포트(Markdown). 숫자는 모두 이 자리에서 돌린 실행 결과에서 나온다.

    python3 -m app.v3.eval.report --out docs/v3/eval-report.md
        [--compliance 결과.json ...] [--groundedness groundedness.json]

- 사례 세트를 돌려 판정 일치율, 검증기 통과율, 금지 위반을 센다.
- 형식 준수 시험은 규칙 정책과 일부러 틀리는 스크립트로 돌린다. 실제 모델 결과는
  `python3 -m app.v3.eval.format_compliance --provider ... --out 결과.json`으로 잰 파일을
  --compliance로 줄 때만 들어가고, 없으면 "미측정"으로 적는다.
- 근거 충실도는 scripts/v3/eval/run_foundry_eval.py가 낸 파일을 --groundedness로 줄 때만 들어간다.

현재 시각을 쓰지 않는다. 같은 코드와 사례면 같은 리포트가 나온다(테스트가 저장된 파일과 견준다).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.v3.eval import format_compliance as compliance
from app.v3.eval import foundry_eval
from app.v3.eval.loader import load_cases
from app.v3.eval.runner import run_cases
from app.v3.eval.world import DEFAULT_TODAY

NOT_MEASURED = compliance.NOT_MEASURED
REAL_PROVIDERS = ("foundry", "ollama")


def collect(
    source: Any = None,
    *,
    compliance_files: list[Path] | None = None,
    groundedness_file: Path | None = None,
) -> dict[str, Any]:
    """리포트에 들어갈 것을 모두 돌려서 모은다."""
    cases = load_cases()
    outcome = run_cases(source, cases)
    situations = compliance.build_situations(cases=cases)
    runs = [
        compliance.run_compliance(compliance.RulePolicy(), situations),
        compliance.run_compliance(compliance.bad_script(situations), situations),
    ]
    given = []
    for path in compliance_files or []:
        given.append(json.loads(Path(path).read_text(encoding="utf-8")))
    measured = {r["model"]["provider"] for r in given if r.get("measured")}
    for provider in REAL_PROVIDERS:
        if provider not in measured:
            given.append(
                compliance.not_measured(
                    provider, "이 저장소에서는 실제 모델에 연결하지 않았습니다."
                )
            )
    groundedness: dict[str, Any] = {"measured": False}
    if groundedness_file:
        groundedness = json.loads(Path(groundedness_file).read_text(encoding="utf-8"))
    rows = foundry_eval.rows_from_cases(source, cases)
    notes = [{"id": c["id"], "note": c["note"]} for c in cases if c.get("note")]
    return {
        "today": DEFAULT_TODAY,
        "outcome": outcome,
        "compliance": [*runs, *given],
        "groundedness": groundedness,
        "groundednessRows": {
            "rows": len(rows),
            "messages": sum(1 for r in rows if r["kind"] == "message"),
            "documents": sum(1 for r in rows if r["kind"] == "document"),
        },
        "notes": notes,
    }


def _pct(rate: float | None) -> str:
    return NOT_MEASURED if rate is None else f"{rate * 100:.1f}%"


def _ratio(passed: int, total: int) -> str:
    return f"{_pct(passed / total if total else None)} ({passed}/{total})"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _model(label: dict[str, Any]) -> str:
    return " · ".join(x for x in (label.get("provider"), label.get("name")) if x) or "-"


def render(data: dict[str, Any]) -> str:
    outcome = data["outcome"]
    summary, results = outcome["summary"], outcome["results"]
    reviews = ", ".join(
        f"{label} {count}건" for label, count in sorted(summary["reviewCounts"].items())
    )
    grounded = data["groundedness"]
    real = [r for r in data["compliance"] if r["model"]["provider"] in REAL_PROVIDERS]
    real_text = (
        ", ".join(f"{_model(r['model'])} {_pct(r['finalPassRate'])}" for r in real if r["measured"])
        or NOT_MEASURED
    )
    lines = [
        "# 설계안 v3 평가 리포트",
        "",
        (
            "이 파일은 `python3 -m app.v3.eval.report --out docs/v3/eval-report.md`가 만든다. "
            "숫자는 모두 그 실행의 결과이고 손으로 고치지 않는다. 재지 못한 것은 "
            f'"{NOT_MEASURED}"으로 적는다.'
        ),
        "",
        f"- 사례 속 기준일: {data['today']}",
        f"- 사례를 돌린 구현: {_model(outcome['model'])}",
        "- 사례의 회사, 업체, 수치는 모두 가상이다. 실제 업체·견적·측정이 아니다.",
        (
            "- 사례는 전문가 검토 전이다. 판정 일치율은 구현이 팀이 적은 기대와 맞는 정도이고, "
            "기대 자체가 옳다는 뜻이 아니다."
        ),
        "",
        "## 1. 요약",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 사례 수 | {summary['caseCount']}건 |",
        f"| 초안 표시 건수(전문가 검토 전) | {summary['draftCount']}건 ({reviews}) |",
        f"| 판정 일치율 | {_ratio(summary['verdictMatched'], summary['caseCount'])} |",
        f"| open 사례(설계 판단 필요) | {summary['openCount']}건"
        + (f" ({', '.join(summary['openCases'])})" if summary["openCases"] else "")
        + " |",
        f"| open을 뺀 판정 일치 | {summary['closedMatched']}/{summary['closedCount']} |",
        (
            f"| 검증기 통과율(나간 메시지와 문서) | "
            f"{_ratio(summary['validatorPassed'], summary['validatorChecked'])} |"
        ),
        f"| 한 번 거부된 뒤 다시 써서 나간 글 | {summary['rewritten']}건 |",
        f"| 두 번 거부되어 handoff로 넘어간 대화 | {summary['handedOff']}건 |",
        f"| 금지 위반 | {summary['forbiddenViolationCount']}건 |",
        f"| 실행 오류 | {summary['errorCount']}건 |",
        f"| 형식 준수(실제 모델, 재작성 뒤) | {real_text} |",
        "| 근거 충실도(Foundry 평가) | "
        + (
            json.dumps(grounded.get("metrics"), ensure_ascii=False)
            if grounded.get("measured")
            else NOT_MEASURED
        )
        + " |",
        "",
        (
            "판정 일치는 사례의 기대(판정 상태, checks 키, 스레드 상태, 질문, 승인 등)가 모두 맞을 때만 "
            "센다. 금지 위반은 사례에 적은 금지 사항과 모든 사례에 걸리는 공통 규칙(가드레일, 허가 품목, "
            "승인 전 확정, 턴 상한, 질문 한도, 조직 경계, 검증 없이 나간 메시지)을 함께 센다."
        ),
        "",
        "## 2. 사례별 결과",
        "",
        "| ID | 사례 | 검토 | 판정 | 금지 위반 | 검증기 |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        verdict = "일치" if r["verdictMatch"] else ("오류" if r["error"] else "불일치")
        if r["open"]:
            verdict += " (open)"
        validator = r["validator"]
        checked = f"{validator['passed']}/{validator['checked']}" if validator["checked"] else "-"
        lines.append(
            f"| {r['id']} | {_cell(r['title'])} | {r['review']} | {verdict} | "
            f"{len(r['forbiddenViolations'])}건 | {checked} |"
        )
    lines += [
        "",
        "검증기 칸의 `-`는 도구 수준에서만 본 사례이거나 대화가 열리지 않아 나간 글이 없는 사례다.",
        "",
    ]

    lines += ["## 3. 기대와 다른 사례, open 사례", ""]
    differing = [
        r for r in results if not r["verdictMatch"] or r["open"] or r["forbiddenViolations"]
    ]
    if not differing:
        lines += ["없음.", ""]
    for r in differing:
        lines.append(f"### {r['id']} {r['title']}")
        lines.append("")
        if r["open"]:
            lines.append(f"- 상태: open. {r['openNote']}")
        if r["error"]:
            lines.append(f"- 실행 오류: `{r['error']}`")
        if r["open"] and r["verdictMatch"]:
            lines.append("- 지금은 기대와 일치한다. open 표시를 지울지 확인이 필요하다.")
        for item in r["mismatches"]:
            lines.append(
                f"- `{item['path']}`: 기대 `{json.dumps(item['expected'], ensure_ascii=False)}`, "
                f"실제 `{json.dumps(item['actual'], ensure_ascii=False)}`"
            )
        for item in r["forbiddenViolations"]:
            lines.append(
                f"- 금지 위반({item['kind']}): {item['why']} "
                f"`{json.dumps(item['detail'], ensure_ascii=False)}`"
            )
        lines.append("")

    if data["notes"]:
        lines += ["## 4. 사례에 적어 둔 메모", ""]
        lines += [f"- {n['id']}: {n['note']}" for n in data["notes"]]
        lines.append("")

    lines += [
        "## 5. 모델 형식 준수 시험",
        "",
        (
            "두 에이전트가 모델에게 실제로 주는 입력(고정 상황)에 모델이 형식대로 답하는지 본다. "
            "거부되면 사유를 돌려주고 한 번 다시 쓰게 한다. 규칙 정책과 스크립트는 모델이 아니고, "
            "시험이 제대로 도는지 확인하려고 돌린 것이다."
        ),
        "",
        "| 대상 | 상황 수 | 첫 시도 통과 | 재작성 뒤 통과 | 비고 |",
        "|---|---|---|---|---|",
    ]
    for r in data["compliance"]:
        if not r["measured"]:
            lines.append(
                f"| {_model(r['model'])} | - | {NOT_MEASURED} | {NOT_MEASURED} | {_cell(r['reason'])} |"
            )
            continue
        codes = ", ".join(f"{k} {v}" for k, v in r["firstFailureCodes"].items()) or "없음"
        lines.append(
            f"| {_model(r['model'])} | {r['situationCount']} | "
            f"{_ratio(r['firstPassed'], r['situationCount'])} | "
            f"{_ratio(r['finalPassed'], r['situationCount'])} | 첫 시도 거부 사유: {codes} |"
        )
    lines.append("")
    first = next((r for r in data["compliance"] if r["measured"]), None)
    if first:
        roles = first["roles"]
        lines += [
            (
                f"상황 {first['situationCount']}개(회사 측 {roles['company']}, 업체 측 {roles['vendor']})는 "
                f"사례 {', '.join(dict.fromkeys(x['caseId'] for x in first['results']))}에서 뽑았다."
            ),
            "",
            "검사 항목별 첫 시도 통과:",
            "",
            "| 대상 | " + " | ".join(c["label"] for c in first["byCheck"]) + " |",
            "|---|" + "---|" * len(first["byCheck"]),
        ]
        for r in data["compliance"]:
            if r["measured"]:
                cells = " | ".join(f"{c['passed']}/{r['situationCount']}" for c in r["byCheck"])
                lines.append(f"| {_model(r['model'])} | {cells} |")
        lines.append("")

    rows = data["groundednessRows"]
    lines += [
        "## 6. 근거 충실도 (Foundry 평가)",
        "",
        (
            f"- 평가 입력: 사례의 실행 기록에서 수치나 조항을 인용한 글 {rows['rows']}행"
            f"(메시지 {rows['messages']}, 문서 {rows['documents']})을 만들 수 있다 "
            "(`python3 -m app.v3.eval.foundry_eval --out eval-groundedness.jsonl`)."
        ),
    ]
    if grounded.get("measured"):
        lines += [
            f"- 평가 모델: {grounded.get('model')}, 평가한 행: {grounded.get('rows')}",
            f"- 결과: `{json.dumps(grounded.get('metrics'), ensure_ascii=False)}`",
        ]
    else:
        lines.append(
            f"- 결과: {NOT_MEASURED}. `scripts/v3/eval/run_foundry_eval.py`는 Azure AI Evaluation SDK와 "
            "Foundry 모델 배포가 있어야 돌고, 이 저장소에서는 돌려 보지 못했다."
        )
    lines += ["", "## 7. 미측정 항목", ""]
    unmeasured = [
        f"형식 준수 시험: {_model(r['model'])} ({r['reason']})"
        for r in data["compliance"]
        if not r["measured"]
    ]
    if not grounded.get("measured"):
        unmeasured.append("근거 충실도(Foundry 평가 groundedness)")
    if outcome["model"].get("provider") == "rule":
        unmeasured.append(
            "실제 모델로 돌린 사례 판정 일치율과 검증기 통과율(이 리포트의 사례는 규칙 정책으로 돌았다)"
        )
    unmeasured.append("전문가 검토(사례의 기대값은 모두 초안이다)")
    lines += [f"- {item}" for item in unmeasured] or ["없음."]
    lines += [
        "",
        "## 8. 다시 만들기",
        "",
        "```bash",
        "python3 -m app.v3.eval.report --out docs/v3/eval-report.md",
        "# 실제 모델과 Foundry 평가 결과를 넣으려면",
        "python3 -m app.v3.eval.format_compliance --provider foundry --out foundry.json",
        "python3 -m app.v3.eval.format_compliance --provider ollama --model <모델> --out ollama.json",
        "python3 -m app.v3.eval.foundry_eval --out eval-groundedness.jsonl",
        "python3 scripts/v3/eval/run_foundry_eval.py --data eval-groundedness.jsonl --out groundedness.json",
        "python3 -m app.v3.eval.report --out docs/v3/eval-report.md \\",
        "  --compliance foundry.json ollama.json --groundedness groundedness.json",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="평가 리포트 만들기")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compliance", type=Path, nargs="*", default=[])
    parser.add_argument("--groundedness", type=Path, default=None)
    args = parser.parse_args(argv)
    data = collect(compliance_files=args.compliance, groundedness_file=args.groundedness)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(data), encoding="utf-8")
    summary = data["outcome"]["summary"]
    sys.stdout.write(
        f"{args.out}: 사례 {summary['caseCount']}건, 판정 일치 {summary['verdictMatched']}, "
        f"검증기 {summary['validatorPassed']}/{summary['validatorChecked']}, "
        f"금지 위반 {summary['forbiddenViolationCount']}건\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
