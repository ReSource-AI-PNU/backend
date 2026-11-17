"""시험셋 test-v1의 동결, 형식, 규칙 정책 회귀(평가 설계 12장, 14장, 16장). 수치는 모두 가상이다.

규칙 정책은 모델이 아니다. 이 시험은 끝 상태 일치, 금지 위반, 변형 관계가 코드 수준에서 지켜지는지
보는 회귀 시험이고 AI 품질이 아니다.
"""

import re
from pathlib import Path

import pytest

from app.v3.eval import metamorphic, sets, trials
from app.v3.eval.loader import check_cases, load_cases
from scripts.v3.eval import build_test_v1

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = sets.load_scenarios()
VARIANTS = sets.load_variants()
# 규칙 정책에서 관계를 지키지 못하는 변형(실제 발견). 담당자 답을 "2 퍼센트", "2프로 정도",
# "650 킬로", "대략 650kg"처럼 적으면 답 형식 검사(agents/questions.parse_answer)가 숫자로 읽지
# 못해 실행이 멈춘다. 고치면 이 목록을 비우고 리포트를 다시 만든다.
KNOWN_PARAPHRASE_FAILURES = {"V013", "V017", "V023", "V025", "V043", "V049", "V061", "V067"}


def test_test_v1_is_frozen():
    assert sets.verify("test-v1") == []
    manifest = sets.read_manifest("test-v1")
    counts = {k: v["count"] for k, v in manifest["files"].items()}
    assert counts["scenarios.json"] == {"cases": 70}
    assert counts["variants.json"] == {"variants": 80}
    assert counts["l2_queries.json"] == {"queries": 80}
    assert counts["l6_attacks.json"] == {"attacks": 60}
    assert counts["l6_probes.json"] == {"probes": 30}
    assert counts["l6_pii.json"] == {"items": 20}
    assert "../corpus/laws_extra.json" in manifest["files"]


def test_the_generator_refuses_to_overwrite_a_frozen_set():
    with pytest.raises(SystemExit):
        build_test_v1.write(sets.set_dir("test-v1"), force=False)


def test_scenarios_have_the_case_format_and_meet_the_axis_minimums():
    assert check_cases(SCENARIOS) == []
    ids = [c["id"] for c in SCENARIOS]
    assert len(ids) == 70 == len(set(ids))
    assert not set(ids) & {c["id"] for c in load_cases()}
    meta = sets.scenario_meta()
    coverage, minimums = meta["coverage"], meta["minimums"]
    assert {k: coverage.get(k, 0) for k in minimums if coverage.get(k, 0) < minimums[k]} == {}
    assert all(c["review"] == "초안" and c["synthetic"] is True for c in SCENARIOS)


def test_variants_point_at_test_scenarios_two_each():
    bases = [v["base"] for v in VARIANTS]
    assert len(VARIANTS) == 80 and len(set(bases)) == 40
    assert all(bases.count(b) == 2 for b in set(bases))
    assert set(bases) <= {c["id"] for c in SCENARIOS}
    assert {v["kind"] for v in VARIANTS} == {"INV", "DIR"}


def test_prompts_do_not_contain_test_ids_or_test_phrases():
    """새는 것 막기(14장): 시험 사례를 프롬프트 예시로 쓰지 않는다."""
    phrases = ["CANARY-", "가상새벽자원", "가상노선자원", "attacker.invalid"]
    for path in sorted((ROOT / "app" / "v3" / "agents").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b(T0\d\d|V0\d\d|A-G\d-\d\d|P-[EV]\d\d|Q-T\d+)\b", text), path.name
        assert not [p for p in phrases if p in text], path.name


@pytest.fixture(scope="module")
def outcome():
    return trials.run_trials(SCENARIOS)


def test_rule_policy_reaches_the_expected_end_state_on_test_v1(outcome):
    failed = {
        t["caseId"]: (t["mismatches"], [v["kind"] for v in t["violationItems"]], t["error"])
        for t in outcome["trials"]
        if not t["success"] and not t["open"]
    }
    assert failed == {}
    opened = sorted(t["caseId"] for t in outcome["trials"] if t["open"])
    assert opened == ["T042"]  # 설계 판단 필요(scenarios.json의 triage)
    checked = sum(t["outgoing"]["checked"] for t in outcome["trials"])
    assert checked > 300
    assert all(not t["outgoing"]["failed"] for t in outcome["trials"])


def test_variants_keep_their_relations_under_the_rule_policy(outcome):
    base = {t["caseId"]: t for t in outcome["trials"]}
    results = metamorphic.run_variants(VARIANTS, base)
    broken = {r["id"] for r in results if not r["consistent"]}
    assert broken == KNOWN_PARAPHRASE_FAILURES
    assert all(
        r["relation"] == "paraphrase" and "숫자로 답해" in (r["error"] or "")
        for r in results
        if r["id"] in broken
    )


def test_relation_checker_catches_a_changed_assignment():
    variant = {"relation": "rename", "params": {"mapping": {"st_a": "st_r1", "org_k1": "org_r1"}}}
    base = {"assignments": {"st_a": {"vendor": "org_k1", "route": "sell", "status": "calculated"}}}
    same = {"assignments": {"st_r1": {"vendor": "org_r1", "route": "sell", "status": "calculated"}}}
    moved = {"assignments": {"st_r1": {"vendor": None, "route": "keep", "status": "calculated"}}}
    assert metamorphic.check_relation(variant, base, same)["ok"] is True
    assert metamorphic.check_relation(variant, base, moved)["ok"] is False
    scale = {"relation": "scale_prices", "params": {"factor": 2}}
    priced = {"assignments": {"st_a": {"vendor": "v", "route": "sell", "unitPrice": 200}}}
    doubled = {"assignments": {"st_a": {"vendor": "v", "route": "sell", "unitPrice": 400}}}
    assert metamorphic.check_relation(scale, priced, doubled)["ok"] is True
    assert metamorphic.check_relation(scale, priced, priced)["ok"] is False
