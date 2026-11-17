"""L2 검색 평가(평가 설계 6장). 조항·근거 검색이 정답 근거를 위로 올리는지 잰다.

    run_layer("test") -> stats.layer("L2", ...)
    python -m app.v3.eval.retrieval --split dev|test|all [--out 결과.json] [--write-baseline]

말뭉치는 공개 근거(법령 시드, 가격조사·KAU 등 기준값, 팀 가정값)와 평가 전용 법령 말뭉치
(sets/corpus/laws_extra.json, law.go.kr 본문에서 가져온 조 단위 원문)를 합친 것이다. 평가 전용
말뭉치는 제품의 근거 레지스트리에 넣지 않는다(시드로 옮길지는 따로 정한다).

방식(6장):
- S0: ③ 도구(judge/regulation.py)의 낱말 수 세기. 법령만 본다.
- S1: 근거 검색(evidence/search.py)의 로컬 순위. BM25F(근거 이름·원문·출처·ID 필드 가중) + 한국어
  낱말 나누기(조사 떼기, 글자 2-gram, 조 번호) + 분야 동의어(evidence/lexical.py). 공개 근거 전부를 본다.
- S1v1: 2026-11-18까지의 S1(낱말 + 한글 2-gram 비율 점수). 고친 뒤와 견주는 참고 기준이다.
- S2~S5: Azure AI Search(BM25, 벡터, 하이브리드, 하이브리드 + 시맨틱 랭커). 설정이 있고
  V3_EVAL_L2_AZURE=1일 때만 평가 전용 색인을 만들어 돈다. 없으면 미측정.
- S6: S1과 S5의 순위를 RRF로 합친 하이브리드(제품의 search_evidence가 백엔드를 연결했을 때와 같다).
  S5가 돌 때만 잰다.

S1의 동의어 표와 무게는 개발 질의(dev)만 보고 정했다. 시험 질의(test-v1)는 고친 뒤 한 번 쟀다.

지표는 BEIR(Thakur et al. 2021)의 nDCG@10과 Järvelin·Kekäläinen(2002)의 nDCG 정의를 따른다.
등급 2 = 직접 근거, 1 = 관련, 0 = 무관. 모든 숫자에 질의 단위 부트스트랩 95% CI를 붙인다.
현재 시각이나 난수를 쓰지 않아 같은 입력이면 같은 결과다(부트스트랩 시드도 고정).
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.v3.eval import stats
from app.v3.eval.world import DEFAULT_TODAY, base_registry
from app.v3.evidence import lexical, search
from app.v3.evidence.model import PUBLIC, Evidence
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import load_seed_file
from app.v3.judge import regulation

SETS = Path(__file__).with_name("sets")
QUERY_FILES = {
    "dev": SETS / "dev" / "l2_queries.json",
    "test": SETS / "test-v1" / "l2_queries.json",
}
EXTRA_CORPUS = SETS / "corpus" / "laws_extra.json"
BASELINE = SETS / "baselines" / "l2.json"

TYPES = {
    "sentence": "문장형",
    "keyword": "낱말형",
    "jargon": "현장 말",
    "value": "기준값",
    "none": "답 없음",
}
LOCAL_SYSTEMS = {
    "S0": "③ 도구 낱말 수 세기(법령만)",
    "S1": "근거 검색 BM25F(필드 가중 + 한국어 낱말·2-gram + 분야 동의어)",
    "S1v1": "이전 근거 검색 어휘 점수(낱말 + 한글 2-gram, 참고)",
}
AZURE_SYSTEMS = {
    "S2": "AI Search BM25 전문 검색",
    "S3": "벡터(임베딩)",
    "S4": "하이브리드(BM25 + 벡터, RRF)",
    "S5": "하이브리드 + 시맨틱 랭커",
    "S6": "S1 + S5 하이브리드(RRF)",
}
SYSTEMS = {**LOCAL_SYSTEMS, **AZURE_SYSTEMS}
AZURE_OPT_IN = "V3_EVAL_L2_AZURE"
DEPTH = 20  # 순위를 이만큼 받아 둔다(지표는 10까지만 본다)
BASELINE_TOLERANCE = 0.02  # 평가 설계 16장: S0·S1 nDCG@10이 기준선보다 0.02 넘게 떨어지면 실패

GOALS = {
    "ndcg@10": stats.target(">=", 0.75),
    "recall@5": stats.target(">=", 0.85),
    "recall@10": stats.target(">=", 0.92),
    "mrr": stats.target(">=", 0.70),
    "no_answer_accuracy": stats.target(">=", 0.80),
}
LABELS = {
    "ndcg@10": "nDCG@10",
    "recall@5": "Recall@5",
    "recall@10": "Recall@10",
    "mrr": "MRR",
    "no_answer_accuracy": "답 없음 정확도",
    "answer_rate": "답 있는 질의 응답률(문턱값 이상)",
}

Ranking = list[tuple[str, float]]  # (근거 ID, 점수), 점수 내림차순


# ── 질의와 말뭉치 ────────────────────────────────────────────────────


def load_queries(split: str) -> list[dict[str, Any]]:
    """split: dev | test | all. 질의마다 split을 붙여 돌려준다."""
    names = ["dev", "test"] if split == "all" else [split]
    out: list[dict[str, Any]] = []
    for name in names:
        if name not in QUERY_FILES:
            raise ValueError(f"split은 dev, test, all 가운데 하나다: {split}")
        data = json.loads(QUERY_FILES[name].read_text(encoding="utf-8"))
        out.extend({**q, "split": name} for q in data["queries"])
    return out


def corpus_registry(*, extended: bool = True) -> InMemoryRegistry:
    """공개 근거 레지스트리. extended면 평가 전용 법령 말뭉치를 더한다."""
    registry = base_registry()
    if extended and EXTRA_CORPUS.exists():
        load_seed_file(registry, EXTRA_CORPUS)
    return registry


def corpus(registry: InMemoryRegistry) -> list[Evidence]:
    return [e for e in registry.list(org_id=None) if PUBLIC in e.access]


def corpus_summary(items: Sequence[Evidence]) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for e in items:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    extra = 0
    digest = None
    if EXTRA_CORPUS.exists():
        raw = EXTRA_CORPUS.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        extra = len(json.loads(raw.decode("utf-8")))
    return {
        "documents": len(items),
        "byKind": dict(sorted(kinds.items())),
        "extraLaws": extra,
        "extraLawsSha256": digest,
    }


# ── 방식 ─────────────────────────────────────────────────────────────


def rank_s0(laws: Sequence[Evidence], query: str, today: str = DEFAULT_TODAY) -> Ranking:
    """judge/regulation.py의 _search와 같은 점수: (라벨 + 원문)에 질의 낱말이 나온 횟수의 합."""
    tokens = regulation._tokens(query)
    scored = []
    for law in laws:
        if law.kind != "law" or law.expired(today):
            continue
        haystack = f"{law.label} {law.excerpt}"
        value = sum(haystack.count(t) for t in tokens)
        if value > 0:
            scored.append((-value, law.id))
    scored.sort()
    return [(eid, float(-neg)) for neg, eid in scored]


class LegacyIndex:
    """S1v1: 이전 판의 search.score()를 문서 쪽 값만 미리 계산해 그대로 낸다(결과가 같아야 한다)."""

    def __init__(self, items: Sequence[Evidence]) -> None:
        self.docs = []
        for e in items:
            fields = []
            for name, weight in search._FIELD_WEIGHTS:
                value = str(getattr(e, name) or "").lower()
                if value:
                    fields.append((weight, value, search.bigrams(value)))
            self.docs.append((e.id, fields))

    def score_all(self, query: str) -> Ranking:
        tokens = list(dict.fromkeys(search.tokenize(query)))
        grams = search.bigrams(query)
        if not tokens and not grams:
            return []
        scored = []
        for eid, fields in self.docs:
            total = 0.0
            for weight, value, value_grams in fields:
                hits = sum(1 for token in tokens if token in value)
                if tokens:
                    total += weight * hits / len(tokens)
                if grams:
                    total += 0.5 * weight * len(grams & value_grams) / len(grams)
            total = round(total, 6)
            if total > 0:
                scored.append((-total, eid))
        scored.sort()
        return [(eid, -neg) for neg, eid in scored]


def local_rankers(items: Sequence[Evidence]) -> dict[str, Callable[[str], Ranking]]:
    index = lexical.LexicalIndex(items)  # search.rank_local과 같다(색인을 한 번만 만든다)
    legacy = LegacyIndex(items)
    laws = [e for e in items if e.kind == "law"]
    return {"S0": lambda q: rank_s0(laws, q), "S1": index.rank, "S1v1": legacy.score_all}


def fused_ranker(*rankers: Callable[[str], Ranking]) -> Callable[[str], Ranking]:
    """순위들을 RRF(k=60)로 합친다(search.search_evidence의 하이브리드와 같다)."""

    def rank(query: str) -> Ranking:
        lists = [[eid for eid, _ in ranker(query)][: search.FUSION_DEPTH] for ranker in rankers]
        return lexical.rrf(lists)

    return rank


def azure_rankers(
    items: Sequence[Evidence], env: Mapping[str, str], *, transport: Any = None
) -> tuple[dict[str, Callable[[str], Ranking]], str | None]:
    """(방식 → 순위 함수, 미측정 이유). 설정이 없거나 켜지 않았으면 빈 사전과 이유."""
    from app.v3.evidence import search_azure as azure

    if (env.get(AZURE_OPT_IN) or "").strip() != "1":
        client = None
        reason = f"Azure AI Search 설정 없음(또는 {AZURE_OPT_IN}=1이 아님)"
    else:
        client = azure.client_from_env(env, transport=transport)
        reason = None if client is not None else "Azure AI Search 설정 없음"
    if client is None:
        return {}, reason
    # 평가 전용 색인. 제품 색인(AZURE_SEARCH_INDEX)을 건드리지 않는다.
    client.index = f"{client.index}-eval-l2"
    client.create_index()
    client.upsert(list(items))
    embed = client._embed

    def run(payload: dict[str, Any]) -> Ranking:
        response = client._call("POST", f"/indexes/{client.index}/docs/search", payload)
        out = []
        for hit in response.get("value", []):
            if PUBLIC not in (hit.get("access") or ()):
                continue
            found = azure.from_hit(hit)
            out.append((str(found["evidence"]["id"]), float(found["score"])))
        return out

    def payload(query: str, *, text: bool, vector: bool, semantic: bool) -> dict[str, Any]:
        return azure.query_payload(
            query if text else "*",
            org_id=None,
            top=DEPTH,
            vector=embed([query])[0] if vector and embed else None,
            semantic=semantic,
        )

    rankers: dict[str, Callable[[str], Ranking]] = {
        "S2": lambda q: run(payload(q, text=True, vector=False, semantic=False)),
        "S5": lambda q: run(payload(q, text=True, vector=bool(embed), semantic=True)),
    }
    if embed is not None:
        rankers["S3"] = lambda q: run(payload(q, text=False, vector=True, semantic=False))
        rankers["S4"] = lambda q: run(payload(q, text=True, vector=True, semantic=False))
    return rankers, None


# ── 질의 하나의 지표 ─────────────────────────────────────────────────


def dcg(grades: Sequence[int]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ndcg_at(ranked: Sequence[str], relevant: Mapping[str, int], k: int = 10) -> float:
    ideal = dcg(sorted(relevant.values(), reverse=True)[:k])
    if ideal <= 0:
        return 0.0
    return dcg([relevant.get(eid, 0) for eid in ranked[:k]]) / ideal


def recall_at(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    wanted = {eid for eid, g in relevant.items() if g >= 1}
    if not wanted:
        return 0.0
    return len(wanted & set(ranked[:k])) / len(wanted)


def reciprocal_rank(ranked: Sequence[str], relevant: Mapping[str, int]) -> float:
    """첫 직접 근거(등급 2)의 순위의 역수. 못 찾으면 0."""
    for i, eid in enumerate(ranked, 1):
        if relevant.get(eid, 0) >= 2:
            return 1.0 / i
    return 0.0


def judge_query(query: dict[str, Any], ranking: Ranking) -> dict[str, Any]:
    relevant = {str(k): int(v) for k, v in (query.get("relevant") or {}).items() if int(v) > 0}
    ids = [eid for eid, _ in ranking]
    top = ranking[0][1] if ranking else 0.0
    row: dict[str, Any] = {
        "id": query["id"],
        "type": query["type"],
        "split": query.get("split"),
        "topScore": top,
        "top10": ids[:10],
    }
    if relevant:
        row.update(
            {
                "ndcg@10": ndcg_at(ids, relevant, 10),
                "recall@5": recall_at(ids, relevant, 5),
                "recall@10": recall_at(ids, relevant, 10),
                "mrr": reciprocal_rank(ids, relevant) if 2 in relevant.values() else None,
            }
        )
    return row


def choose_threshold(rows: Sequence[dict[str, Any]]) -> float:
    """개발셋에서 답 없음 문턱값을 고른다. 최고 점수가 문턱값 아래면 "답 없음"으로 본다.

    답 없는 질의를 바르게 거른 비율과 답 있는 질의에 답한 비율의 평균(균형 정확도)이 가장 큰
    문턱값. 같으면 작은 쪽. 후보는 최고 점수들 사이의 중간값과 0이다.
    """
    scores = sorted({float(r["topScore"]) for r in rows})
    candidates = [0.0, *[(a + b) / 2 for a, b in itertools.pairwise(scores)]]
    candidates.append(scores[-1] + 1.0 if scores else 1.0)
    none = [r for r in rows if r["type"] == "none"]
    answerable = [r for r in rows if r["type"] != "none"]

    def balanced(t: float) -> float:
        rejected = sum(1 for r in none if r["topScore"] < t) / len(none) if none else 0.0
        answered = sum(1 for r in answerable if r["topScore"] >= t)
        return (rejected + (answered / len(answerable) if answerable else 0.0)) / 2

    best = max(candidates, key=lambda t: (balanced(t), -t))
    return round(best, 6)


def run_system(ranker: Callable[[str], Ranking], queries: Sequence[dict[str, Any]]) -> list[dict]:
    return [judge_query(q, ranker(q["query"])[:DEPTH]) for q in queries]


# ── 기준선 ───────────────────────────────────────────────────────────


def load_baseline() -> dict[str, Any] | None:
    if not BASELINE.exists():
        return None
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_baseline() -> dict[str, Any]:
    """개발셋으로 방식과 문턱값을 고르고, S0·S1 nDCG@10을 split마다 적는다."""
    items = corpus(corpus_registry())
    rankers = local_rankers(items)
    dev = load_queries("dev")
    test = load_queries("test")
    thresholds: dict[str, float] = {}
    ndcg: dict[str, dict[str, float | None]] = {}
    for name, ranker in rankers.items():
        dev_rows = run_system(ranker, dev)
        test_rows = run_system(ranker, test)
        thresholds[name] = choose_threshold(dev_rows)
        ndcg[name] = {
            "dev": _mean([r["ndcg@10"] for r in dev_rows if "ndcg@10" in r]),
            "test": _mean([r["ndcg@10"] for r in test_rows if "ndcg@10" in r]),
        }
    chosen = max(ndcg, key=lambda n: (ndcg[n]["dev"] or 0.0, n == "S1"))
    return {
        "note": (
            "개발셋(dev)으로 고른 방식과 답 없음 문턱값, S0·S1의 nDCG@10. CI가 이 값보다 "
            f"{BASELINE_TOLERANCE} 넘게 떨어지는지 본다(평가 설계 16장). 질의나 말뭉치를 바꾸면 "
            "python -m app.v3.eval.retrieval --write-baseline 으로 다시 쓰고 이유를 커밋에 적는다."
        ),
        "chosenBy": "dev nDCG@10",
        "chosen": chosen,
        "thresholds": thresholds,
        "ndcg@10": ndcg,
        "corpus": corpus_summary(items),
    }


# ── 층 결과 ──────────────────────────────────────────────────────────


def _metric_rows(
    system: str, rows: Sequence[dict[str, Any]], threshold: float, resamples: int
) -> list[dict[str, Any]]:
    answerable = [r for r in rows if r["type"] != "none"]
    none = [r for r in rows if r["type"] == "none"]
    with_direct = [r for r in answerable if r.get("mrr") is not None]
    name = f"{system} {SYSTEMS[system]}"
    out = [
        stats.rate_metric(
            key,
            LABELS[key],
            [r[key] for r in answerable],
            goal=GOALS[key],
            note=name,
            resamples=resamples,
        )
        for key in ("ndcg@10", "recall@5", "recall@10")
    ]
    out.append(
        stats.rate_metric(
            "mrr",
            LABELS["mrr"],
            [r["mrr"] for r in with_direct],
            goal=GOALS["mrr"],
            note=f"{name}. 직접 근거(등급 2)가 있는 질의만",
            resamples=resamples,
        )
    )
    out.append(
        stats.rate_metric(
            "no_answer_accuracy",
            LABELS["no_answer_accuracy"],
            [r["topScore"] < threshold for r in none],
            goal=GOALS["no_answer_accuracy"],
            note=f"{name}. 문턱값 {threshold:g}(개발셋에서 정함)",
            resamples=resamples,
        )
    )
    out.append(
        stats.rate_metric(
            "answer_rate",
            LABELS["answer_rate"],
            [r["topScore"] >= threshold for r in answerable],
            note=f"{name}. 같은 문턱값. 목표 없음(답 없음 정확도와 함께 본다)",
            resamples=resamples,
        )
    )
    for m in out:
        m["system"] = system
    return out


def _fmt(value: float | None) -> str:
    return stats.NOT_MEASURED if value is None else f"{value:.3f}"


def cell(m: dict[str, Any]) -> str:
    """방식별 비교 표의 칸: 값 [95% CI]."""
    ci = m["ci"]
    return f"{_fmt(m['value'])} [{_fmt(ci[0])}, {_fmt(ci[1])}]" if ci else _fmt(m["value"])


_cell = cell


def run_layer(
    split: str = "test",
    *,
    resamples: int = stats.RESAMPLES,
    systems: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    azure_transport: Any = None,
) -> dict[str, Any]:
    """split의 질의로 방식마다 순위를 내고 지표를 잰다. Azure 방식은 설정이 있을 때만 돈다."""
    env = os.environ if env is None else env
    queries = load_queries(split)
    items = corpus(corpus_registry())
    wanted = list(systems or SYSTEMS)
    local = local_rankers(items)
    rankers = {k: v for k, v in local.items() if k in wanted}
    not_measured: list[str] = []
    azure_wanted = [s for s in wanted if s in AZURE_SYSTEMS]
    if azure_wanted:
        found, reason = azure_rankers(items, env, transport=azure_transport)
        if "S5" in found:
            found["S6"] = fused_ranker(local["S1"], found["S5"])
        for name in azure_wanted:
            if name in found:
                rankers[name] = found[name]
            else:
                why = reason or "임베딩 설정(V3_EMBEDDINGS) 없음"
                not_measured.append(f"{name} {SYSTEMS[name]}: {why}")
    baseline = load_baseline() or {}
    thresholds = dict(baseline.get("thresholds") or {})
    results = {name: run_system(ranker, queries) for name, ranker in rankers.items()}
    dev_rows: dict[str, list[dict[str, Any]]] = {}

    def on_dev(name: str) -> list[dict[str, Any]]:
        if name not in dev_rows:
            dev_rows[name] = (
                results[name] if split == "dev" else run_system(rankers[name], load_queries("dev"))
            )
        return dev_rows[name]

    for name in results:
        if name not in thresholds:  # 기준선에 없는 방식(Azure)은 개발셋으로 바로 정한다
            thresholds[name] = choose_threshold(on_dev(name))
    chosen = baseline.get("chosen")
    if chosen not in results:
        dev_ndcg = {
            name: _mean([r["ndcg@10"] for r in on_dev(name) if "ndcg@10" in r]) or 0.0
            for name in results
        }
        chosen = max(dev_ndcg, key=lambda n: dev_ndcg[n]) if dev_ndcg else None

    by_system = {
        name: _metric_rows(name, rows, thresholds[name], resamples)
        for name, rows in results.items()
    }
    compare_rows = [
        [f"{name} {SYSTEMS[name]}", *[_cell(m) for m in rows]] for name, rows in by_system.items()
    ]
    compare_rows += [
        [f"{name} {SYSTEMS[name]}", *([stats.NOT_MEASURED] * len(LABELS))]
        for name in wanted
        if name not in results
    ]
    type_rows = []
    for kind, label in TYPES.items():
        n = sum(1 for q in queries if q["type"] == kind)
        cells = []
        for name, rows in results.items():
            picked = [r for r in rows if r["type"] == kind]
            if kind == "none":
                value = _mean([float(r["topScore"] < thresholds[name]) for r in picked])
            else:
                value = _mean([r["ndcg@10"] for r in picked])
            cells.append(_fmt(value))
        small = " (표본 적음)" if n < stats.SMALL_SAMPLE else ""
        type_rows.append([label, f"{n}{small}", *cells])
    answerable = sum(1 for q in queries if q["type"] != "none")
    summary = corpus_summary(items)
    notes = [
        (
            f"말뭉치 {summary['documents']}건(종류별 {summary['byKind']}), 그 가운데 평가 전용 "
            f"법령 {summary['extraLaws']}조(law.go.kr 본문, sets/corpus/laws_extra.json)."
        ),
        (
            f"주 지표는 개발셋 nDCG@10으로 고른 방식({chosen})이다. 답 있는 질의 {answerable}개, "
            f"답 없는 질의 {len(queries) - answerable}개."
        ),
        "정답 등급은 한 사람(초안)이 근거 원문을 읽고 매겼다. 이중 라벨과 κ는 아직 재지 않았다.",
        "S0은 법령만 보므로 기준값 질의의 정답(가격조사, KAU, 가정값)을 찾을 수 없다.",
        (
            "S1(BM25F)의 동의어 표(evidence/synonyms.ko.json)와 무게는 개발 질의로만 정했고, 시험 "
            "질의는 고친 뒤 한 번 쟀다. S1v1은 고치기 전의 S1이다(같은 질의, 같은 말뭉치)."
        ),
    ]
    not_measured.append("인용 문장 적합률: 질의별 요건 라벨이 없어 미측정")
    not_measured.append("관련 등급의 이중 라벨과 Cohen's κ(평가 설계 13장)")
    return stats.layer(
        "L2",
        "검색",
        unit="질의",
        n=len(queries),
        metrics=by_system.get(chosen, []) if chosen else [],
        tables=[
            {
                "title": "방식별 비교(값 [95% CI])",
                "columns": ["방식", *LABELS.values()],
                "rows": compare_rows,
            },
            {
                "title": "질의 종류별 nDCG@10(답 없음은 답 없음 정확도)",
                "columns": ["종류", "n", *results],
                "rows": type_rows,
            },
        ],
        not_measured_items=not_measured,
        notes=notes,
        details={
            "split": split,
            "chosen": chosen,
            "thresholds": {name: thresholds[name] for name in results},
            "corpus": summary,
            "bySystem": by_system,
            "queries": results,
        },
    )


def check_baseline(split: str = "dev", *, tolerance: float = BASELINE_TOLERANCE) -> list[str]:
    """S0·S1 nDCG@10이 저장한 기준선보다 tolerance 넘게 떨어졌으면 문제 목록."""
    baseline = load_baseline()
    if baseline is None:
        return ["기준선 파일이 없다"]
    items = corpus(corpus_registry())
    queries = load_queries(split)
    problems = []
    for name, ranker in local_rankers(items).items():
        now = _mean([r["ndcg@10"] for r in run_system(ranker, queries) if "ndcg@10" in r]) or 0.0
        saved = baseline["ndcg@10"][name][split]
        if now < saved - tolerance:
            problems.append(f"{name} {split} nDCG@10 {now:.4f} < 기준선 {saved:.4f} − {tolerance}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L2 검색 평가")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="test")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--resamples", type=int, default=stats.RESAMPLES)
    parser.add_argument(
        "--write-baseline", action="store_true", help="개발셋으로 방식·문턱값을 다시 정해 쓴다"
    )
    args = parser.parse_args(argv)
    if args.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(build_baseline(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sys.stdout.write(f"{BASELINE}\n")
    result = run_layer(args.split, resamples=args.resamples)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    for row in result["tables"][0]["rows"]:
        sys.stdout.write(" | ".join(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
