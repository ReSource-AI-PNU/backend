"""시연 시나리오를 실제 모델로 처음부터 끝까지 돌려 모델의 형식 준수를 잰다(메모리 안에서만 돈다).

    uv run python scripts/v3/llm_conformance.py --llm foundry [--max-calls 120] [--out 결과.json]
                                               [--dump 기록.jsonl] [--today 2026-11-18]

- 모델 설정은 서버와 같은 함수(agents/factory.build_llm_factory)로 고른다. --llm foundry면 환경 변수
  FOUNDRY_ENDPOINT, FOUNDRY_DEPLOYMENT, FOUNDRY_API_KEY를 읽는다. 키 값은 출력하지 않는다.
- 업체가 ollama를 골랐는데 OLLAMA_BASE_URL이 없으면 그 업체는 규칙 정책으로 돈다(서버와 같다).
  --all-vendors를 주면 그 업체도 회사 측과 같은 모델이 답한다. 결과에는 업체마다 실제로 쓴 구현이 나온다.
- 담당자 질문에는 시연 값으로 답한다(오염도 0.5%, 수분 0.8%, 로트 일치 "예", 나머지는 "모름").
- 승인은 하지 않는다. 진단이 승인 대기(awaiting_approval)까지 가는지와 대화마다의 결과를 본다.

알려 주는 것: 대화마다 마지막 상태와 메시지 종류, 초안 수, 거부 코드별 수, 첫 초안 통과율,
모델 호출 수와 걸린 시간, 전체 걸린 시간. --dump를 주면 모델 입출력과 거부 사유(재작성 때 모델에게
돌려준 문장)를 JSONL로 남긴다(분석용. 저장소 밖 경로에 둔다).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.v3 import service
from app.v3.agents.factory import build_llm_factory, normalize_mode
from app.v3.agents.llm import LLMError
from app.v3.agents.policy_rule import RulePolicy
from app.v3.demo.seed import EMITTER
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore

ANSWERS: dict[str, Any] = {
    "contaminationPct": 0.5,
    "moisturePct": 0.8,
    "lotSame": "예",
}
UNKNOWN = "unknown"


class Meter:
    def __init__(self, max_calls: int, dump: Path | None) -> None:
        self.max_calls = max_calls
        self.calls: list[dict[str, Any]] = []
        self.dump = dump.open("w", encoding="utf-8") if dump else None

    def write(self, row: dict[str, Any]) -> None:
        if self.dump:
            self.dump.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            self.dump.flush()


class MeteredLLM:
    """호출 수와 걸린 시간을 센다. 모델 쪽 동작은 바꾸지 않는다."""

    def __init__(self, inner: Any, meter: Meter, role: str, org_id: str) -> None:
        self.inner, self.meter, self.role, self.org_id = inner, meter, role, org_id
        self.provider, self.model = inner.provider, inner.model

    def complete(self, messages, *, tools=None, response_schema=None):
        real = self.provider not in ("rule",)
        if real and sum(c["real"] for c in self.meter.calls) >= self.meter.max_calls:
            raise LLMError("측정 스크립트의 호출 한도를 넘었다")
        started = time.monotonic()
        response: dict[str, Any] = {}
        error = None
        try:
            response = self.inner.complete(messages, tools=tools, response_schema=response_schema)
        except Exception as caught:
            error = f"{type(caught).__name__}: {str(caught)[:300]}"
            raise
        finally:
            self._record(messages, response, error, round(time.monotonic() - started, 2), real)
        return response

    def _record(self, messages, response, error, seconds, real) -> None:
        usage = response.get("usage") or {}
        self.meter.calls.append(
            {
                "role": self.role,
                "org": self.org_id,
                "provider": self.provider,
                "real": real,
                "seconds": seconds,
                "error": error,
                "promptTokens": usage.get("prompt_tokens"),
                "completionTokens": usage.get("completion_tokens"),
            }
        )
        try:
            briefing = json.loads(messages[-1].get("content") or "{}")
        except (TypeError, json.JSONDecodeError):
            briefing = {}
        if not isinstance(briefing, dict):
            briefing = {}
        self.meter.write(
            {
                "role": self.role,
                "org": self.org_id,
                "provider": self.provider,
                "seconds": seconds,
                "task": briefing.get("task"),
                "incoming": (briefing.get("incoming") or {}).get("type"),
                "allowed": (briefing.get("thread") or {}).get("allowedTypes"),
                "guide": briefing.get("guide"),
                "feedback": briefing.get("rejections"),
                "error": error,
                "content": response.get("content"),
                "toolCalls": response.get("tool_calls"),
                "usage": usage or None,
            }
        )


def metered_factory(base: Any, meter: Meter, *, all_vendors: bool = False):
    """all_vendors면 ollama를 고른 업체도 회사 측과 같은 모델이 답한다(Ollama에 닿지 못할 때의 대용)."""

    def factory(role: str, org_id: str, model: dict[str, Any] | None = None) -> Any:
        llm = base(role, org_id, model) if base is not None else None
        if llm is None and all_vendors and base is not None:
            llm = base("company", org_id, None)
        return MeteredLLM(llm if llm is not None else RulePolicy(), meter, role, org_id)

    return factory


def _answer(field: str) -> Any:
    return ANSWERS.get(field, UNKNOWN)


def run(
    mode: str,
    *,
    today: str,
    max_calls: int,
    dump: Path | None = None,
    env: Any = None,
    all_vendors: bool = False,
) -> dict[str, Any]:
    meter = Meter(max_calls, dump)
    base = build_llm_factory(mode, os.environ if env is None else env, strict=True)
    app = service.App(
        InMemoryStore(),
        InMemoryRegistry(),
        llm_factory=metered_factory(base, meter, all_vendors=all_vendors),
        today=today,
    )
    service.seed(app)
    started = time.monotonic()
    session_id = service.create_session(app, EMITTER, EMITTER)["session"]["id"]
    view = service.run_session(app, session_id, EMITTER)
    answered = []
    for _ in range(6):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                value = _answer(question["field"])
                answered.append({"field": question["field"], "value": value})
                service.answer_question(app, session_id, question["id"], value, EMITTER)
        view = service.run_session(app, session_id, EMITTER)
    wall = round(time.monotonic() - started, 1)
    return report(app, session_id, view, meter, wall, answered)


def report(app, session_id, view, meter: Meter, wall: float, answered) -> dict[str, Any]:
    events = app.store.list_events(session_id)
    drafts: Counter[str] = Counter()
    first: Counter[str] = Counter()
    first_ok: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    per_thread: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "validation":
            continue
        detail = event.get("detail") or {}
        if "attempt" not in detail or detail.get("recheckedBy"):
            continue
        side = "vendor" if detail.get("reportedBy") == "vendor" else "company"
        thread_id = event.get("threadId")
        row = per_thread.setdefault(
            str(thread_id), {"drafts": 0, "rejections": Counter(), "firstTry": [0, 0]}
        )
        drafts[side] += 1
        row["drafts"] += 1
        if detail.get("attempt") == 1:
            first[side] += 1
            row["firstTry"][1] += 1
            if detail.get("ok"):
                first_ok[side] += 1
                row["firstTry"][0] += 1
        for item in detail.get("rejections") or []:
            codes[item["code"]] += 1
            row["rejections"][item["code"]] += 1
    threads = []
    for thread in view["threads"]:
        if thread["status"] == "not_opened":
            continue  # 허가 품목이 맞지 않아 열지 않은 대화. 모델이 쓴 것이 없다
        row = per_thread.get(
            thread["id"], {"drafts": 0, "rejections": Counter(), "firstTry": [0, 0]}
        )
        threads.append(
            {
                "vendorOrgId": thread["vendorOrgId"],
                "vendorModel": {
                    k: (thread.get("vendorModel") or {}).get(k) for k in ("provider", "name")
                },
                "status": thread["status"],
                "types": [m["type"] for m in thread["messages"]],
                "drafts": row["drafts"],
                "firstTry": f"{row['firstTry'][0]}/{row['firstTry'][1]}",
                "rejections": dict(row["rejections"]),
            }
        )
    real = [c for c in meter.calls if c["real"]]
    total_first = sum(first.values())
    by_role = Counter(f"{c['role']}:{c['provider']}" for c in meter.calls)
    settled = [t for t in threads if t["status"] in ("agreed", "confirmed")]
    return {
        "sessionStatus": view["session"]["status"],
        "resultStatus": (view.get("result") or {}).get("status"),
        "approvals": len(view.get("approvals") or []),
        "agreedThreads": f"{len(settled)}/{len(threads)}",
        "answered": answered,
        "threads": threads,
        "drafts": dict(drafts),
        "firstTryPassRate": round(sum(first_ok.values()) / total_first, 3) if total_first else None,
        "firstTry": {k: f"{first_ok[k]}/{first[k]}" for k in first},
        "rejections": dict(codes),
        "modelCalls": len(real),
        "callsByRole": dict(by_role),
        "modelErrors": sum(1 for c in real if c["error"]),
        "modelSeconds": round(sum(c["seconds"] for c in real), 1),
        "slowestCall": max((c["seconds"] for c in real), default=0),
        "promptTokens": sum(c["promptTokens"] or 0 for c in real),
        "completionTokens": sum(c["completionTokens"] or 0 for c in real),
        "wallSeconds": wall,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--llm", default=None, help="rule | foundry | anthropic (기본: V3_LLM)")
    parser.add_argument("--today", default="2026-11-18")
    parser.add_argument("--max-calls", type=int, default=120)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dump", type=Path)
    parser.add_argument(
        "--all-vendors",
        action="store_true",
        help="ollama를 고른 업체도 회사 측과 같은 모델로 답하게 한다(Ollama에 닿지 못할 때)",
    )
    args = parser.parse_args(argv)
    mode = normalize_mode(args.llm or os.environ.get("V3_LLM"))
    result = run(
        mode,
        today=args.today,
        max_calls=args.max_calls,
        dump=args.dump,
        all_vendors=args.all_vendors,
    )
    result = {"llm": mode, **result}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
