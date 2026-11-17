"""근거 검색의 BM25F 어휘 순위(evidence/lexical.py). 법령 문구는 시험용으로 지어낸 것이다."""

from __future__ import annotations

from app.v3.evidence import lexical
from app.v3.evidence.model import Evidence


def law(number: str, title: str, body: str) -> Evidence:
    return Evidence(
        id=f"law:시험법:제{number}조@2026-01-01",
        kind="law",
        label=f"시험법 제{number}조({title})",
        excerpt=body,
        source="시험 자료",
        as_of="2026-01-01",
    )


DOCS = [
    law(
        "1",
        "정의",
        "“재활용”이란 폐기물을 다시 쓰거나 다시 쓸 수 있는 상태로 만드는 활동을 말한다.",
    ),
    law("2", "처리의 위탁", "사업장폐기물배출자는 수탁자에게 폐합성수지 처리를 위탁할 수 있다."),
    law(
        "3",
        "보관",
        "처리업자는 폐기물을 보관기한 안에 처리하여야 한다. 보관량은 30일 처리량 이하다.",
    ),
    law("4", "전산 처리", "인계ㆍ인수 내용은 전자정보처리프로그램에 입력하여야 한다."),
    law("5", "벌칙", "제2조를 위반한 자는 과태료를 부과한다. 폐기물 폐기물 폐기물."),
]


def test_words_strip_particles_and_keep_article_numbers():
    assert lexical.words("허가품목을 법에서 뜻은 제16조의13에 따라") == [
        "허가품목",
        "법",
        "뜻",
        "제16조의13",
        "에",  # 조 번호 뒤에 홀로 남은 조사(낱말 수에는 들지만 거의 모든 조에 있어 무게가 낮다)
        "따라",
    ]
    assert lexical.words("사이 결과 PP") == ["사이", "결과", "pp"]  # 끝 글자가 조사처럼 보여도
    assert lexical.grams("폐합성수지") == ["폐합", "합성", "성수", "수지"]
    assert "#합성" in lexical.terms("폐합성수지")


def test_query_terms_drop_question_words_and_expand_domain_words():
    weights = lexical.query_terms("폐플라스틱 처리는 무엇인가")
    assert weights["폐플라스틱"] == lexical.WORD_WEIGHT and "무엇인가" not in weights
    assert weights["폐합성수지"] == lexical.WORD_WEIGHT * lexical.EXPANSION_WEIGHT
    assert "폐합성수지" not in lexical.query_terms("폐플라스틱", expand=False)


def test_rank_finds_the_law_word_for_a_staff_word_and_scores_between_zero_and_one():
    index = lexical.LexicalIndex(DOCS)
    ranked = index.rank("폐플라스틱 처리를 위탁하려면")
    assert ranked[0][0] == DOCS[1].id
    assert all(0 < score <= 1 for _, score in ranked)
    assert [s for _, s in ranked] == sorted((s for _, s in ranked), reverse=True)
    assert index.rank("재활용의 뜻은")[0][0] == DOCS[0].id
    assert index.rank("처리업자가 보관할 수 있는 기간")[0][0] == DOCS[2].id
    assert index.rank("전혀없는낱말xyz") == [] and index.rank("  ") == []


def test_title_matches_outweigh_repeated_body_words():
    index = lexical.LexicalIndex(DOCS)
    assert index.rank("폐기물 벌칙")[0][0] == DOCS[4].id
    assert index.rank("폐기물 전산 처리")[0][0] == DOCS[3].id


def test_the_index_cache_follows_versions_and_text():
    first = lexical.LexicalIndex.cached(DOCS)
    assert lexical.LexicalIndex.cached(list(DOCS)) is first
    changed = [*DOCS[:-1], law("5", "벌칙", "과태료 규정이 바뀌었다.")]
    other = lexical.LexicalIndex.cached(changed)
    assert other is not first and other.rank("과태료 규정")[0][0] == DOCS[4].id


def test_rrf_merges_rankings_deterministically():
    assert lexical.rrf([["a", "b"], ["b", "c"]]) == [
        ("b", round(1 / 62 + 1 / 61, 9)),
        ("a", round(1 / 61, 9)),
        ("c", round(1 / 62, 9)),
    ]
    assert lexical.rrf([["x"], ["y"]]) == [("x", round(1 / 61, 9)), ("y", round(1 / 61, 9))]
    assert lexical.rrf([]) == []


def test_synonyms_load_from_the_data_file():
    loaded = lexical.load_synonyms()
    assert loaded == lexical.SYNONYMS and loaded["폐플라스틱"][0] == "폐합성수지"
