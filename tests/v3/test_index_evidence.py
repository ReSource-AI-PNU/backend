"""검색 백엔드 선택 계층과 색인 스크립트. Azure AI Search와 임베딩 서버는 가짜 transport다."""

import json

import pytest

from app.v3.evidence import lexical, search, search_azure
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry
from scripts.v3 import index_evidence

SEARCH_ENV = {
    "AZURE_SEARCH_ENDPOINT": "https://search.invalid",
    "AZURE_SEARCH_API_KEY": "search-key",
}
OLLAMA_ENV = {**SEARCH_ENV, "V3_EMBEDDINGS": "ollama", "OLLAMA_BASE_URL": "http://ollama.invalid"}


def evidence(evidence_id, access=("*",), label="가상 근거", excerpt="가상 원문"):
    return Evidence(
        id=evidence_id,
        kind=evidence_id.split(":", 1)[0],
        label=label,
        excerpt=excerpt,
        source="가상 출처",
        as_of="2026-11-10",
        access=access,
    )


@pytest.fixture
def registry():
    made = InMemoryRegistry()
    made.put(evidence("law:가상법:제1조@2026-01-01", label="가상법 제1조", excerpt="폐기물 위탁"))
    made.put(evidence("law:가상법:제2조@2026-01-01", label="가상법 제2조", excerpt="폐기물 보관"))
    made.put(evidence("doc:org_a:계약서:단가", access=("org_a",), excerpt="폐기물 위탁 단가"))
    made.put(evidence("calc:tool:abc:합계", access=("org_a",)))
    return made


@pytest.fixture(autouse=True)
def local_backend():
    search.use_backend(None)
    yield
    search.use_backend(None)


class FakeSearch:
    """Azure AI Search REST API의 모양만 흉내 낸다."""

    def __init__(self, hits=(), status=200):
        self.hits, self.status, self.calls = list(hits), status, []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append((method, url, headers, payload))
        if self.status != 200:
            return self.status, b'{"error": {"message": "denied"}}'
        if url.split("?")[0].endswith("/docs/search"):
            return 200, json.dumps({"value": self.hits}).encode("utf-8")
        return 200, b"{}"


class FakeOllama:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        self.calls.append((url, payload))
        vectors = [[0.1, 0.2, 0.3] for _ in payload["input"]]
        return 200, {}, json.dumps({"embeddings": vectors}).encode("utf-8")


def hit(item, score=1.0):
    return {
        "@search.score": score,
        "access": list(item.access),
        "payload": json.dumps(item.to_dict(), ensure_ascii=False),
    }


# ── 검색 선택 계층 ───────────────────────────────────────────────────
def test_local_search_is_the_default(registry):
    assert search.backend_name() == "local"
    found = search.search_evidence(registry, "폐기물 위탁", org_id="org_a", limit=5)
    assert found[0]["evidence"]["id"] in ("law:가상법:제1조@2026-01-01", "doc:org_a:계약서:단가")


def test_backend_gives_the_order_and_registry_gives_the_content(registry):
    second = registry.get("law:가상법:제2조@2026-01-01")
    first = registry.get("law:가상법:제1조@2026-01-01")
    stale = Evidence(
        id=first.id, kind="law", excerpt="색인에 남은 옛 원문", source="x", as_of="2025-01-01"
    )
    private = registry.get("doc:org_a:계약서:단가")
    gone = evidence("law:가상법:제9조@2026-01-01")
    fake = FakeSearch(
        [hit(second, 3.0), hit(stale, 2.0), hit(second, 1.5), hit(private, 1.0), hit(gone, 0.5)]
    )
    client = search_azure.AzureSearchClient("https://search.invalid", "idx", "k", transport=fake)
    search.use_backend(client.search, fuse=False)
    assert search.backend_name() == "azure"

    found = search.search_evidence(registry, "폐기물", org_id="org_b", limit=10)
    # 순위는 백엔드, 내용은 레지스트리. 겹친 것, 볼 수 없는 것, 색인에만 남은 것은 빠진다
    assert [(f["evidence"]["id"], f["score"]) for f in found] == [(second.id, 3.0), (first.id, 2.0)]
    assert found[1]["evidence"]["excerpt"] == "폐기물 위탁"
    request = fake.calls[0][3]
    assert request["top"] == 10 and "org_b" in request["filter"]

    mine = search.search_evidence(registry, "폐기물", org_id="org_a", kinds=["doc"], limit=10)
    assert [f["evidence"]["id"] for f in mine] == [private.id]
    assert "search.in(kind, 'doc'" in fake.calls[1][3]["filter"]
    assert len(search.search_evidence(registry, "폐기물", org_id="org_a", limit=1)) == 1


