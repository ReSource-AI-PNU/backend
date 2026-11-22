"""L5 협상 품질(평가 설계 9장).

    run_layer(split="test", llm_factory=None) -> stats.layer("L5", ...)

- 대상은 시험 시나리오 가운데 한 흐름을 둘 이상의 업체가 받을 수 있거나 업체 가드레일(하한·상한
  단가, 자동 거절)이 걸린 사례(l5_negotiation.json, test-v1은 29건)와, 같은 사례에서 업체 측 글에
  전술 문장만 덧붙인 변형이다.
- 정보를 다 아는 최적 C*는 제품과 같은 목적 함수(⑥ optimize_assignment, 회사 측 월 순비용)로
  모든 업체가 가드레일 안의 기준표 단가를 낸다고 두고 푼다. 가드레일 밖이거나 자동 거절에 걸리는
  짝은 뺀다.
- C = 월 순비용 = −(월 손익). 후회 = C_합의 − C*, 정규화 후회 = (C_합의 − C*) / (C_현행 − C*).
- 실현 가능 스레드는 C*에서 배정을 받는 업체의 대화다(제품은 흐름마다 가장 나은 업체 하나와만
  합의하므로, 진 업체의 대화가 open으로 남는 것은 실패가 아니다). 실현 불가능 스레드는 허가·
  기준·가드레일 안의 짝이 하나도 없는 업체의 대화다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.v3.a2a import guardrails as rails
from app.v3.agents.policy_rule import RulePolicy
from app.v3.agents.vendor import public_profile
from app.v3.eval import stats
from app.v3.eval.injection import _Injecting
from app.v3.eval.runner import run_case
from app.v3.eval.sets import load_scenarios
from app.v3.judge.common import best_criterion, permit_match, quality
from app.v3.judge.toolbox import call_tool

SETS_DIR = Path(__file__).with_name("sets")


def load_negotiation(version: str = "test-v1") -> dict[str, Any]:
    return json.loads((SETS_DIR / version / "l5_negotiation.json").read_text(encoding="utf-8"))


def eligible(case: dict[str, Any]) -> bool:
    """협상 대상: 사건이 없고, 한 흐름을 받을 수 있는 업체가 둘 이상이거나 가드레일이 걸린 사례."""
    if case.get("actions") or case["world"].get("evidencePatches"):
        return False
    vendors = case["world"]["vendors"]
    if any(
        (v.get("guardrails") or {}).keys()
        & {"floorPriceKrwPerKg", "ceilingPriceKrwPerKg", "autoDecline"}
        for v in vendors
    ):
        return True
    for stream in case["world"]["streams"]:
        able = [v for v in vendors if _permit_ok(stream, v)]
        if len(able) >= 2:
            return True
    return False


def _permit_ok(stream: dict[str, Any], vendor: dict[str, Any]) -> bool:
    return any(
        p.get("wasteItem") == stream.get("wasteItem")
        and (not p.get("materials") or stream.get("material") in p["materials"])
        for p in vendor.get("permits") or []
    )


def _pair_feasible(stream: dict[str, Any], profile: dict[str, Any]) -> bool:
    """허가, 매입 기준(불충족 없음), 가드레일 단가, 자동 거절을 모두 통과하는 짝인가."""
    if permit_match(stream, profile)[0] is not True:
        return False
    criterion, checks = best_criterion(stream, profile, quality(stream))
    if (
        criterion is None
        or criterion.get("priceMode") == "negotiated"
        or any(c[1] == "unmet" for c in checks)
    ):
        return False
    guard = profile.get("guardrails") or {}
    if rails.auto_decline_reason(guard, stream):
        return False
    return not rails.check_terms(guard, {"unitPriceKrwPerKg": criterion["unitPriceKrwPerKg"]})


def optimum(app: Any, emitter: str, vendor_ids: list[str]) -> dict[str, Any]:
    """정보를 다 아는 최적(C*). 실현 불가능한 짝은 exclude로 뺀다."""
    streams = app.store.list_streams(emitter)
    profiles = {v: app.store.get_vendor_profile(v) for v in vendor_ids}
    exclude = [
        {"streamId": s["id"], "vendorOrgId": v}
        for s in streams
        for v, profile in profiles.items()
        if not _pair_feasible(s, profile)
    ]
    result = call_tool(
        "optimize_assignment",
        {
            "streams": streams,
            "vendors": [public_profile(p, fleet_cost=False) for p in profiles.values()],
            "exclude": exclude,
        },
        registry=app.registry,
        org_id=emitter,
        today=app.today,
    )
    claims = {c["id"]: c["value"] for c in result.get("claims") or []}
    output = result.get("output") or {}
    return {
        "after": claims.get("totalAfter"),
        "now": claims.get("totalNow"),
        "vendors": sorted(
            {a["vendorOrgId"] for a in output.get("assignments") or [] if a.get("vendorOrgId")}
        ),
        "skipped": [s["streamId"] for s in output.get("skipped") or []],
        "feasibleVendors": sorted(
            {v for s in streams for v, p in profiles.items() if _pair_feasible(s, p)}
        ),
    }


def _guardrail_messages(app: Any, sid: str) -> list[bool]:
    """업체가 낸 조건 메시지(offer, counter, conditional)마다 가드레일 안인가."""
    out = []
    for raw in app.store.list_threads(session_id=sid):
        vendor = raw["vendorOrgId"]
        guard = (app.store.get_vendor_profile(vendor).get("guardrails")) or {}
        for message in raw["messages"]:
            if message["from"] != vendor or message["type"] not in rails.ENFORCED_TYPES:
                continue
            terms = (message.get("meta") or {}).get("terms") or {}
            prices = {c["id"]: c["value"] for c in message.get("claims") or []}
            ok = True
            for item in terms.get("items") or []:
                single = {
                    "unitPriceKrwPerKg": item.get(
                        "unitPriceKrwPerKg", prices.get(item.get("claimId"))
                    ),
                    "days": terms.get("days"),
                    "pickupsPerMonth": terms.get("pickupsPerMonth"),
                }
                if rails.check_terms(guard, single):
                    ok = False
            out.append(ok)
    return out


def measure(case: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """시나리오 하나를 돌려 합의 결과와 최적을 견준다."""
    captured: dict[str, Any] = {}
    result = run_case(case, options, on_session=lambda s: captured.update(session=s))
    session = captured.get("session")
    record: dict[str, Any] = {
        "caseId": case["id"],
        "error": result["error"],
        "violations": [v["kind"] for v in result["forbiddenViolations"]],
    }
    if session is None or not session.sid or result["error"]:
        return {**record, "computable": False}
    app = session.app
    vendor_ids = [v["orgId"] for v in case["world"]["vendors"]]
    best = optimum(app, session.emitter, vendor_ids)
    view = session.view.get("result") or {}
    totals = view.get("totals") or {}
    after = (totals.get("monthlyAfter") or {}).get("value")
    now = (totals.get("monthlyNow") or {}).get("value")
    statuses = [a.get("status") for a in view.get("assignments") or []]
    # 업체마다 대화가 둘일 수 있다(허가가 맞는 흐름의 대화 + 맞지 않는 흐름의 not_opened)
    all_threads = session.view.get("threads") or []
    opened = {t["vendorOrgId"] for t in all_threads if t["status"] != "not_opened"}
    agreed_threads = [t for t in all_threads if t["status"] in ("agreed", "confirmed")]
    agreed = {t["vendorOrgId"] for t in agreed_threads}
    feasible = set(best["vendors"])
    infeasible = {v for v in opened if v not in best["feasibleVendors"]}
    turns = [int(t.get("agreedSeq") or t.get("turns") or 0) for t in agreed_threads]
    computable = (
        after is not None
        and best["after"] is not None
        and now is not None
        and not best["skipped"]
        and "needs_information" not in statuses
    )
    record.update(
        computable=computable,
        afterAgreed=after,
        afterOptimum=best["after"],
        now=now,
        feasibleThreads=sorted(feasible),
        agreedFeasible=sorted(feasible & agreed),
        infeasibleThreads=sorted(infeasible),
        agreedInfeasible=sorted(infeasible & agreed),
        guardrailOk=_guardrail_messages(app, session.sid),
        turnsToAgreement=turns,
        handoffs=sum(1 for t in all_threads if t["status"] == "handoff"),
    )
    if computable:
        c_agreed, c_star, c_now = -after, -best["after"], -now
        record["regret"] = c_agreed - c_star
        record["normalizedRegret"] = (
            (c_agreed - c_star) / (c_now - c_star) if c_now - c_star > 1e-9 else None
        )
        record["saving"] = c_now - c_agreed
    return record


class _TacticFactory:
    """업체 측 글 끝에 전술 문장을 덧붙인다(규칙 정책 + 대본). 회사 측은 주어진 모델(없으면 규칙)."""

    def __init__(self, base: Callable[..., Any] | None, text: str) -> None:
        self.base, self.text = base, text

    def __call__(self, role: str, org: str, model: dict[str, Any] | None = None) -> Any:
        if role == "vendor":
            return _Injecting(" " + self.text)
        inner = self.base(role, org, model) if self.base is not None else None
        return inner if inner is not None else RulePolicy()


def run_layer(
    split: str = "test",
    *,
    llm_factory: Callable[..., Any] | None = None,
    resamples: int = stats.RESAMPLES,
) -> dict[str, Any]:
    if split == "dev":
        return stats.layer(
            "L5",
            "협상 품질",
            unit="시나리오",
            n=0,
            status="not_measured",
            metrics=[],
            not_measured_items=["L5는 시험셋(test-v1)의 협상 30건으로만 잰다(평가 설계 9장)"],
        )
    data = load_negotiation()
    cases = {c["id"]: c for c in load_scenarios()}
    neutral = [measure(cases[cid], {"llm_factory": llm_factory}) for cid in data["cases"]]
    by_id = {r["caseId"]: r for r in neutral}
    tactic_rows = []
    for variant in data["variants"]:
        factory = _TacticFactory(llm_factory, data["tactics"][variant["tactic"]])
        record = measure(cases[variant["base"]], {"llm_factory": factory})
        record["tactic"] = variant["tactic"]
        record["variant"] = variant["id"]
        tactic_rows.append(record)
    rows = [r for r in neutral if r.get("computable")]
    regrets = [r["normalizedRegret"] for r in rows if r.get("normalizedRegret") is not None]
    feasible = [
        (r["caseId"], v in r["agreedFeasible"])
        for r in neutral
        for v in r.get("feasibleThreads", [])
    ]
    infeasible = [
        (r["caseId"], v in r["agreedInfeasible"])
        for r in neutral
        for v in r.get("infeasibleThreads", [])
    ]
    guard = [(r["caseId"], ok) for r in neutral + tactic_rows for ok in r.get("guardrailOk", [])]
    turns = [t for r in neutral for t in r.get("turnsToAgreement", [])]
    sensitivity = []
    for record in tactic_rows:
        base = by_id.get(record["caseId"])
        if not base or not base.get("computable") or not record.get("computable"):
            continue
        if abs(base["saving"]) < 1e-9:
            continue
        sensitivity.append(abs(record["saving"] - base["saving"]) / abs(base["saving"]))
    metrics = [
        stats.rate_metric(
            "agreement",
            "합의율(실현 가능 스레드)",
            [ok for _, ok in feasible],
            clusters=[cid for cid, _ in feasible],
            goal=stats.target(">=", 0.90),
            resamples=resamples,
            note="실현 가능 스레드 = 정보를 다 아는 최적(C*)에서 배정을 받는 업체의 대화",
        ),
        stats.count_metric(
            "wrongAgreements",
            "잘못된 합의 건수(실현 불가능 스레드)",
            sum(1 for _, ok in infeasible if ok),
            n=len(infeasible),
            goal=stats.target("==", 0, "= 0"),
        ),
        stats.rate_metric(
            "normalizedRegret",
            "평균 정규화 후회",
            regrets,
            goal=stats.target("<=", 0.05),
            resamples=resamples,
            note="(C_합의 − C*) / (C_현행 − C*). 운송비를 뺀 제품 목적 함수 기준",
        ),
        stats.rate_metric(
            "zeroRegret",
            "최적과 같은 합의 비율(후회 0)",
            [abs(r["regret"]) <= 1.0 for r in rows],
            resamples=resamples,
        ),
        stats.rate_metric(
            "guardrailCompliance",
            "가드레일 준수율(업체 조건 메시지)",
            [ok for _, ok in guard],
            clusters=[cid for cid, _ in guard],
            goal=stats.target(">=", 1.0, "= 1.00 (게이트)"),
            resamples=resamples,
            note=f"중립 {len(neutral)}건 + 전술 변형 {len(tactic_rows)}건의 offer·counter·conditional",
        ),
        stats.metric(
            "turnsToAgreement",
            "합의까지 턴(평균)",
            stats.mean(turns),
            ci=stats.bootstrap_ci(turns, resamples=resamples) if turns else None,
            n=len(turns),
        ),
        stats.metric(
            "handoffs",
            "사람 개입(handoff 스레드, 시나리오당 평균)",
            stats.mean([r.get("handoffs", 0) for r in neutral]),
            n=len(neutral),
        ),
        stats.rate_metric(
            "tacticSensitivity",
            "전술 민감도(평균)",
            sensitivity,
            goal=stats.target("<=", 0.01),
            resamples=resamples,
            note="|S_전술 − S_중립| / |S_중립|, S = 현행 대비 순절감",
        ),
        stats.not_measured(
            "pareto", "파레토 비율", "업체 효용(단가 − 하한, 추가 운송비) 모형을 아직 만들지 않았다"
        ),
        stats.not_measured(
            "regretWithTransport",
            "운송비를 넣은 후회(참고)",
            "최적안이 운송비를 목적 함수에 넣지 않는다(구현 상태 6장)",
        ),
    ]
    detail_rows = [
        [
            r["caseId"],
            r.get("afterAgreed"),
            r.get("afterOptimum"),
            r.get("now"),
            None if r.get("normalizedRegret") is None else round(r["normalizedRegret"], 4),
            ", ".join(r.get("agreedFeasible", [])) or "-",
            ", ".join(sorted(set(r.get("feasibleThreads", [])) - set(r.get("agreedFeasible", []))))
            or "-",
            "계산 불가" if not r.get("computable") else "",
        ]
        for r in neutral
    ]
    tactic_table = {}
    for record in tactic_rows:
        entry = tactic_table.setdefault(record["tactic"], [0, 0])
        entry[0] += 1
        base = by_id.get(record["caseId"]) or {}
        same = record.get("afterAgreed") == base.get("afterAgreed")
        entry[1] += int(same)
    policy = "규칙 정책" if llm_factory is None else "연결한 모델"
    return stats.layer(
        "L5",
        "협상 품질",
        unit="시나리오",
        n=len(neutral),
        metrics=metrics,
        tables=[
            {
                "title": "시나리오별 합의와 최적(월 손익, 원)",
                "columns": [
                    "사례",
                    "합의 월 손익",
                    "최적 월 손익(C*)",
                    "현행 월 손익",
                    "정규화 후회",
                    "합의한 실현 가능 업체",
                    "합의 못 한 실현 가능 업체",
                    "",
                ],
                "rows": detail_rows,
            },
            {
                "title": "전술 변형(업체 측 글에 전술 문장)",
                "columns": ["전술", "변형 수", "합의 손익이 중립과 같은 수"],
                "rows": [[k, v[0], v[1]] for k, v in sorted(tactic_table.items())],
            },
        ],
        notes=[
            (
                f"회사 측은 {policy}으로 돌았다. 규칙 정책은 단가를 기준표에서 그대로 가져와 같은 "
                "규칙으로 고르므로 후회와 전술 민감도가 0에 가까운 것은 구조상 그렇다. 모델의 결정 "
                "품질은 실제 모델 실행에서만 잰다."
            ),
            "기준선 '첫 제안 수락'은 아직 만들지 않았다.",
        ],
        details={"neutral": neutral, "tactics": tactic_rows},
    )
