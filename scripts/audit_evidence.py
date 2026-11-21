"""Reproducible inventory of coefficient seeds and domain assumption references.

No remote source is fetched or classified as read by this script.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/v3/evidence-audit-2026-11-21.json"
PATTERN = re.compile(
    r"energy|virgin|emission|carbon|density|yield|substitution|detour|transport|pretreat|labor|currentTreatment|current_per_kg|minimum_kg|priceBand|askPct|pilotMonths|methodAssumed|산정 보류|가정값|시범 기간|다음 등급",
    re.IGNORECASE,
)


def audit():
    coefficients = []
    for path in sorted((ROOT / "app/v3/evidence/seeds").glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            if not isinstance(item.get("value"), (float, int)):
                continue
            meta = item.get("meta", {})
            coefficients.append(
                {
                    "file": path.relative_to(ROOT).as_posix(),
                    "id": item["id"],
                    "value": item["value"],
                    "unit": item.get("unit"),
                    "source": item.get("source"),
                    "version": item.get("version", 1),
                    "asOf": item.get("asOf"),
                    **meta,
                }
            )
    path = ROOT / "app/v3/judge/defaults.v1.json"
    defaults = json.loads(path.read_text(encoding="utf-8"))
    coefficients.extend(
        {"file": path.relative_to(ROOT).as_posix(), "version": defaults["version"], **item}
        for item in defaults["items"]
    )
    references = []
    for parent in ("app", "infra/h100/recon", "docs", "scripts", "../frontend/src"):
        for path in sorted((ROOT / parent).rglob("*")):
            if path.suffix not in (".py", ".json", ".md", ".csv", ".ts", ".tsx") or path == OUT:
                continue
            if any(part in ("cases", "__pycache__", "fixture-data") for part in path.parts) or (
                "scan" in path.parts and path.suffix == ".json"
            ):
                continue
            for line, text in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                if PATTERN.search(text):
                    references.append(
                        {
                            "file": path.relative_to(ROOT).as_posix(),
                            "line": line,
                            "text": text.strip()[:650],
                        }
                    )
    OUT.write_text(
        json.dumps(
            {
                "auditedOn": "2026-11-21",
                "scope": "backend app (v3 and legacy), defaults, coefficient seeds, synthetic demo, scripts, reconstruction, repository docs and frontend src; test/eval fixtures, bundled fixture-data and scan geometry JSON excluded from textual index",
                "classification": {
                    "measured": "① 사용자 실측·증빙",
                    "official": "② 공식 자료",
                    "literature": "③ 조건부 문헌",
                    "assumption": "④ 가정",
                    "policy": "⑤ 정책",
                    "hold": "⑥ 보류",
                },
                "coefficients": coefficients,
                "references": references,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(coefficients)} coefficient records, {len(references)} domain references -> {OUT.name}"
    )


if __name__ == "__main__":
    audit()