def test_backend_and_local_rankings_are_fused_with_rrf_by_default(registry):
    first = registry.get("law:가상법:제1조@2026-01-01")
    second = registry.get("law:가상법:제2조@2026-01-01")
    private = registry.get("doc:org_a:계약서:단가")
    fake = FakeSearch([hit(second, 3.0), hit(private, 2.0)])
    client = search_azure.AzureSearchClient("https://search.invalid", "idx", "k", transport=fake)
    search.use_backend(client.search)
    local = search.rank_local(registry.list(org_id="org_b"), "폐기물 위탁")
    fused = search.search_evidence(registry, "폐기물 위탁", org_id="org_b", limit=10)
    expected = lexical.rrf([[second.id], [eid for eid, _ in local]])
    assert [(f["evidence"]["id"], f["score"]) for f in fused] == expected
    assert {f["evidence"]["id"] for f in fused} == {first.id, second.id}  # 볼 수 없는 것은 없다
    assert fake.calls[0][3]["top"] == search.FUSION_DEPTH  # 합치려고 더 깊이 받는다
    search.use_backend(None)


def test_backend_failure_and_empty_query_fall_back_to_local(registry):
    fake = FakeSearch(status=403)
    client = search_azure.AzureSearchClient("https://search.invalid", "idx", "k", transport=fake)
    search.use_backend(client.search)
    local = search.search_evidence(registry, "폐기물 위탁", org_id="org_b", limit=5)
    assert local and fake.calls  # 불렀지만 실패해 로컬 검색으로 답했다
    search.use_backend(None)
    assert search.search_evidence(registry, "폐기물 위탁", org_id="org_b", limit=5) == local
    search.use_backend(client.search)
    before = len(fake.calls)
    listed = search.search_evidence(registry, "  ", org_id="org_b", limit=5)
    assert [f["score"] for f in listed] == [0.0, 0.0] and len(fake.calls) == before
    assert search.search_evidence(registry, "폐기물", org_id="org_b", limit=0) == []


# ── 환경 변수로 고르기 ───────────────────────────────────────────────
def test_client_and_embeddings_from_env():
    assert search_azure.client_from_env({}) is None
    assert search_azure.client_from_env({"AZURE_SEARCH_ENDPOINT": "https://search.invalid"}) is None
    plain = search_azure.client_from_env(SEARCH_ENV)
    assert (plain.index, plain.vector_dimensions) == ("rsai-evidence", 1024)
    assert search_azure.embeddings_from_env(SEARCH_ENV) is None
    assert search_azure.embeddings_from_env({"V3_EMBEDDINGS": "none"}) is None
    with pytest.raises(ValueError, match="OLLAMA_BASE_URL"):
        search_azure.embeddings_from_env({"V3_EMBEDDINGS": "ollama"})
    with pytest.raises(ValueError, match="FOUNDRY_EMBEDDING_DEPLOYMENT"):
        search_azure.embeddings_from_env(
            {"V3_EMBEDDINGS": "foundry", "FOUNDRY_ENDPOINT": "https://f.invalid"}
        )
    with pytest.raises(ValueError, match="V3_EMBEDDINGS"):
        search_azure.embeddings_from_env({"V3_EMBEDDINGS": "other"})
    with pytest.raises(ValueError, match="정수"):
        search_azure.client_from_env({**SEARCH_ENV, "AZURE_SEARCH_VECTOR_DIMENSIONS": "많이"})
    ollama = FakeOllama()
    embed = search_azure.embeddings_from_env(OLLAMA_ENV, transport=ollama)
    assert embed(["가", "나"]) == [[0.1, 0.2, 0.3]] * 2
    assert ollama.calls[0] == (
        "http://ollama.invalid/api/embed",
        {"model": "bge-m3", "input": ["가", "나"]},
    )


