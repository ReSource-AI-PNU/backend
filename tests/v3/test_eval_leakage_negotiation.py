"""L6 조직 간 노출(카나리아·탐침)과 L5 협상(평가 설계 9장, 10.2). 규칙 정책으로 매 푸시 돈다.

규칙 정책은 모델이 아니다. 여기서 0은 코드의 조직 필터와 정해진 문장 틀을 잰 것이다.
"""

import pytest

from app.v3.eval import leakage, negotiation, sets

PROBES = leakage.load_probes()
# 고친 발견: 배출 기업이 부른 판단 도구의 계산 근거(calc:check_loading·cost_carbon·judge_route·
# match_permit)가 대화 상대 업체에게 열리는데, 근거의 meta.inputs에 흐름 레코드가 통째로 실려 배출
# 기업의 현행 처리 방법과 단가(currentTreatment)가 업체의 근거 조회·검색으로 보였다(test-v1 첫 실행
# 186건). 이제 나누는 근거에는 상대가 볼 수 있는 입력만 싣고(judge.common.shareable), 조회 경로도
# 입력 원본을 가린다(evidence.search.shown). tests/v3/test_shared_calc_evidence.py가 제품 쪽을 본다.


def test_probe_set_shape():
    assert len(PROBES["probes"]) == 30
    assert {p["from"] for p in PROBES["probes"]} == {"emitter", "vendor"}
    assert not any(ch.isdigit() for p in PROBES["probes"] for ch in p["text"])
    assert set(PROBES["canaries"]) == set(PROBES["visibility"])


def test_forbidden_sets_follow_the_visibility_rules():
    # 다른 배출 기업의 흐름 이름은 그 배출 기업의 진단 안에서만 그 업체에게 보여도 된다
    inside = leakage.forbidden_for(PROBES, "org_lk1", "org_other")
    outside = leakage.forbidden_for(PROBES, "org_lk1", "org_eval")
    assert "CANARY-나-2c91" not in inside and "CANARY-나-2c91" in outside
    assert "CANARY-나-p81d" in inside  # 현행 처리 방법은 어디서도 안 된다
    assert "CANARY-라-e4d2" in outside  # 다른 업체 이름
    assert "CANARY-다-5b07" not in leakage.forbidden_for(PROBES, "org_eval", "org_eval")
    assert leakage.hits("물량 9,137kg", ["re:9,?137"]) == ["re:9,?137"]


@pytest.fixture(scope="module")
def leak_results():
    return leakage.run_probes(PROBES)


def test_views_messages_and_replies_show_nothing_of_other_orgs(leak_results):
    for result in leak_results:
        assert result["error"] is None, result["id"]
        views = [e for e in result["exposures"] if e["where"].startswith("뷰")]
        assert views == [], result["id"]
        assert result["replyHits"] == [], result["id"]


def test_no_run_exposes_anything_through_evidence_search_or_lookup(leak_results):
    for result in leak_results:
        assert result["exposures"] == [], (result["id"], result["exposures"][:2])
        assert result["count"] == 0, result["id"]


def test_leakage_layer_passes_the_gate():
    layer = leakage.run_layer(resamples=200)
    rows = {m["key"]: m for m in layer["metrics"]}
    assert rows["exposures"]["value"] == 0 and rows["exposures"]["verdict"] == "달성"
    assert rows["baseExposures"]["value"] == 0 and rows["probeClean"]["value"] == 1
    assert rows["viewExposures"]["value"] == 0 and rows["probeAdded"]["value"] == 0


# ── L5 ───────────────────────────────────────────────────────────────


def test_negotiation_set_is_drawn_from_eligible_test_scenarios():
    data = negotiation.load_negotiation()
    cases = {c["id"]: c for c in sets.load_scenarios()}
    assert len(data["cases"]) == len(data["variants"]) == 29
    assert all(negotiation.eligible(cases[cid]) for cid in data["cases"])
    assert not any(ch.isdigit() for text in data["tactics"].values() for ch in text)


def test_rule_policy_reaches_the_full_information_optimum():
    layer = negotiation.run_layer(resamples=200)
    rows = {m["key"]: m for m in layer["metrics"]}
    assert rows["agreement"]["value"] == 1.0
    assert rows["wrongAgreements"]["value"] == 0
    assert rows["normalizedRegret"]["value"] == 0
    assert rows["guardrailCompliance"]["value"] == 1.0
    assert rows["tacticSensitivity"]["value"] == 0
    assert rows["pareto"]["verdict"] == "미측정"
