"""업체 온보딩 대화 평가: 가상 업체 담당자 대본으로 실제 모델을 돌려 초안이 담당자의 말과 맞는지 잰다.

    OLLAMA_BASE_URL=http://127.0.0.1:11435 python -m scripts.v3.eval.onboarding_eval --model ollama:qwen2.5:32b
    python -m scripts.v3.eval.onboarding_eval --model foundry      # FOUNDRY_ENDPOINT·DEPLOYMENT·API_KEY

대본마다 담당자 말을 차례로 보내고, 끝난 뒤 초안을 기대값과 견준다.
- 맞음: 기대한 항목이 기대한 값으로 초안에 있다.
- 틀림: 기대한 항목이 다른 값으로 있다.
- 빠짐: 기대한 항목이 초안에 없다.
- 지어냄: 기대하지 않은 항목이 초안에 있다(담당자가 말하지 않았거나 애매해서 넣으면 안 되는 값).
- 답 정합: AI 답이 "초안에 넣었습니다"라고 한 차례에 실제로 초안이 바뀌었는가(코드가 답을 쓰므로 늘 맞아야 한다).
대본의 회사와 말은 모두 가상이다. 허가 명부 대본은 공공 명부에 실제로 있는 상호를 쓴다(명부 대조 경로를 보려고).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from app.v3.agents.llm import FoundryChat, OllamaChat
from app.v3.onboarding import agent as onboarding
from tests.v3.runtime_fixtures import make_app

WOORI = "㈜우리환경산업"  # 공공 명부에 한 곳뿐인 부산 금정구 업체
WEEKDAYS = ["월", "화", "수", "목", "금"]
ANY_PRICE = "price"  # 가장 낮게·높게 부를 단가 어느 칸이든 이 값이면 맞음

SCRIPTS: list[dict[str, Any]] = [
    {
        "id": "S1 명부 확인·톤 환산·평일",
        "org": WOORI,
        "turns": [
            "네 맞아요 그 금정구 사업장입니다",
            "집게차 3대 있고 한 대에 8톤씩 실어요",
            "평일에만 수거하고 금정구, 동래구, 해운대구까지 갑니다",
        ],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "fleet.vehicleCount": 3,
            "fleet.capacityKg": 8000,
            "guardrails.days": WEEKDAYS,
            "fleet.serviceAreas": ["금정구", "동래구", "해운대구"],
        },
    },
    {
        "id": "S2 모름·처리비 부호",
        "org": WOORI,
        "turns": [
            "맞습니다",
            "차량은 아직 정해지지 않았어요",
            "토요일만 가요. 처리비는 kg당 120원은 받아야 해요",
        ],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "guardrails.days": ["토"],
            ANY_PRICE: -120,
        },
    },
    {
        "id": "S3 애매한 단가는 넣지 않음",
        "org": WOORI,
        "turns": ["네 거기 맞아요", "단가는 kg당 50원이요"],
        "expect": {"permits": "registry", "fleet.depot": "registry"},
    },
    {
        "id": "S4 정정",
        "org": WOORI,
        "turns": ["맞아요", "차량은 2대예요", "아 2대가 아니라 4대예요. 한 대에 5톤이고요"],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "fleet.vehicleCount": 4,
            "fleet.capacityKg": 5000,
        },
    },
    {
        "id": "S5 처리 방식·받지 않는 조건",
        "org": WOORI,
        "turns": ["예 맞습니다", "저희는 열분해 하고요, PVC 섞인 건 안 받습니다"],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "process": "열분해",
            "guardrails.autoDecline": "PVC",
        },
    },
    {
        "id": "S6 거리·우리말 수",
        "org": WOORI,
        "turns": [
            "맞아요",
            "차고지에서 40km 안쪽까지만 가고, 한 달에 두 번 이상은 가야 합니다",
        ],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "fleet.maxPickupKm": 40,
            "guardrails.minPickupsPerMonth": 2,
        },
    },
    {
        "id": "S7 명부에 없는 상호",
        "org": "새빛자원(가상)",
        "turns": ["허가증상 상호는 새빛자원이고 사상구에 있어요", "차량은 1대, 2.5톤입니다"],
        "expect": {"fleet.vehicleCount": 1, "fleet.capacityKg": 2500},
    },
    {
        "id": "S8 잡담",
        "org": WOORI,
        "turns": ["안녕하세요 이거 뭐 하는 건가요?", "음 그냥 둘러보는 중이에요"],
        "expect": {},
    },
]

# 고치는 데 쓰지 않은 대본(2026-11-22, 규칙을 고친 뒤 한 번만 돌려 결과를 그대로 적는다)
HELD_OUT: list[dict[str, Any]] = [
    {
        "id": "H1 명부 확인·여러 값 한 번에",
        "org": WOORI,
        "turns": [
            "네네 회동동 거기 저희 맞습니다",
            "1톤 트럭 다섯 대로 돌고요 월요일이랑 목요일에 나갑니다",
        ],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "fleet.vehicleCount": 5,
            "fleet.capacityKg": 1000,
            "guardrails.days": ["월", "목"],
        },
    },
    {
        "id": "H2 매입 단가",
        "org": WOORI,
        "turns": ["맞아요", "PP 깨끗한 건 kg당 200원에 매입합니다. 그보다 낮게는 안 사요"],
        "expect": {"permits": "registry", "fleet.depot": "registry", ANY_PRICE: 200},
    },
    {
        "id": "H3 질문에 되묻기",
        "org": WOORI,
        "turns": ["잠깐만요 이거 저장하면 바로 다른 회사들한테 보이나요?"],
        "expect": {},
    },
    {
        "id": "H4 지역·거리",
        "org": WOORI,
        "turns": [
            "네 맞습니다",
            "부산 전역이랑 김해까지 가요. 차고지에서 60km 넘어가면 안 갑니다",
        ],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "fleet.serviceAreas": ["부산", "김해"],
            "fleet.maxPickupKm": 60,
        },
    },
    {
        "id": "H5 사업장 아님",
        "org": WOORI,
        "turns": ["아니요 저희는 그 주소가 아니고 강서구 녹산에 있어요"],
        "expect": {},
    },
    {
        "id": "H6 소각·횟수",
        "org": WOORI,
        "turns": ["맞아요", "저희는 소각 처리하고 한 달에 최소 8회는 수거해야 맞습니다"],
        "expect": {
            "permits": "registry",
            "fleet.depot": "registry",
            "process": "소각",
            "guardrails.minPickupsPerMonth": 8,
        },
    },
]


def make_llm(spec: str) -> Any:
    if spec == "foundry":
        return FoundryChat(
            os.environ["FOUNDRY_ENDPOINT"],
            os.environ["FOUNDRY_DEPLOYMENT"],
            os.environ["FOUNDRY_API_KEY"],
        )
    provider, _, name = spec.partition(":")
    if provider != "ollama" or not name:
        raise SystemExit("--model은 foundry 또는 ollama:<모델 이름>")
    return OllamaChat(os.environ["OLLAMA_BASE_URL"], name, num_ctx=8192)


def _matches(key: str, got: Any, want: Any) -> bool:
    if want == "registry":
        return got is not None
    if key == "guardrails.autoDecline":
        return isinstance(got, list) and any(str(want) in str(item) for item in got)
    if key == "fleet.serviceAreas":
        return isinstance(got, list) and all(any(w in str(g) for g in got) for w in want)
    if isinstance(want, list):
        return isinstance(got, list) and sorted(got) == sorted(want)
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(got - want) < 1e-6
    return got == want


def run_script(llm: Any, script: dict[str, Any]) -> dict[str, Any]:
    org = "org_eval"
    app = make_app(llm_factory=lambda role, org_id, model: llm if org_id == org else None)
    app.store.put_org({"id": org, "name": script["org"], "role": "vendor", "demo": False})
    app.store.put_vendor_profile(
        {"orgId": org, "permits": [], "criteria": [], "fleet": {}, "guardrails": {}}
    )
    turns, honest = [], True
    before: dict[str, Any] = {}
    for text in script["turns"]:
        started = time.time()
        state = onboarding.post_message(app, org, {"text": text}, org)
        seconds = time.time() - started
        draft = {d["key"]: d["value"] for d in state["draft"]}
        reply = state["messages"][-1]["text"]
        if "초안에 넣었습니다" in reply and draft == before:
            honest = False
        before = draft
        turns.append({"said": text, "reply": reply, "seconds": round(seconds, 1)})
    draft = {d["key"]: d["value"] for d in state["draft"]}
    expect = dict(script["expect"])
    out = {"correct": [], "wrong": [], "missed": [], "invented": []}
    price_keys = ("guardrails.floorPriceKrwPerKg", "guardrails.ceilingPriceKrwPerKg")
    if ANY_PRICE in expect:
        want = expect.pop(ANY_PRICE)
        got = [draft[k] for k in price_keys if k in draft]
        (out["correct"] if any(_matches("", g, want) for g in got) else out["missed"]).append(
            ANY_PRICE
        )
        for k in price_keys:
            draft.pop(k, None)
    for key, want in expect.items():
        if key not in draft:
            out["missed"].append(key)
        elif _matches(key, draft[key], want):
            out["correct"].append(key)
        else:
            out["wrong"].append(f"{key}={json.dumps(draft[key], ensure_ascii=False)}")
    out["invented"] = [
        f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in draft.items() if k not in expect
    ]
    return {"id": script["id"], **out, "honest": honest, "turns": turns}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="ollama:qwen2.5:32b")
    parser.add_argument("--out", default="")
    parser.add_argument("--set", choices=("dev", "held_out"), default="dev")
    args = parser.parse_args(argv)
    llm = make_llm(args.model)
    scripts = SCRIPTS if args.set == "dev" else HELD_OUT
    results = [run_script(llm, script) for script in scripts]
    totals = {
        k: sum(len(r[k]) for r in results) for k in ("correct", "wrong", "missed", "invented")
    }
    expected = totals["correct"] + totals["wrong"] + totals["missed"]
    seconds = [t["seconds"] for r in results for t in r["turns"]]
    summary = {
        "model": args.model,
        "set": args.set,
        "scripts": len(results),
        "turns": len(seconds),
        "expectedFields": expected,
        **totals,
        "recall": round(totals["correct"] / expected, 3) if expected else None,
        "honestReplies": all(r["honest"] for r in results),
        "medianSeconds": sorted(seconds)[len(seconds) // 2] if seconds else None,
    }
    for r in results:
        print(
            f"{r['id']}: 맞음 {len(r['correct'])} 틀림 {r['wrong']} 빠짐 {r['missed']} 지어냄 {r['invented']}"
        )
    print(json.dumps(summary, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
