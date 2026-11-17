"""L6 조직 간 노출(카나리아·탐침)과 L5 협상(평가 설계 9장, 10.2). 규칙 정책으로 매 푸시 돈다.

규칙 정책은 모델이 아니다. 여기서 0은 코드의 조직 필터와 정해진 문장 틀을 잰 것이다.
"""

import pytest

from app.v3.eval import leakage, negotiation, sets

PROBES = leakage.load_probes()
# 실제 발견(제품 코드는 그대로 두고 리포트에 적는다): 배출 기업이 부른 판단 도구의 계산 근거
# (calc:check_loading·cost_carbon·judge_route·match_permit)가 대화 상대 업체에게 열리는데, 근거의
# meta.inputs에 흐름 레코드가 통째로 실려 배출 기업의 현행 처리 방법과 단가(currentTreatment)가
# 업체의 근거 조회·검색으로 보인다. 계약 5장 "현행 처리 단가에서 나온 계산 근거는 계산한 조직만
# 본다", 공통 규칙 "현행 처리 단가는 업체에 보이지 않는다"와 어긋난다.
KNOWN_EVIDENCE_LEAK = {
    ("org_lk1", "CANARY-가-7f3a"),
    ("org_lk1", "CANARY-나-p81d"),
    ("org_lk2", "CANARY-가-7f3a"),
    ("org_lk2", "CANARY-나-p81d"),
}


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


def test_probes_add_no_exposure_beyond_the_known_evidence_leak(leak_results):
    base = leak_results[0]
    for result in leak_results:
        assert result["count"] == base["count"], result["id"]
        found = {
            (e["viewer"], needle)
            for e in result["exposures"]
            if e["where"].startswith("근거")
            for needle in e["found"]
        }
        assert found == KNOWN_EVIDENCE_LEAK, result["id"]


def test_leakage_layer_reports_the_gate_as_failed_while_the_leak_exists():
    layer = leakage.run_layer(resamples=200)
    rows = {m["key"]: m for m in layer["metrics"]}
    assert rows["exposures"]["verdict"] == "미달"
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
