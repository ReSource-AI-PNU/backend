"""회사 측·업체 측 에이전트를 Foundry Agent Service에 Prompt Agent로 등록한다.

로컬 미실행(실제 호출): Azure를 부르는 부분은 실행해 보지 못했다. 등록 계획(이름, 모델, 지시문)은
tests/v3/test_maf.py가 로컬에서 확인한다. --dry-run은 아무것도 부르지 않는다.

    FOUNDRY_PROJECT_ENDPOINT=... FOUNDRY_MODEL=... python -m scripts.v3.register_foundry_agents [--dry-run]

- 지시문은 런타임의 시스템 프롬프트와 같은 글이다(app.v3.agents.maf.instructions_for).
- 등록한 뒤 출력된 버전을 FOUNDRY_COMPANY_AGENT_VERSION, FOUNDRY_VENDOR_AGENT_VERSION에 넣고
  V3_AGENT_FRAMEWORK=agent로 띄우면 런타임이 등록한 에이전트를 부른다.
- 인증은 DefaultAzureCredential(관리 ID나 az 로그인)이다. 키를 받지 않는다.
- 등록한 에이전트에 판단 도구(MCP)를 붙이지 않는다. 도구는 코드가 조직을 정해 부른다
  (Prompt Agent 정의에 MCP 도구를 넣는 방법은 공식 문서에서 확인하지 못했다).

확인한 API (2026-11-17, azure-ai-projects 2.x 빠른 시작):
  AIProjectClient(endpoint=, credential=DefaultAzureCredential())
  project.agents.create_version(agent_name=, definition=PromptAgentDefinition(model=, instructions=))
  → .id, .name, .version
  https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent
필요한 패키지: azure-ai-projects>=2, azure-identity
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from app.v3.agents import maf

ROLES = ("company", "vendor")


def plan(env: Mapping[str, str], model: str | None = None) -> list[dict[str, str]]:
    """등록할 에이전트 목록. 이름은 환경 변수로 바꿀 수 있다."""
    chosen = (model or env.get("FOUNDRY_MODEL") or env.get("FOUNDRY_DEPLOYMENT") or "").strip()
    if not chosen:
        raise ValueError("모델 배포 이름이 없다. --model이나 FOUNDRY_MODEL로 준다")
    items = []
    for role in ROLES:
        prefix = "FOUNDRY_COMPANY_AGENT" if role == "company" else "FOUNDRY_VENDOR_AGENT"
        instructions = maf.instructions_for(role)
        items.append(
            {
                "role": role,
                "name": (env.get(f"{prefix}_NAME") or maf.AGENT_NAMES[role]).strip(),
                "versionVariable": f"{prefix}_VERSION",
                "model": chosen,
                "instructions": instructions,
                "instructionsDigest": hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:12],
            }
        )
    return items


def project_client(endpoint: str) -> Any:
    """로컬 미실행."""
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def register(project: Any, items: list[dict[str, str]]) -> list[dict[str, str]]:
    """에이전트마다 새 버전을 등록한다. 로컬 미실행(가짜 project로만 확인)."""
    from azure.ai.projects.models import PromptAgentDefinition

    return register_with(project, items, PromptAgentDefinition)


def register_with(
    project: Any, items: list[dict[str, str]], definition: Callable[..., Any]
) -> list[dict[str, str]]:
    done = []
    for item in items:
        created = project.agents.create_version(
            agent_name=item["name"],
            definition=definition(model=item["model"], instructions=item["instructions"]),
        )
        done.append(
            {
                "role": item["role"],
                "name": str(created.name),
                "version": str(created.version),
                "id": str(created.id),
                "versionVariable": item["versionVariable"],
            }
        )
    return done


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    project: Any = None,
    definition: Callable[..., Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="두 에이전트를 Foundry Agent Service에 등록한다")
    parser.add_argument("--model", default=None, help="모델 배포 이름(없으면 FOUNDRY_MODEL)")
    parser.add_argument("--dry-run", action="store_true", help="등록할 내용만 보여 준다(호출 없음)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        items = plan(env, args.model)
    except ValueError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 2
    for item in items:
        print(
            f"{item['role']}: 이름 {item['name']}, 모델 {item['model']}, "
            f"지시문 {len(item['instructions'])}자(지문 {item['instructionsDigest']})"
        )
    if args.dry_run:
        print("등록하지 않았다(--dry-run).")
        return 0
    endpoint = (env.get("FOUNDRY_PROJECT_ENDPOINT") or "").strip()
    if not endpoint and project is None:
        print(
            "FOUNDRY_PROJECT_ENDPOINT(Foundry 프로젝트 주소)가 없다. 아무것도 등록하지 않았다.",
            file=sys.stderr,
        )
        return 2
    target = project if project is not None else project_client(endpoint)
    done = (
        register_with(target, items, definition)
        if definition is not None
        else register(target, items)
    )
    for item in done:
        print(
            f"등록: {item['name']} 버전 {item['version']} → {item['versionVariable']}={item['version']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
