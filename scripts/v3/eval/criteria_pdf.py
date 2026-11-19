"""매입 기준표 판독 평가. 시험 PDF(tests/v3/fixtures/criteria_pdf)를 실제 업로드 경로로 읽고 기대값과 맞춘다.

service.upload_document를 업체 조직으로 부른다: Document Intelligence(글·표 구조) → Prompt Shields(문서 안 지시문)
→ 모델 판독(쪽 그림 + 글·표 구조) → 코드의 단위 환산·확인 필요 판정. 실제 외부 서비스를 부르므로 아래 환경
변수가 있어야 한다(값은 Key Vault에 있다. 운영 배포 스크립트 infra/vm/deploy-vm.sh가 넣는 이름과 같다).

    V3_LLM=foundry FOUNDRY_ENDPOINT FOUNDRY_DEPLOYMENT FOUNDRY_API_KEY
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT AZURE_DOCUMENT_INTELLIGENCE_API_KEY
    AZURE_CONTENT_SAFETY_ENDPOINT AZURE_CONTENT_SAFETY_API_KEY

    uv run python -m scripts.v3.eval.criteria_pdf --runs 3 --out docs/v3/eval/criteria_pdf.json

기대값(expected.json)은 문서마다 결과 종류(outcome), 행 수, 행별 값이다. 값이 null이면 비워 둬야 맞고, review에
적은 칸은 확인 대상으로 표시돼야 맞다. 칸 하나라도 틀리면 그 문서의 그 회차는 실패다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "v3" / "fixtures" / "criteria_pdf"
VENDOR = "org_saegil"

# 기대값 키 → 초안 행에서 읽는 값
GETTERS = {
    "price": lambda row: row["price"]["amount"],
    "candidates": lambda row: row["price"]["candidates"],
    "direction": lambda row: row["price"]["direction"],
    "minMonthlyKg": lambda row: row["minMonthlyKg"]["value"],
    "maxContaminationPct": lambda row: row["maxContaminationPct"]["value"],
    "maxMoisturePct": lambda row: row["maxMoisturePct"]["value"],
    "collectionFee": lambda row: row["collectionFee"]["value"],
    "feePerPickupKrw": lambda row: row["collectionFee"]["feePerPickupKrw"],
    "page": lambda row: row["page"],
}
REVIEW_KEYS = {"price": "price", "minMonthlyKg": "minMonthlyKg"}


def build_app() -> Any:
    from app.v3 import service
    from app.v3.evidence.registry import InMemoryRegistry
    from app.v3.intake import criteria_llm, documents, shields
    from app.v3.store import InMemoryStore

    reader = criteria_llm.reader_from_env(os.environ)
    if reader is None:
        raise SystemExit("판독기를 만들지 못했다: V3_LLM=foundry와 FOUNDRY_* 환경 변수를 확인한다")
    app = service.App(
        InMemoryStore(),
        InMemoryRegistry(),
        documents=documents.client_from_env(os.environ),
        shields=shields.client_from_env(os.environ),
        criteria_reader=reader,
    )
    service.seed(app)
    return app


def _same(actual: Any, expected: Any) -> bool:
    if expected is None or actual is None:
        return actual is expected
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(abs(float(a) - float(e)) < 1e-6 for a, e in zip(actual, expected, strict=True))
        )
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and abs(float(actual) - float(expected)) < 1e-6
    return actual == expected


def _find(rows: list[dict[str, Any]], match: dict[str, str]) -> dict[str, Any] | None:
    for row in rows:
        if match.get("material") and row["material"]["value"] != match["material"]:
            continue
        if "grade" in match and row["grade"]["value"] != match["grade"]:
            continue
        if match.get("form") and match["form"] not in str(row["form"]["value"] or ""):
            continue
        return row
    return None


def grade(result: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """한 회차의 결과를 기대값과 맞춘다. {"pass", "problems", "cells": (맞은 칸, 전체 칸), "wrong", "blank"}

    wrong은 기대와 다른 값을 낸 칸(담당자가 놓치면 틀린 기준이 저장될 수 있다), blank는 값이 있어야 하는데
    비워 두고 확인 대상으로 넘긴 칸(담당자가 직접 적어야 하지만 틀린 값은 아니다)의 수다.
    """
    problems: list[str] = []
    cells = [0, 0]
    wrong = blank = 0
    if spec.get("blocked"):
        ok = bool(result.get("blocked"))
        return {
            "pass": ok,
            "problems": [] if ok else ["지시문을 막지 못했다"],
            "cells": [int(ok), 1],
        }
    if result.get("blocked"):
        return {"pass": False, "problems": ["문서가 막혔다(Prompt Shields)"], "cells": [0, 1]}
    draft = result.get("draft") or {}
    if draft.get("outcome") not in spec["outcome"]:
        problems.append(f"결과 종류 {draft.get('outcome')} (기대 {spec['outcome']})")
        if draft.get("failure"):
            problems.append(f"실패: {draft['failure']}")
    rows = draft.get("rows") or []
    if len(rows) != spec["rowCount"]:
        problems.append(f"행 수 {len(rows)} (기대 {spec['rowCount']})")
    for item in spec["rows"]:
        row = _find(rows, item["match"])
        label = "/".join(str(v) for v in item["match"].values())
        if row is None:
            problems.append(f"{label} 행이 없다")
            cells[1] += len(item["expect"]) + len(item.get("review", []))
            continue
        for key, expected in item["expect"].items():
            cells[1] += 1
            actual = GETTERS[key](row)
            if _same(actual, expected):
                cells[0] += 1
            else:
                problems.append(f"{label} {key}: {actual!r} (기대 {expected!r})")
                if actual is None:
                    blank += 1
                else:
                    wrong += 1
        for key in item.get("review", []):
            cells[1] += 1
            if row[REVIEW_KEYS[key]]["review"]:
                cells[0] += 1
            else:
                problems.append(f"{label} {key}: 확인 대상이 아니다")
    return {
        "pass": not problems,
        "problems": problems,
        "cells": cells,
        "wrong": wrong,
        "blank": blank,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", nargs="*", help="이 파일 이름만(예: 01_basic.pdf)")
    parser.add_argument("--out", help="결과 JSON을 쓸 곳")
    parser.add_argument("--show", action="store_true", help="행마다 읽은 값을 찍는다")
    args = parser.parse_args()

    from app.v3 import service

    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    names = [n for n in expected if not n.startswith("_") and (not args.only or n in args.only)]
    app = build_app()
    report: dict[str, Any] = {
        "date": date.today().isoformat(),
        "model": app.criteria_reader.model,
        "runs": args.runs,
        "documents": {},
    }
    for name in names:
        spec = expected[name]
        data = (FIXTURES / name).read_bytes()
        runs = []
        for attempt in range(args.runs):
            started = time.monotonic()
            try:
                result = service.upload_document(
                    app, VENDOR, "purchase_criteria", name, data, VENDOR
                )
            except service.ServiceError as error:
                result = {"error": f"{error.code}: {error.message}"}
            seconds = round(time.monotonic() - started, 1)
            graded = (
                {"pass": False, "problems": [result["error"]], "cells": [0, 1]}
                if "error" in result
                else grade(result, spec)
            )
            draft = result.get("draft") or {}
            failed_rows = (
                [
                    {
                        "id": row["id"],
                        "quote": row["quote"],
                        "price": [
                            row["price"]["raw"],
                            row["price"]["unitRaw"],
                            row["price"]["amount"],
                        ],
                        "minMonthlyKg": [
                            row["minMonthlyKg"]["raw"],
                            row["minMonthlyKg"]["unitRaw"],
                            row["minMonthlyKg"]["per"],
                            row["minMonthlyKg"]["value"],
                        ],
                        "review": row["review"],
                    }
                    for row in draft.get("rows") or []
                ]
                if not graded["pass"]
                else None
            )
            runs.append(
                {
                    **graded,
                    "rows": failed_rows,
                    "seconds": seconds,
                    "outcome": draft.get("outcome"),
                    "input": (draft.get("reader") or {}).get("input"),
                    "blocked": result.get("blocked"),
                }
            )
            mark = "통과" if graded["pass"] else "실패"
            print(
                f"[{name} #{attempt + 1}] {mark} · {seconds}s · {draft.get('outcome') or ('막힘' if result.get('blocked') else '-')}"
                f" · 칸 {graded['cells'][0]}/{graded['cells'][1]}"
            )
            for problem in graded["problems"]:
                print(f"    - {problem}")
            if args.show and draft:
                for row in draft.get("rows") or []:
                    print(
                        f"    {row['id']} p{row['page']} {row['material']['value']} {row['grade']['value']} "
                        f"{row['form']['value']} | 단가 {row['price']['amount']} {row['price']['direction']}"
                        f" ({row['price']['raw']} {row['price']['unitRaw']}) | 최소 {row['minMonthlyKg']['value']}"
                        f" ({row['minMonthlyKg']['raw']} {row['minMonthlyKg']['unitRaw']} {row['minMonthlyKg']['per']})"
                        f" | 이물 {row['maxContaminationPct']['value']} 수분 {row['maxMoisturePct']['value']}"
                        f" | 수거 {row['collectionFee']['value']} | 확인 {row['review']}"
                    )
                for common in draft.get("common") or []:
                    print(
                        f"    공통 {common['number']} {common['field']} {common['rows']} {common['quote']}"
                    )
        report["documents"][name] = {"title": spec.get("title"), "runs": runs}
    total = [r for doc in report["documents"].values() for r in doc["runs"]]
    cells = [sum(r["cells"][0] for r in total), sum(r["cells"][1] for r in total)]
    report["summary"] = {
        "documents": len(names),
        "attempts": len(total),
        "passed": sum(r["pass"] for r in total),
        "cellAccuracy": round(cells[0] / cells[1], 4) if cells[1] else None,
        # 틀린 값(확정 후보로 잘못 나온 칸)과 비워서 확인으로 넘긴 칸, 외부 서비스 연결 실패 회차
        "wrongCells": sum(r.get("wrong", 0) for r in total),
        "blankCells": sum(r.get("blank", 0) for r in total),
        "serviceErrors": sum(
            1 for r in total if any(p.startswith(("DOCUMENT_", "SHIELD_")) for p in r["problems"])
        ),
        "cells": cells,
        "medianSeconds": sorted(r["seconds"] for r in total)[len(total) // 2] if total else None,
    }
    print(json.dumps(report["summary"], ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if report["summary"]["passed"] == len(total) else 1


if __name__ == "__main__":
    sys.exit(main())
