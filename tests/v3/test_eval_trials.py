"""반복 실행기와 평가 통계(평가 설계 4.3, 7장, 8장). 수치는 모두 가상 사례에서 나온다."""

import math

from app.v3.agents.llm import ScriptedLLM
from app.v3.eval import stats, trials
from app.v3.eval.loader import load_cases

CASES = {c["id"]: c for c in load_cases()}


# ── 통계 ─────────────────────────────────────────────────────────────


def test_pass_hat_k_matches_the_tau_bench_estimator():
    # 과업 셋, n=4: 4번 다 성공, 2번 성공, 0번 성공
    counts, n = [4, 2, 0], 4
    assert stats.pass_hat_k(counts, n, 1) == (1 + 0.5 + 0) / 3
    assert math.isclose(stats.pass_hat_k(counts, n, 2), (1 + 1 / 6 + 0) / 3)
    assert stats.pass_hat_k(counts, n, 4) == 1 / 3
    assert stats.pass_hat_k(counts, n, 5) is None
    assert stats.pass_hat_k([], n, 1) is None


def test_bootstrap_is_seeded_and_brackets_the_mean():
    values = [1.0] * 14 + [0.0] * 6
    first = stats.bootstrap_ci(values, resamples=2000)
    assert first == stats.bootstrap_ci(values, resamples=2000)
    assert first[0] < 0.7 < first[1]
    assert stats.bootstrap_ci([]) is None
    assert stats.bootstrap_ci([1.0, 1.0, 1.0]) == (1.0, 1.0)


def test_cluster_bootstrap_resamples_whole_groups():
    # 묶음 안의 값이 같으면 묶음 단위 구간이 값 단위 구간보다 넓다
    values = [1.0] * 10 + [0.0] * 10
    clusters = ["a"] * 5 + ["b"] * 5 + ["c"] * 5 + ["d"] * 5
    wide = stats.bootstrap_ci(values, clusters=clusters, resamples=2000)
    narrow = stats.bootstrap_ci(values, resamples=2000)
    assert wide[1] - wide[0] > narrow[1] - narrow[0]


def test_paired_bootstrap_says_when_no_difference_is_shown():
    same = stats.paired_bootstrap([1, 0, 1, 0], [1, 0, 1, 0], resamples=500)
    assert same["delta"] == 0 and same["verdict"] == "차이 확인 못 함"
    better = stats.paired_bootstrap([1] * 20, [0] * 20, resamples=500)
    assert better["verdict"] == "나아짐"


def test_targets_are_marked_as_targets_and_judged():
    goal = stats.target(">=", 0.8)
    assert goal["isTarget"] is True
    assert stats.verdict(0.81, goal) == "달성" and stats.verdict(0.7, goal) == "미달"
    assert stats.verdict(None, goal) == "미측정"
    row = stats.rate_metric("x", "x", [True] * 5, goal=goal, resamples=100)
    assert row["smallSample"] is True and row["n"] == 5


# ── 초안 단위 기록 ───────────────────────────────────────────────────


def _event(seq, actor, kind, thread, **detail):
    return {"seq": seq, "actor": actor, "kind": kind, "threadId": thread, "detail": detail}


def test_drafts_are_grouped_into_slots_and_rechecks_are_left_out():
    events = [
        _event(1, "validator", "validation", "th_1", ok=True, attempt=1, orgId="org_e"),
        _event(
            2,
            "validator",
            "validation",
            "th_1",
            ok=False,
            attempt=1,
            rejections=[{"code": "NUMBER_OUTSIDE_PLACEHOLDER"}],
        ),
        _event(3, "validator", "validation", "th_1", ok=True, attempt=2, orgId="org_v"),
        _event(4, "validator", "validation", "th_1", ok=True, recheckedBy="org_e", codes=[]),
        _event(
            5,
            "validator",
            "validation",
            "th_1",
            ok=False,
            attempt=1,
            rejections=[{"code": "TYPE_NOT_ALLOWED"}],
        ),
        _event(
            6,
            "validator",
            "validation",
            "th_1",
            ok=False,
            attempt=2,
            rejections=[{"code": "SCHEMA"}],
        ),
        _event(7, "vendor_agent", "handoff", "th_1"),
        _event(8, "company_agent", "validation", "th_2", ok=True, attempt=1, type="accept"),
    ]
    slots = trials.drafts_from_events(events, "org_e")
    assert [s["outcome"] for s in slots] == ["passed", "passed", "handoff", "decision"]
    assert [s["side"] for s in slots] == ["company", "vendor", "vendor", "company"]
    assert slots[1]["attempts"][0]["codes"] == ["NUMBER_OUTSIDE_PLACEHOLDER"]
    assert trials.rechecks_from_events(events) == {"checked": 1, "failed": 0, "codes": []}


# ── 실행 ─────────────────────────────────────────────────────────────


