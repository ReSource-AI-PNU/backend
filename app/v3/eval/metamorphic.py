"""변형(metamorphic) 시험의 관계 판정(평가 설계 12.3, CheckList의 INV·DIR).

    check_relation(variant, base_observed, variant_observed) -> {"ok", "why"}
    run_variants(variants, base_trials, ...) -> {"results": [...], "consistency": ...}

변형의 기대는 정답 끝 상태가 아니라 기본 사례와의 관계다. 끝 상태는 runner의 관찰
(assignments, threads)을 쓴다. 금액은 반올림 안(1원)에서 비교한다.
"""

from __future__ import annotations

from typing import Any

from app.v3.eval import stats
from app.v3.eval.trials import Meter, run_trial

MONEY = ("unitPrice", "monthlyAfter", "monthlyNow", "difference")
TOLERANCE = 1.0  # 원


def _assignment_map(observed: dict[str, Any], mapping: dict[str, str] | None = None) -> dict:
    mapping = mapping or {}
    out = {}
    for sid, item in (observed.get("assignments") or {}).items():
        out[mapping.get(sid, sid)] = {
            "vendor": mapping.get(item.get("vendor"), item.get("vendor")),
            "route": item.get("route"),
            "status": item.get("status"),
            **{k: item.get(k) for k in MONEY},
        }
    return out


def _threads(observed: dict[str, Any], mapping: dict[str, str] | None = None) -> dict:
    mapping = mapping or {}
    out: dict[str, list[str]] = {}
    for thread in observed.get("threads") or []:
        vendor = mapping.get(thread["vendor"], thread["vendor"])
        out.setdefault(vendor, []).append(thread["status"])
    return {k: sorted(v) for k, v in out.items()}


def _same_money(a: Any, b: Any, factor: float = 1.0) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) * factor - float(b)) <= TOLERANCE * max(1.0, factor)


def _shape(assignments: dict) -> dict:
    return {sid: (v["vendor"], v["route"], v["status"]) for sid, v in assignments.items()}


def check_relation(
    variant: dict[str, Any], base: dict[str, Any], seen: dict[str, Any]
) -> dict[str, Any]:
    """base와 seen은 trials.run_trial의 observed다. 관계를 지켰으면 ok=True."""
    relation, params = variant["relation"], variant.get("params") or {}
    if relation == "rename":
        reverse = {new: old for old, new in params["mapping"].items()}
        a, b = _assignment_map(base), _assignment_map(seen, reverse)
        same = _shape(a) == _shape(b) and all(
            _same_money(a[s][k], b.get(s, {}).get(k)) for s in a for k in MONEY
        )
        threads_same = _threads(base) == _threads(seen, reverse)
        return _result(same and threads_same, "이름·ID·순서를 바꾸면 끝 상태가 같아야 한다")
    if relation == "paraphrase":
        a, b = _assignment_map(base), _assignment_map(seen)
        same = _shape(a) == _shape(b) and all(
            _same_money(a[s][k], b.get(s, {}).get(k)) for s in a for k in MONEY
        )
        return _result(
            same and _threads(base) == _threads(seen),
            "담당자 답을 말만 바꿔 적으면(2% → 2 퍼센트) 끝 상태가 같아야 한다",
        )
    if relation == "add_unpermitted_vendor":
        extra = params["vendor"]
        a, b = _assignment_map(base), _assignment_map(seen)
        opened = [s for s in _threads(seen).get(extra, []) if s != "not_opened"]
        assigned = [sid for sid, v in b.items() if v["vendor"] == extra]
        base_threads = _threads(base)
        seen_threads = {k: v for k, v in _threads(seen).items() if k != extra}
        ok = _shape(a) == _shape(b) and not opened and not assigned and base_threads == seen_threads
        return _result(
            ok, "허가 품목이 없는 업체를 더해도 배정이 같고 그 업체와 대화가 없어야 한다"
        )
    if relation == "scale_prices":
        factor = float(params["factor"])
        a, b = _assignment_map(base), _assignment_map(seen)
        ok = _shape(a) == _shape(b) and all(
            _same_money(a[s][k], b.get(s, {}).get(k), factor) for s in a for k in MONEY
        )
        return _result(
            ok, "모든 단가와 현행 단가에 같은 배수를 곱하면 배정이 같고 손익은 같은 배수다"
        )
    if relation == "contamination_over":
        sid, vendor = params["stream"], params["vendor"]
        got = _assignment_map(seen).get(sid) or {}
        ok = got.get("vendor") != vendor or got.get("status") in (
            "conditional",
            "needs_information",
        )
        return _result(
            ok, "측정 오염도를 업체 상한 위로 올리면 그 업체 배정이 없어지거나 조건부여야 한다"
        )
    if relation == "raise_price":
        sid, vendor = params["stream"], params["vendor"]
        before = (_assignment_map(base).get(sid) or {}).get("vendor")
        after = (_assignment_map(seen).get(sid) or {}).get("vendor")
        ok = before != vendor or after == vendor
        return _result(ok, "한 업체의 매입 단가를 올려도 그 업체의 순위가 내려가지 않아야 한다")
    if relation == "below_min":
        # 금액 claim은 그 업체와의 거래 금액(단가)이다. 현행 유지(keep)의 월 손익은 지금 처리비라
        # 거래 금액이 아니다(C33: 최소 물량 미달은 배정하지 않고 대안으로 둔다).
        sid, vendor = params["stream"], params["vendor"]
        got = _assignment_map(seen).get(sid) or {}
        no_deal = got.get("vendor") != vendor or got.get("unitPrice") is None
        ok = no_deal or got.get("status") in ("conditional", "needs_information")
        return _result(
            ok, "물량을 최소 물량 아래로 내리면 그 업체 금액 claim이 없거나 조건부여야 한다"
        )
    if relation == "remove_permit":
        sid, vendor = params["stream"], params["vendor"]
        opened = [
            t
            for t in seen.get("threads") or []
            if t["vendor"] == vendor and t["status"] != "not_opened" and sid in t["streams"]
        ]
        got = _assignment_map(seen).get(sid) or {}
        ok = not opened and got.get("vendor") != vendor
        return _result(
            ok, "업체 허가 품목에서 그 재질을 지우면 그 흐름으로 대화가 열리지 않아야 한다"
        )
    raise ValueError(f"모르는 관계: {relation}")


