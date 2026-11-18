"""근거 레지스트리의 근거를 Azure AI Search 색인에 올린다.

2026-11-18 맥에서 실제로 돌렸다: V3_EMBEDDINGS=foundry(text-embedding-3-small, 1536차원)로
rsai-evidence 색인을 만들고 시드 근거 48건을 올렸다. Ollama 임베딩 경로는 실제로 돌려 보지 못했다.
순서와 요약은 tests/v3/test_index_evidence.py가 가짜 transport로 확인한다.

    python -m scripts.v3.index_evidence [--create-index] [--kinds law,ref] [--dry-run]

환경 변수:
- AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_API_KEY (필수), AZURE_SEARCH_INDEX(기본 rsai-evidence)
- V3_EMBEDDINGS = ollama | foundry | (비움). 색인과 질의가 같은 설정을 써야 한다.
  ollama: OLLAMA_BASE_URL, OLLAMA_EMBED_MODEL(기본 bge-m3, 1024차원)
  foundry: FOUNDRY_ENDPOINT, FOUNDRY_API_KEY, FOUNDRY_EMBEDDING_DEPLOYMENT
  (차원이 1024가 아니면 AZURE_SEARCH_VECTOR_DIMENSIONS도 맞춘다)
- DATABASE_URL이 있으면 SQL 레지스트리의 근거를, 없으면 저장소의 시드(법령·기준값·가정값)를 올린다.

조직 경계: 문서마다 access를 싣고 검색할 때 조직 필터를 건다(search_azure.org_filter).
calc 근거는 기본으로 올리지 않는다(도구를 부를 때마다 생기고, 화면은 ID로 바로 연다).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Mapping
from typing import Any

from app.v3.evidence import search_azure
from app.v3.evidence.model import KINDS

DEFAULT_KINDS = tuple(kind for kind in KINDS if kind != "calc")


def build_registry(env: Mapping[str, str]) -> Any:
    """DATABASE_URL이 있으면 API와 같은 SQL 레지스트리(로컬 미실행), 없으면 시드만 든 메모리."""
    if (env.get("DATABASE_URL") or "").strip():
        from app.platform.db import engine
        from app.v3.sql import SqlRegistry

        return SqlRegistry(engine())
    from app.v3.evidence.registry import InMemoryRegistry
    from app.v3.evidence.seeds import load_seed_dir
    from app.v3.judge.defaults import register_defaults

    registry = InMemoryRegistry()
    load_seed_dir(registry)
    register_defaults(registry)
    return registry


def parse_kinds(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_KINDS
    if raw.strip() == "all":
        return tuple(KINDS)
    kinds = tuple(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValueError(f"알 수 없는 근거 종류: {', '.join(unknown)}")
    return kinds


def run(
    registry: Any,
    client: Any,
    *,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
    create_index: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """근거를 읽어 색인에 올리고 요약을 돌려준다. dry_run이면 아무것도 부르지 않는다."""
    items = registry.list(kinds=kinds, org_id=None)  # 시스템 호출: 조직 것도 access와 함께 올린다
    by_kind = {kind: sum(1 for e in items if e.kind == kind) for kind in kinds}
    summary = {
        "index": client.index,
        "kinds": list(kinds),
        "found": len(items),
        "byKind": by_kind,
        "private": sum(1 for e in items if "*" not in e.access),
        "createdIndex": False,
        "uploaded": 0,
        "dryRun": dry_run,
    }
    if dry_run:
        return summary
    if create_index:
        client.create_index()
        summary["createdIndex"] = True
    summary["uploaded"] = client.upsert(items)
    return summary


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    transport: Any = None,
    embed_transport: Any = None,
    registry: Any = None,
) -> int:
    parser = argparse.ArgumentParser(description="근거를 Azure AI Search 색인에 올린다")
    parser.add_argument(
        "--create-index", action="store_true", help="색인을 만들거나 고친 뒤 올린다"
    )
    parser.add_argument("--kinds", default=None, help="쉼표로 나눈 근거 종류. all이면 calc까지")
    parser.add_argument("--dry-run", action="store_true", help="무엇을 올릴지만 센다(호출 없음)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        kinds = parse_kinds(args.kinds)
        client = search_azure.client_from_env(
            env,
            transport=transport,
            embed_transport=embed_transport,
            # 임베딩 한도(429)나 연결 끊김이면 기다렸다 다시 보낸다. 시험에서 가짜 transport면 기다리지 않는다
            sleep=time.sleep if transport is None else None,
        )
    except ValueError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 2
    if client is None:
        print(
            "AZURE_SEARCH_ENDPOINT와 AZURE_SEARCH_API_KEY가 있어야 한다. 아무것도 올리지 않았다.",
            file=sys.stderr,
        )
        return 2
    try:
        summary = run(
            registry if registry is not None else build_registry(env),
            client,
            kinds=kinds,
            create_index=args.create_index,
            dry_run=args.dry_run,
        )
    except search_azure.AzureSearchError as error:
        print(f"색인 호출 실패: HTTP {error.status}", file=sys.stderr)
        return 1
    vectors = "벡터 포함" if (env.get("V3_EMBEDDINGS") or "").strip() else "벡터 없음"
    done = "셈만 했다(호출 없음)" if summary["dryRun"] else f"{summary['uploaded']}건을 올렸다"
    print(f"색인 {summary['index']}: 근거 {summary['found']}건 가운데 {done} ({vectors})")
    for kind, count in summary["byKind"].items():
        if count:
            print(f"  {kind}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