def test_rule_policy_repeats_give_identical_trials_and_pass_k():
    picked = [CASES["C04"], CASES["C28"], CASES["C47"]]
    outcome = trials.run_trials(picked, repeats=2)
    assert [c["successes"] for c in outcome["cases"]] == [2, 2, 2]
    rows = {m["key"]: m for m in trials.l4_metrics(outcome, resamples=200)}
    assert rows["pass^1"]["value"] == 1.0 and rows["pass^2"]["value"] == 1.0
    assert rows["pass^3"]["value"] is None and rows["pass^3"]["verdict"] == "미측정"
    assert rows["violations"]["value"] == 0
    first, second = (t for t in outcome["trials"] if t["caseId"] == "C47")
    assert first["slots"] == second["slots"]
    # C47: 기준표 단가가 하한 밖이라 업체 초안이 두 번 거부되고 담당자에게 넘어간다(기대한 동작)
    assert first["slots"][-1]["outcome"] == "handoff"
    assert first["firstRejection"] == "GUARDRAIL_PRICE_BELOW_FLOOR"
    assert outcome["meter"]["calls"] == 0


def test_a_model_that_never_writes_json_is_a_format_failure_with_tokens_counted():
    def broken(messages, tools, schema):
        return {
            "content": "죄송합니다. 숫자 300을 씁니다.",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }

    def factory(role, org_id, model=None):
        return ScriptedLLM([broken] * 20) if role == "company" else None

    outcome = trials.run_trials([CASES["C04"]], repeats=1, llm_factory=factory)
    trial = outcome["trials"][0]
    assert trial["success"] is False
    assert trial["failureType"] == "형식 실패"
    assert trial["firstRejection"] == "INVALID_OUTPUT"
    assert trial["calls"] >= 2 and trial["promptTokens"] == 11 * trial["calls"]
    assert trial["completionTokens"] == 7 * trial["calls"]
    rows = {m["key"]: m for m in trials.l3_layer(outcome, resamples=100)["metrics"]}
    assert rows["firstTry"]["value"] < 1.0 and rows["handoff"]["value"] > 0
    # 나간 글은 여전히 모두 검증을 통과한다(handoff 문장은 코드가 쓴다)
    assert rows["outgoingHallucination"]["value"] == 0


def test_budget_stops_new_trials_and_reports_the_rest_as_not_measured():
    def factory(role, org_id, model=None):
        return ScriptedLLM(["{}"] * 50) if role == "company" else None

    outcome = trials.run_trials(
        [CASES["C04"], CASES["C11"]], repeats=2, llm_factory=factory, budget_calls=1
    )
    assert len(outcome["trials"]) == 1
    assert [s["reason"] for s in outcome["skipped"]] == [trials.BUDGET_SKIPPED] * 3
    layer = trials.l4_layer(outcome, resamples=100)
    assert layer["status"] == "partial" and len(layer["notMeasured"]) == 3
    assert layer["n"] == 0  # 반복을 다 채운 사례가 없으면 성공률을 내지 않는다


def test_a_broken_case_is_an_execution_error():
    case = {**CASES["C04"], "world": {**CASES["C04"]["world"], "vendors": [{"orgId": "org_x"}]}}
    case["world"]["vendors"][0]["criteria"] = [{"id": "c", "material": "PP"}]
    outcome = trials.run_trials([case])
    assert outcome["trials"][0]["failureType"] == "실행 오류"


def test_parallel_workers_give_the_same_trials_in_the_same_order():
    picked = [CASES["C04"], CASES["C28"], CASES["C47"]]
    one = trials.run_trials(picked, repeats=2)
    many = trials.run_trials(picked, repeats=2, workers=3)

    def keep(outcome):
        return [
            (t["caseId"], t["repeat"], t["success"], t["slots"], t["mismatches"])
            for t in outcome["trials"]
        ]

    assert keep(one) == keep(many)
    assert [c["successes"] for c in many["cases"]] == [c["successes"] for c in one["cases"]]


def test_each_parallel_trial_counts_only_its_own_calls():
    def answer(messages, tools, schema):
        return {"content": "{}", "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

    def factory(role, org_id, model=None):
        return ScriptedLLM([answer] * 30) if role == "company" else None

    outcome = trials.run_trials(
        [CASES["C04"], CASES["C11"]], repeats=2, llm_factory=factory, workers=4
    )
    assert sum(t["calls"] for t in outcome["trials"]) == outcome["meter"]["calls"]
    assert all(t["promptTokens"] == 3 * t["calls"] for t in outcome["trials"])


def test_rate_limited_calls_wait_and_retry_without_counting_as_a_new_call():
    from app.v3.agents.llm import LLMError

    class Throttled:
        provider, model = "fake", "fake"

        def __init__(self):
            self.left = 2

        def complete(self, messages, *, tools=None, response_schema=None):
            if self.left:
                self.left -= 1
                raise LLMError("429", status=429)
            return {"content": "{}", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    waited = []
    meter = trials.Meter(lambda role, org, model=None: Throttled(), sleep=waited.append)
    llm = meter.factory("company", "org_a")
    llm.complete([{"role": "user", "content": "x"}])
    assert meter.calls == 1 and meter.throttled == 2 and waited == list(trials.THROTTLE_WAITS[:2])
    assert meter.errors == []

    class Refused(Throttled):
        def complete(self, messages, *, tools=None, response_schema=None):
            raise LLMError("400", status=400)

    meter = trials.Meter(lambda role, org, model=None: Refused(), sleep=waited.append)
    try:
        meter.factory("company", "org_a").complete([{"role": "user", "content": "x"}])
    except LLMError:
        pass
    assert meter.throttled == 0 and len(meter.errors) == 1
