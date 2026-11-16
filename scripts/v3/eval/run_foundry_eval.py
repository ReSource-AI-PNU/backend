"""미실행(Azure 필요). Foundry 평가로 근거 충실도(groundedness)를 잰다.

Azure AI Evaluation SDK(azure-ai-evaluation)와 Foundry 채팅 모델 배포가 있어야 한다. 이 저장소에서는
돌려 보지 못했다(py_compile만 확인). 처음 돌릴 때 SDK 판에 따라 인자 이름이 다르면 함께 고친다.

    # 1) 입력 만들기(로컬에서 된다)
    python3 -m app.v3.eval.foundry_eval --out eval-groundedness.jsonl
    # 2) 평가(환경 변수 FOUNDRY_ENDPOINT, FOUNDRY_DEPLOYMENT, FOUNDRY_API_KEY)
    python3 scripts/v3/eval/run_foundry_eval.py --data eval-groundedness.jsonl --out groundedness.json
    # Foundry 프로젝트에 결과를 남기려면 --project <프로젝트 주소>

결과 파일(groundedness.json)은 app.v3.eval.report가 읽는 모양이다:
{"measured": true, "model": 배포 이름, "rows": 행 수, "metrics": {...}, "studioUrl": 주소 또는 null}.
키는 환경 변수로만 받고 파일이나 출력에 남기지 않는다. 입력의 수치는 모두 가상이다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foundry 평가: 근거 충실도")
    parser.add_argument("--data", type=Path, required=True, help="foundry_eval이 만든 JSONL")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--project", default="", help="결과를 남길 Foundry 프로젝트 주소(선택)")
    parser.add_argument("--api-version", default="2024-10-21")
    args = parser.parse_args(argv)

    missing = [
        name
        for name in ("FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "FOUNDRY_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        sys.stderr.write(f"환경 변수가 필요합니다: {', '.join(missing)}\n")
        return 2
    try:
        from azure.ai.evaluation import GroundednessEvaluator, evaluate
    except ImportError:
        sys.stderr.write(
            "azure-ai-evaluation이 없습니다. 평가를 돌리는 환경에서 설치한 뒤 다시 실행하세요.\n"
        )
        return 2

    model_config = {
        "azure_endpoint": os.environ["FOUNDRY_ENDPOINT"],
        "azure_deployment": os.environ["FOUNDRY_DEPLOYMENT"],
        "api_key": os.environ["FOUNDRY_API_KEY"],
        "api_version": args.api_version,
    }
    options = {}
    if args.project:
        options["azure_ai_project"] = args.project
    result = evaluate(
        data=str(args.data),
        evaluators={"groundedness": GroundednessEvaluator(model_config)},
        evaluator_config={
            "groundedness": {
                "column_mapping": {
                    "query": "${data.query}",
                    "response": "${data.response}",
                    "context": "${data.context}",
                }
            }
        },
        **options,
    )
    rows = sum(1 for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip())
    summary = {
        "measured": True,
        "model": os.environ["FOUNDRY_DEPLOYMENT"],
        "rows": rows,
        "metrics": dict(result.get("metrics") or {}),
        "studioUrl": result.get("studio_url"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"근거 충실도 평가 {rows}행: {json.dumps(summary['metrics'], ensure_ascii=False)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