# ── 색인 스크립트 ────────────────────────────────────────────────────
def test_script_needs_search_settings(registry, capsys):
    assert index_evidence.main([], {}, registry=registry) == 2
    assert "AZURE_SEARCH_ENDPOINT" in capsys.readouterr().err
    bad = {**SEARCH_ENV, "V3_EMBEDDINGS": "ollama"}
    assert index_evidence.main([], bad, registry=registry) == 2
    assert "OLLAMA_BASE_URL" in capsys.readouterr().err
    assert index_evidence.main(["--kinds", "법령"], SEARCH_ENV, registry=registry) == 2


def test_dry_run_calls_nothing(registry, capsys):
    fake, ollama = FakeSearch(), FakeOllama()
    code = index_evidence.main(
        ["--dry-run"], OLLAMA_ENV, transport=fake, embed_transport=ollama, registry=registry
    )
    assert code == 0 and fake.calls == [] and ollama.calls == []
    assert "근거 3건" in capsys.readouterr().out  # calc는 기본으로 올리지 않는다


def test_upload_creates_index_embeds_and_keeps_access(registry, capsys):
    fake, ollama = FakeSearch(), FakeOllama()
    code = index_evidence.main(
        ["--create-index"], OLLAMA_ENV, transport=fake, embed_transport=ollama, registry=registry
    )
    assert code == 0
    (put_method, put_url, put_headers, definition), (post_method, post_url, _, upload) = fake.calls
    assert (put_method, post_method) == ("PUT", "POST")
    assert put_url.startswith("https://search.invalid/indexes/rsai-evidence?api-version=")
    assert put_headers["api-key"] == "search-key" and definition["name"] == "rsai-evidence"
    assert post_url.split("?")[0].endswith("/indexes/rsai-evidence/docs/index")
    documents = {doc["evidenceId"]: doc for doc in upload["value"]}
    assert set(documents) == {
        "law:가상법:제1조@2026-01-01",
        "law:가상법:제2조@2026-01-01",
        "doc:org_a:계약서:단가",
    }
    assert documents["doc:org_a:계약서:단가"]["access"] == ["org_a"]  # 조직 필터에 쓰인다
    assert all(doc[search_azure.VECTOR_FIELD] == [0.1, 0.2, 0.3] for doc in upload["value"])
    assert len(ollama.calls) == 1 and len(ollama.calls[0][1]["input"]) == 3
    printed = capsys.readouterr().out
    assert "3건을 올렸다" in printed and "벡터 포함" in printed and "search-key" not in printed

    everything = index_evidence.run(
        registry, search_azure.client_from_env(SEARCH_ENV, transport=FakeSearch()), kinds=("calc",)
    )
    assert everything["uploaded"] == 1 and everything["private"] == 1
    assert index_evidence.parse_kinds("all")[-1:] != () and "calc" in index_evidence.parse_kinds(
        "all"
    )


def test_search_failure_is_reported_without_the_key(registry, capsys):
    code = index_evidence.main([], SEARCH_ENV, transport=FakeSearch(status=403), registry=registry)
    assert code == 1
    err = capsys.readouterr().err
    assert "HTTP 403" in err and "search-key" not in err


def test_default_registry_is_the_seed_set():
    seeds = index_evidence.build_registry({})
    summary = index_evidence.run(seeds, search_azure.client_from_env(SEARCH_ENV), dry_run=True)
    # 제품 법령 26조 + 법령 말뭉치 348조(2026-11-19부터 ③ 규정 검토가 함께 본다)
    assert summary["byKind"]["law"] == 374 and summary["byKind"]["ref"] == 54
    assert summary["private"] == 0 and summary["uploaded"] == 0
