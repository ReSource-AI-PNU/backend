"""L2 검색 평가(평가 설계 6장). 지표 식, 질의 세트, 말뭉치, 기준선 회귀를 본다."""

import json
import math
from types import SimpleNamespace

import pytest

from app.v3.eval import retrieval
from app.v3.eval.world import DEFAULT_TODAY
from app.v3.evidence import search
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import load_seed_dir
from app.v3.judge import regulation

COUNTS = {
    "dev": {"sentence": 13, "keyword": 7, "jargon": 7, "value": 7, "none": 6},
    "test": {"sentence": 27, "keyword": 13, "jargon": 13, "value": 13, "none": 14},
}


@pytest.fixture(scope="module")
def items():
    return retrieval.corpus(retrieval.corpus_registry())


# ── 지표 식(손으로 계산한 값) ────────────────────────────────────────


def test_ndcg_recall_and_mrr_match_hand_computed_values():
    relevant = {"a": 2, "c": 1, "d": 2}
    ranked = ["a", "b", "c"]
    dcg = 3 / math.log2(2) + 0 + 1 / math.log2(4)
    ideal = 3 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4)
    assert retrieval.ndcg_at(ranked, relevant, 10) == pytest.approx(dcg / ideal)
    assert retrieval.ndcg_at(["a", "d", "c"], relevant, 10) == pytest.approx(1.0)
    assert retrieval.ndcg_at(["b"], relevant, 10) == 0.0
    assert retrieval.recall_at(ranked, relevant, 2) == pytest.approx(1 / 3)
    assert retrieval.recall_at(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert retrieval.reciprocal_rank(ranked, relevant) == 1.0
    assert retrieval.reciprocal_rank(["x", "c", "d"], relevant) == pytest.approx(1 / 3)
    assert retrieval.reciprocal_rank(["c"], relevant) == 0.0  # 등급 1은 직접 근거가 아니다


def test_ndcg_cuts_at_k():
    relevant = {f"g{i}": 1 for i in range(12)}
    ranked = [f"x{i}" for i in range(10)] + ["g0"]
    assert retrieval.ndcg_at(ranked, relevant, 10) == 0.0


def test_threshold_is_chosen_by_balanced_accuracy_and_prefers_the_smaller_value():
    rows = [
        {"type": "none", "topScore": 1.0},
        {"type": "none", "topScore": 2.0},
        {"type": "sentence", "topScore": 5.0},
        {"type": "sentence", "topScore": 6.0},
    ]
    assert retrieval.choose_threshold(rows) == 3.5
    assert retrieval.choose_threshold([{"type": "sentence", "topScore": 0.0}]) == 0.0


# ── 질의 세트 ────────────────────────────────────────────────────────


def test_query_files_have_the_designed_counts_per_split_and_type():
    for split, wanted in COUNTS.items():
        data = json.loads(retrieval.QUERY_FILES[split].read_text(encoding="utf-8"))
        assert data["synthetic"] is True and data["split"] == split
        assert data["labeling"]["labelers"] == 1  # 이중 라벨 전이라는 표시
        found: dict[str, int] = {}
        for q in data["queries"]:
            found[q["type"]] = found.get(q["type"], 0) + 1
        assert found == wanted
    assert len(retrieval.load_queries("all")) == 120


def test_query_ids_and_texts_do_not_repeat_across_splits():
    queries = retrieval.load_queries("all")
    assert len({q["id"] for q in queries}) == 120
    assert len({q["query"].strip() for q in queries}) == 120


def test_every_gold_id_exists_in_the_corpus_with_a_valid_grade(items):
    known = {e.id for e in items}
    for q in retrieval.load_queries("all"):
        relevant = q["relevant"]
        if q["type"] == "none":
            assert relevant == {}, q["id"]
            continue
        assert relevant, q["id"]
        assert set(relevant) <= known, (q["id"], set(relevant) - known)
        assert set(relevant.values()) <= {1, 2}, q["id"]


# ── 말뭉치 ───────────────────────────────────────────────────────────


def test_extra_law_corpus_is_the_product_law_corpus_and_records_its_source():
    extra = json.loads(retrieval.EXTRA_CORPUS.read_text(encoding="utf-8"))
    assert len(extra) >= 300
    product = InMemoryRegistry()
    load_seed_dir(product)
    product_laws = {e.id: e for e in product.list(kinds=["law"], org_id=None)}
    ids = [x["id"] for x in extra]
    assert len(set(ids)) == len(ids)
    # 2026-11-19부터 ③ 규정 검토도 같은 조문을 본다(제품과 평가가 같은 말뭉치)
    assert set(ids) <= set(product_laws)
    assert all(product_laws[i].meta["evalOnly"] is False for i in ids)
    for x in extra:
        meta = x["meta"]
        assert x["kind"] == "law" and x["excerpt"].strip()
        assert meta["retrievedAt"]
        assert meta["fetchedFrom"].startswith("https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=")
        assert x["excerpt"].startswith(meta["article"])  # 조 번호로 시작하는 원문


def test_corpus_has_seeds_defaults_and_extra_laws(items):
    summary = retrieval.corpus_summary(items)
    assert summary["byKind"]["law"] >= 300
    assert summary["byKind"]["ref"] >= 1 and summary["byKind"]["assumption"] >= 1


# ── 방식이 제품 코드와 같은 순위를 내는가 ────────────────────────────


QUERIES = ["폐플라스틱 재활용 위탁", "위탁", "소각 처리", "PP 압축 경남 시장 가격", "필름"]


def test_regulation_tool_ranks_laws_like_the_evidence_search(items):
    """③ 도구는 근거 검색(S1, Azure가 연결되면 S6)을 법령으로 좁혀 쓴다(2026-11-19부터)."""
    registry = retrieval.corpus_registry()
    rec = SimpleNamespace(registry=registry, org_id=None, today=DEFAULT_TODAY)
    for query in QUERIES:
        expected = [
            hit["evidence"]["id"]
            for hit in search.search_evidence(registry, query, kinds=["law"], org_id=None, limit=20)
        ]
        assert [law.id for law, _ in regulation._search(rec, query, 20)] == expected[:20]


def test_s0_keeps_the_previous_word_count_for_comparison(items):
    """S0은 2026-11-19까지의 ③ 도구(법령 원문에 질의 낱말이 나온 횟수의 합)다."""
    laws = [e for e in items if e.kind == "law"]
    ranked = retrieval.rank_s0(laws, "소각 처리")
    assert ranked and all(score > 0 for _, score in ranked)
    first = next(e for e in laws if e.id == ranked[0][0])
    text = f"{first.label} {first.excerpt}"
    assert ranked[0][1] == text.count("소각") + text.count("처리")


def test_s1_ranks_like_the_evidence_search(items):
    registry = retrieval.corpus_registry()
    rankers = retrieval.local_rankers(items)
    assert search.backend_name() == search.LOCAL
    for query in QUERIES:
        expected = [
            (hit["evidence"]["id"], hit["score"])
            for hit in search.search_evidence(registry, query, org_id=None, limit=20)
        ]
        assert rankers["S1"](query)[:20] == expected


def test_s1v1_keeps_the_previous_lexical_score_for_comparison(items):
    legacy = retrieval.LegacyIndex(items)
    by_id = {e.id: e for e in items}
    for query in QUERIES:
        ranked = legacy.score_all(query)[:20]
        assert ranked and all(
            by_id[eid] and search.score(by_id[eid], query) == s for eid, s in ranked
        )


def test_s6_fuses_s1_with_s5_by_reciprocal_rank():
    s1 = lambda q: [("a", 0.9), ("b", 0.5)]
    s5 = lambda q: [("b", 3.0), ("c", 2.0)]
    fused = retrieval.fused_ranker(s1, s5)("x")
    assert [eid for eid, _ in fused] == ["b", "a", "c"]


# ── 층 결과와 기준선 ─────────────────────────────────────────────────


def test_run_layer_is_deterministic_and_reports_azure_as_not_measured():
    first = retrieval.run_layer("dev", resamples=50, env={})
    again = retrieval.run_layer("dev", resamples=50, env={})
    assert first == again
    assert first["layer"] == "L2" and first["n"] == 40 and first["unit"] == "질의"
    keys = [m["key"] for m in first["metrics"]]
    assert keys == ["ndcg@10", "recall@5", "recall@10", "mrr", "no_answer_accuracy", "answer_rate"]
    for m in first["metrics"]:
        assert m["ci"] is not None and m["ci"][0] <= m["value"] <= m["ci"][1]
        assert m["system"] == first["details"]["chosen"]
    assert sum("Azure AI Search 설정 없음" in item for item in first["notMeasured"]) == 5
    assert first["metrics"][0]["target"]["isTarget"] is True


def test_azure_systems_run_through_an_eval_only_index_when_opted_in():
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, json.loads(body) if body else None))
        if url.split("?")[0].endswith("/docs/search"):
            hit = {
                "access": ["*"],
                "payload": json.dumps({"id": "law:폐기물관리법:제18조@2026-11-12"}),
                "@search.score": 3.0,
            }
            return 200, json.dumps({"value": [hit]}).encode("utf-8")
        return 200, b"{}"

    env = {
        "V3_EVAL_L2_AZURE": "1",
        "AZURE_SEARCH_ENDPOINT": "https://search.invalid",
        "AZURE_SEARCH_API_KEY": "k",
    }
    result = retrieval.run_layer(
        "dev", resamples=20, systems=["S2", "S5"], env=env, azure_transport=transport
    )
    assert "/indexes/rsai-evidence-eval-l2" in calls[0][1]  # 제품 색인이 아닌 평가 전용 색인
    searches = [body for _, url, body in calls if "/docs/search" in url]
    assert any(body.get("queryType") != "semantic" for body in searches)  # S2: BM25
    assert any(body.get("queryType") == "semantic" for body in searches)  # S5
    assert set(result["details"]["queries"]) == {"S2", "S5"}
    both = retrieval.run_layer(
        "dev", resamples=20, systems=["S1", "S5", "S6"], env=env, azure_transport=transport
    )
    assert set(both["details"]["queries"]) == {"S1", "S5", "S6"}


def test_s0_and_s1_ndcg_have_not_dropped_below_the_saved_baseline():
    baseline = retrieval.load_baseline()
    assert baseline is not None and baseline["chosen"] in retrieval.LOCAL_SYSTEMS
    assert set(baseline["thresholds"]) == {"S0", "S1", "S1v1"}
    assert retrieval.check_baseline("dev") == []
    assert retrieval.check_baseline("test") == []
