"""평가셋의 판 관리와 동결(평가 설계 14장).

    load_scenarios("test-v1") / load_variants("test-v1") / load_split("dev" | "test")
    build_manifest("test-v1") -> dict           # 파일마다 SHA-256, 건수, 만든 날, 라벨링 지침 판
    verify("test-v1") -> [문제, ...]            # 빈 목록이면 동결 그대로다
    python3 -m app.v3.eval.sets --freeze test-v1   # MANIFEST.json을 처음 쓸 때만

- 개발 사례는 app/v3/eval/cases/(C01~C50), 시험셋은 app/v3/eval/sets/<판>/에 있다.
- 시험셋 파일과 그 시험셋이 쓰는 말뭉치(sets/corpus/)의 해시를 MANIFEST.json에 적는다. CI 시험이
  해시를 대조해 다르면 실패한다. 고치려면 새 판(test-v2)을 만든다. 판이 다른 결과끼리는 숫자를
  비교하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from app.v3.eval.loader import load_cases

SETS_DIR = Path(__file__).with_name("sets")
CURRENT_TEST = "test-v1"
MANIFEST = "MANIFEST.json"
SHARED = ("corpus/laws_extra.json",)  # 시험셋이 함께 쓰는 말뭉치(sets/ 기준 경로)
LABEL_GUIDE = "v1"
# 파일 안에서 건수를 셀 배열의 키
COUNT_KEYS = ("cases", "variants", "queries", "attacks", "probes", "items")
# 동결된 판의 정오표. 기대가 틀렸다고 정한 사례와 이유다. 동결된 파일은 고치지 않으므로 그 판의
# 결과에는 그대로 들어가고, 리포트가 이유를 함께 적는다. 다음 판에서 고친다(sets/README.md 정오표).
ERRATA: dict[str, dict[str, str]] = {
    "test-v1": {
        "T042": (
            "기대 오류(test-v2에서 고친다): st_a의 calculated 금지는 틀렸다. 출처(PIR·PCR)는 매각 "
            "경로의 매입 기준과 적재 검사에 쓰이지 않으므로(설계안 v3 8.2·8.3, C36) 담당자가 "
            "모른다고 답해도 확인할 것이 남지 않는다. 모르는 출처는 unresolved와 unknownFields에 "
            "남는다. st_b의 금지와 나머지 기대는 그대로 맞다"
        ),
    },
}


def errata(version: str = CURRENT_TEST) -> dict[str, str]:
    """그 판의 정오표 {사례 ID: 이유}."""
    return dict(ERRATA.get(version) or {})


def set_dir(version: str) -> Path:
    return SETS_DIR / version


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_scenarios(version: str = CURRENT_TEST) -> list[dict[str, Any]]:
    data = _read(set_dir(version) / "scenarios.json")
    return [{**case, "file": f"{version}/scenarios.json"} for case in data["cases"]]


def load_variants(version: str = CURRENT_TEST) -> list[dict[str, Any]]:
    return list(_read(set_dir(version) / "variants.json")["variants"])


def scenario_meta(version: str = CURRENT_TEST) -> dict[str, Any]:
    data = _read(set_dir(version) / "scenarios.json")
    return {k: v for k, v in data.items() if k != "cases"}


def load_split(split: str, version: str = CURRENT_TEST) -> list[dict[str, Any]]:
    """L4 시나리오. dev = C01~C50, test = 시험셋 시나리오."""
    if split == "dev":
        return load_cases()
    if split == "test":
        return load_scenarios(version)
    raise ValueError(f"split은 dev 또는 test다: {split}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _count(path: Path) -> dict[str, int]:
    if path.suffix != ".json":
        return {}
    data = _read(path)
    if isinstance(data, list):
        return {"items": len(data)}
    return {k: len(data[k]) for k in COUNT_KEYS if isinstance(data.get(k), list)}


def _files(version: str) -> list[tuple[str, Path]]:
    folder = set_dir(version)
    own = [
        (p.relative_to(folder).as_posix(), p)
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != MANIFEST
    ]
    shared = [(f"../{rel}", SETS_DIR / rel) for rel in SHARED if (SETS_DIR / rel).exists()]
    return own + shared


def build_manifest(version: str = CURRENT_TEST, *, created: str = "2026-11-18") -> dict[str, Any]:
    files = {
        rel: {"sha256": sha256(path), "bytes": path.stat().st_size, "count": _count(path)}
        for rel, path in _files(version)
    }
    return {
        "version": version,
        "created": created,
        "labelingGuide": LABEL_GUIDE,
        "frozen": True,
        "note": (
            "동결된 시험셋. 파일을 고치면 CI의 동결 시험이 실패한다. 고치려면 새 판(test-v2)을 "
            "만들고 MANIFEST를 새로 쓴다(app/v3/eval/sets/README.md)."
        ),
        "files": files,
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    """리포트 머리에 적는 매니페스트 해시(파일 목록과 해시를 정렬한 JSON의 SHA-256)."""
    canonical = json.dumps(manifest["files"], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_manifest(version: str = CURRENT_TEST) -> dict[str, Any] | None:
    path = set_dir(version) / MANIFEST
    return _read(path) if path.exists() else None


def verify(version: str = CURRENT_TEST) -> list[str]:
    """동결 시험. 매니페스트와 다른 파일, 빠진 파일, 매니페스트에 없는 파일을 돌려준다."""
    manifest = read_manifest(version)
    if manifest is None:
        return [f"{version}: {MANIFEST}가 없다(아직 동결하지 않았다)"]
    problems = []
    present = dict(_files(version))
    for rel, entry in manifest["files"].items():
        path = present.get(rel)
        if path is None:
            problems.append(f"{rel}: 파일이 없다")
        elif sha256(path) != entry["sha256"]:
            problems.append(f"{rel}: 해시가 매니페스트와 다르다(동결된 파일을 고쳤다)")
    for rel in present:
        if rel not in manifest["files"]:
            problems.append(f"{rel}: 매니페스트에 없는 파일이다")
    return problems


def freeze(version: str) -> dict[str, Any]:
    path = set_dir(version) / MANIFEST
    if path.exists():
        raise SystemExit(f"{path}가 이미 있다. 동결된 판은 고치지 않는다. 새 판을 만든다.")
    manifest = build_manifest(version)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="평가셋 동결과 확인")
    parser.add_argument("--freeze", metavar="VERSION", help="MANIFEST.json을 처음 쓴다")
    parser.add_argument("--verify", metavar="VERSION", default=None)
    args = parser.parse_args(argv)
    if args.freeze:
        manifest = freeze(args.freeze)
        sys.stdout.write(
            f"{args.freeze} 동결: 파일 {len(manifest['files'])}개, {manifest_hash(manifest)}\n"
        )
        return 0
    version = args.verify or CURRENT_TEST
    problems = verify(version)
    sys.stdout.write("\n".join(problems) + "\n" if problems else f"{version}: 동결 그대로\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
