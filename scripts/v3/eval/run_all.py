"""평가 설계의 층을 한 번에 돌려 리포트를 쓴다(평가 설계 15장, 16장, 18장).

    python3 -m scripts.v3.eval.run_all --split test --llm rule
    python3 -m scripts.v3.eval.run_all --split test --llm foundry --repeats 4 --budget-calls 3000
    python3 -m scripts.v3.eval.run_all --layers L2,L6 --split dev --resamples 2000
    python3 -m scripts.v3.eval.run_all --from-raw docs/v3/eval/2026-11-18-test-foundry.raw.jsonl

쓰는 파일(--out-dir, 기본 docs/v3/eval):
    <날짜>-<split>-<llm>.json      층별 지표, CI, 표, 머리(커밋, 평가셋 판, 매니페스트 해시, 모델, 토큰)
    <날짜>-<split>-<llm>.md        같은 내용의 Markdown(손으로 고치지 않는다)
    <날짜>-<split>-<llm>.raw.jsonl 원 출력: 실행마다 나간 메시지와 문서(--no-raw로 끈다)
    latest.json                    신뢰 페이지용 최신 요약(split마다 한 칸)
그리고 docs/v3/eval-report.md를 다시 만든다(latest.json 요약이 들어간다).

- --llm rule(기본)은 모델을 부르지 않는다(네트워크 없음, CI와 같다). 결과는 코드의 회귀 시험이다.
- --llm foundry는 FOUNDRY_ENDPOINT, FOUNDRY_DEPLOYMENT, FOUNDRY_API_KEY가 있어야 한다(없으면 멈춘다).
  Prompt Shields(AZURE_CONTENT_SAFETY_*)와 AI Search(V3_EVAL_L2_AZURE=1 + AZURE_SEARCH_*)는 있을
  때만 그 칸을 잰다.
- --budget-calls에 닿으면 새 실행을 시작하지 않고 남은 칸을 "예산 초과로 미측정"으로 적는다.
- --from-raw는 저장한 원 출력(.raw.jsonl)과 같은 자리의 리포트(.json)에서 모델을 다시 부르지 않고
  리포트(.json, .md)와 latest.json(과 eval-report.md)을 다시 만든다. 값과 n은 그대로이고 신뢰구간만 지금
  규칙(비율은 Wilson 점수 구간)으로 다시 낸다. 원 출력 행 수가 리포트의 L4 실행 수와 다르면 멈춘다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from app.v3.eval import report, stats, suite
from app.v3.service import SEOUL

OUT_DIR = suite.ROOT / "docs" / "v3" / "eval"
EVAL_REPORT = suite.ROOT / "docs" / "v3" / "eval-report.md"


def parse_layers(text: str) -> tuple[str, ...]:
    picked = tuple(x.strip().upper() for x in text.split(",") if x.strip())
    unknown = [x for x in picked if x not in suite.LAYERS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"모르는 층: {', '.join(unknown)} (가능: {', '.join(suite.LAYERS)})"
        )
    return picked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="평가 설계 층별 평가 실행")
    parser.add_argument("--layers", type=parse_layers, default=suite.LAYERS, help="예: L2,L4,L6")
    parser.add_argument("--split", choices=("dev", "test"), default="test")
    parser.add_argument(
        "--repeats", type=int, default=None, help="시나리오 반복 n(기본: rule 1, 모델 4)"
    )
    parser.add_argument("--llm", choices=("rule", "foundry"), default="rule")
    parser.add_argument("--budget-calls", type=int, default=None, help="모델 호출 상한")
    parser.add_argument("--resamples", type=int, default=stats.RESAMPLES)
    parser.add_argument("--date", default=None, help="파일 이름과 머리의 날짜(기본: 오늘)")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--no-raw", action="store_true", help="원 출력 JSONL을 쓰지 않는다")
    parser.add_argument(
        "--no-report", action="store_true", help="docs/v3/eval-report.md를 다시 만들지 않는다"
    )
    parser.add_argument(
        "--from-raw",
        type=Path,
        default=None,
        help="저장한 <날짜>-<split>-<llm>.raw.jsonl(과 같은 자리의 .json)에서 리포트와 latest.json을 "
        "다시 만든다. 모델을 부르지 않는다",
    )
    args = parser.parse_args(argv)
    repeats = args.repeats or (1 if args.llm == "rule" else 4)
    date = args.date or dt.datetime.now(SEOUL).date().isoformat()

    def say(message: str) -> None:
        sys.stderr.write(f"· {message}\n")

    if args.from_raw is not None:
        return rebuild(
            args.from_raw,
            args.out_dir if args.out_dir != OUT_DIR else None,
            not args.no_report,
            say,
        )

    result = suite.run_suite(
        layers=args.layers,
        split=args.split,
        repeats=repeats,
        llm=args.llm,
        budget_calls=args.budget_calls,
        resamples=args.resamples,
        keep_raw=not args.no_raw,
        progress=say,
    )
    result["header"]["date"] = date
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"{date}-{args.split}-{args.llm}"
    raw = result.pop("raw")
    json_path, md_path, raw_path = (
        stem.with_suffix(".json"),
        stem.with_suffix(".md"),
        Path(f"{stem}.raw.jsonl"),
    )
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8"
    )
    md_path.write_text(suite.render_markdown(result), encoding="utf-8")
    files = [json_path, md_path]
    if raw and not args.no_raw:
        with raw_path.open("w", encoding="utf-8") as handle:
            for row in raw:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        files.append(raw_path)
    suite.update_latest(out / "latest.json", result, files)
    if not args.no_report and out.resolve() == OUT_DIR.resolve():
        EVAL_REPORT.write_text(report.render(report.collect()), encoding="utf-8")
    for layer in result["layers"]:
        main_metric = next((m for m in layer["metrics"] if m["value"] is not None), None)
        text = (
            f"{main_metric['label']} {main_metric['value']:.3f}"
            if main_metric
            else stats.NOT_MEASURED
        )
        say(f"{layer['layer']} {layer['title']}: n={layer['n']}, {text}")
    sys.stdout.write("\n".join(str(p) for p in files) + "\n")
    return 0


def rebuild(raw: Path, out_dir: Path | None, write_report: bool, say) -> int:
    """--from-raw: 저장한 결과의 신뢰구간을 지금 규칙으로 다시 내고 .json·.md·latest.json을 다시 쓴다."""
    try:
        result, _rows, json_path = suite.rebuild_from_raw(raw)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    out = out_dir or json_path.parent
    out.mkdir(parents=True, exist_ok=True)
    stem = out / json_path.name.removesuffix(".json")
    json_out, md_out = stem.with_suffix(".json"), stem.with_suffix(".md")
    json_out.write_text(
        json.dumps(result, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8"
    )
    md_out.write_text(suite.render_markdown(result), encoding="utf-8")
    raw_path = Path(raw)
    suite.update_latest(out / "latest.json", result, [json_out, md_out, raw_path])
    if write_report and out.resolve() == OUT_DIR.resolve():
        EVAL_REPORT.write_text(report.render(report.collect()), encoding="utf-8")
    say(f"다시 만듦(모델 호출 없음): {json_out.name}, {md_out.name}, latest.json")
    sys.stdout.write("\n".join(str(p) for p in (json_out, md_out)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
