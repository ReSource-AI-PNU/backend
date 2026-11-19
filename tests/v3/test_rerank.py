"""근거 검색의 모델 재순위(evidence/rerank.py). 모델은 가짜 chat이다."""

import json

import pytest

from app.v3.eval import retrieval
from app.v3.evidence import rerank, search
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry


def evidence(evidence_id, label, excerpt, access=("*",)):
    return Evidence(
        id=evidence_id,
        kind=evidence_id.split(":", 1)[0],
        label=label,
        excerpt=excerpt,
        source="가상 출처",
        as_of="2026-11-10",
        access=access,
    )


class FakeChat:
    """후보 목록에서 pick 함수가 고른 번호를 돌려준다."""

    def __init__(self, pick=None, error=None):
        self.pick = pick or (lambda lines: [])
        self.error = error
        self.calls = []

    def complete(self, messages, response_schema=None):
        self.calls.append(messages)
        if self.error:
            raise self.error
        lines = [ln for ln in messages[-1]["content"].splitlines() if ln.startswith("[")]
        return {"content": json.dumps({"ranked": self.pick(lines)})}


def pick_label(word):
    """이름에 word가 든 후보를 앞으로."""
    return lambda lines: [ln[1 : ln.index("]")] for ln in lines if word in ln]


@pytest.fixture
def registry():
    made = InMemoryRegistry()
    made.put(evidence("law:가상법:제1조@2026-01-01", "가상법 제1조", "폐기물 처리 위탁 기준"))
    made.put(evidence("law:가상법:제2조@2026-01-01", "가상법 제2조", "폐기물 보관 기간"))
    made.put(evidence("ref:가격:폐기물@2026-10", "폐기물 처리 단가", "폐기물 처리 폐기물 처리"))
    made.put(evidence("doc:org_a:계약서", "계약서", "폐기물 처리", access=("org_a",)))
    return made


@pytest.fixture(autouse=True)
def no_reranker():
    search.use_reranker(None)
    yield
    search.use_reranker(None)


def ids(hits):
    return [h["evidence"]["id"] for h in hits]


def test_picked_candidates_go_first_and_the_rest_keep_their_order():
    by_id = {k: evidence(k, f"이름{k}", f"원문{k}") for k in ("law:a", "law:b", "law:c", "law:d")}
    ranked = [("law:a", 0.9), ("law:b", 0.8), ("law:c", 0.7), ("law:d", 0.6)]
    chat = FakeChat(lambda lines: ["3", "[1]", "99", "3"])  # 범위 밖·겹친 번호는 버린다
    out = rerank.LlmReranker(chat).rerank("질의", ranked, by_id)
    assert [k for k, _ in out] == ["law:c", "law:a", "law:b", "law:d"]
    assert out[0][1] == 0.9 and out[1][1] < 0.9 and all(v <= 0.45 for _, v in out[2:])


def test_failure_or_blank_query_keeps_the_original_order():
    ranked = [("a", 0.9), ("b", 0.8)]
    by_id = {"a": evidence("law:a", "a", "a"), "b": evidence("law:b", "b", "b")}
    broken = rerank.LlmReranker(FakeChat(error=TimeoutError("느림")))
    assert broken.rerank("질의", ranked, by_id) == ranked and broken.failures == 1
    assert rerank.LlmReranker(FakeChat()).rerank("  ", ranked, by_id) == ranked


def test_nothing_picked_lowers_the_top_score_and_results_are_remembered():
    ranked = [("a", 0.9), ("b", 0.8)]
    by_id = {"a": evidence("law:a", "a", "a"), "b": evidence("law:b", "b", "b")}
    chat = FakeChat()
    reranker = rerank.LlmReranker(chat)
    out = reranker.rerank("관련 없는 질의", ranked, by_id)
    assert [k for k, _ in out] == ["a", "b"] and out[0][1] == 0.45
    reranker.rerank("관련 없는 질의", ranked, by_id)
    assert len(chat.calls) == 1


def test_search_evidence_uses_the_reranker_and_never_shows_hidden_evidence(registry):
    plain = search.search_evidence(registry, "폐기물 처리", org_id="org_b", limit=5)
    assert ids(plain)[0] == "ref:가격:폐기물@2026-10"  # 어휘 순위는 짧은 기준값 문서를 올린다
    chat = FakeChat(pick_label("가상법 제1조"))
    search.use_reranker(rerank.LlmReranker(chat))
    hits = search.search_evidence(registry, "폐기물 처리", org_id="org_b", limit=5)
    assert ids(hits)[0] == "law:가상법:제1조@2026-01-01"
    assert "doc:org_a:계약서" not in ids(hits) and "계약서" not in chat.calls[0][-1]["content"]
    assert search.reranker_name() == "llm"
    search.use_reranker(None)
    assert ids(search.search_evidence(registry, "폐기물 처리", org_id="org_b", limit=5)) == ids(
        plain
    )


def test_reranker_is_built_only_for_foundry_with_every_setting():
    env = {
        "V3_LLM": "foundry",
        "FOUNDRY_ENDPOINT": "https://foundry.invalid",
        "FOUNDRY_DEPLOYMENT": "gpt-5-mini",
        "FOUNDRY_API_KEY": "key",
    }
    assert isinstance(rerank.reranker_from_env(env), rerank.LlmReranker)
    assert rerank.reranker_from_env({**env, "V3_LLM": "rule"}) is None
    assert rerank.reranker_from_env({**env, "FOUNDRY_API_KEY": ""}) is None
    assert rerank.reranker_from_env({**env, rerank.ENV_SWITCH: "0"}) is None


def test_eval_measures_the_reranked_system_only_when_asked():
    off = retrieval.run_layer("dev", resamples=50, systems=["S1", "S7"], env={})
    assert any(line.startswith("S7") for line in off["notMeasured"])
    chat = FakeChat(lambda lines: ["1"])
    on = retrieval.run_layer("dev", resamples=50, systems=["S1", "S7"], env={}, rerank_chat=chat)
    assert not any(line.startswith("S7") for line in on["notMeasured"])
    names = [row[0] for row in on["tables"][0]["rows"]]
    assert any(name.startswith("S7") for name in names) and chat.calls