def _result(ok: bool, why: str) -> dict[str, Any]:
    return {"ok": bool(ok), "why": why}


def run_variants(
    variants: list[dict[str, Any]],
    base_trials: dict[str, dict[str, Any]],
    *,
    options: dict[str, Any] | None = None,
    meter: Meter | None = None,
) -> list[dict[str, Any]]:
    """변형마다 한 번 돈다. base_trials는 기본 사례 ID → run_trial 결과(첫 반복)."""
    meter = meter or Meter(None)
    options = options or {}
    out = []
    for variant in variants:
        trial = run_trial(variant["case"], options, meter=meter)
        base = base_trials.get(variant["base"])
        if base is None:
            verdict = {"ok": False, "why": "기본 사례를 돌리지 않았다"}
        elif trial["error"]:
            verdict = {"ok": False, "why": f"실행 오류: {trial['error'][:160]}"}
        else:
            verdict = check_relation(variant, base["observed"], trial["observed"])
        policy_ok = trial["violations"] == 0 and not trial["error"]
        out.append(
            {
                "id": variant["id"],
                "base": variant["base"],
                "kind": variant["kind"],
                "relation": variant["relation"],
                "ok": verdict["ok"],
                "why": verdict["why"],
                "violations": [v["kind"] for v in trial["violationItems"]],
                "error": trial["error"],
                "consistent": bool(verdict["ok"] and policy_ok),
            }
        )
    return out


def variant_metrics(results: list[dict[str, Any]], *, resamples: int = stats.RESAMPLES) -> dict:
    """변형 일관성 = 관계를 지킨 변형 / 변형. 기본 사례 단위로 묶어 CI를 낸다(4.3)."""
    rows = [
        stats.rate_metric(
            "variantConsistency",
            "변형 일관성",
            [r["consistent"] for r in results],
            clusters=[r["base"] for r in results],
            goal=stats.target(">=", 0.95, "≥ 0.95 (규칙 정책은 1.00)"),
            resamples=resamples,
            note="관계를 지키고 금지 위반이 없는 변형의 비율. n은 기본 사례(묶음) 수",
        )
    ]
    by: dict[str, list[bool]] = {}
    for r in results:
        by.setdefault(r["relation"], []).append(r["consistent"])
    table = {
        "title": "변형 관계별 일관성",
        "columns": ["관계", "종류", "n", "지킴", "비율", ""],
        "rows": [
            [
                relation,
                next(r["kind"] for r in results if r["relation"] == relation),
                len(flags),
                sum(flags),
                round(sum(flags) / len(flags), 3),
                "표본 적음" if len(flags) < stats.SMALL_SAMPLE else "",
            ]
            for relation, flags in sorted(by.items())
        ],
    }
    failed = {
        "title": "관계를 지키지 못한 변형",
        "columns": ["변형", "기본 사례", "관계", "이유", "위반", "오류"],
        "rows": [
            [
                r["id"],
                r["base"],
                r["relation"],
                r["why"],
                ", ".join(r["violations"]) or "-",
                (r["error"] or "-")[:100],
            ]
            for r in results
            if not r["consistent"]
        ],
    }
    return {"metrics": rows, "tables": [table, failed]}
