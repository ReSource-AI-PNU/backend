import base64
import json

import pytest

from app.v3.evidence import search_azure as az
from app.v3.evidence.model import Evidence

PRICE = Evidence(
    id="doc:org_saegil:매입기준표:PP-A-단가", kind="doc", label="PP A등급 매입 단가",
    excerpt="PP 분쇄품 A등급은 kg당 320원에 매입한다.", source="매입 기준표", as_of="2026-11-01",
    value=320, unit="원/kg", access=("org_saegil", "org_hanbit"),
)  # fmt: skip
LAW = Evidence(
    id="law:시험용법:제1조@2026-01-01", kind="law", label="허가 품목 확인",
    excerpt="수탁자의 허가 품목을 확인하여야 한다.", source="시험용법", as_of="2026-01-01",
)  # fmt: skip
SECRET = Evidence(
    id="doc:org_other:위탁계약서:단가", kind="doc", excerpt="x", source="y", as_of="2026-11-01",
    access=("org_other",),
)  # fmt: skip


class FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, None if body is None else json.loads(body)))
        return self.responses.pop(0) if self.responses else (200, b"{}")


def fake_embed(texts):
    return [[float(len(t)), 1.0, 0.0] for t in texts]


def hit(evidence, **extra):
    doc = az.to_document(evidence)
    return {k: doc[k] for k in ("id", "evidenceId", "kind", "access", "payload")} | extra


def client(transport, **kw):
    return az.AzureSearchClient(
        "https://srch.example.net/", "evidence", "test-key", transport=transport, **kw
    )


def test_doc_key_is_azure_safe_and_reversible():
    key = az.doc_key(PRICE.id)
    assert set(key) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-=")
    assert base64.urlsafe_b64decode(key).decode() == PRICE.id


def test_index_definition():
    index = az.index_definition("evidence", vector_dimensions=1024)
    fields = {f["name"]: f for f in index["fields"]}
    assert fields["id"]["key"] is True
    assert fields["access"]["type"] == "Collection(Edm.String)" and fields["access"]["filterable"]
    assert fields["kind"]["filterable"]
    assert fields["excerpt"]["analyzer"] == "ko.microsoft"
    assert fields[az.VECTOR_FIELD]["dimensions"] == 1024
    assert (
        fields[az.VECTOR_FIELD]["vectorSearchProfile"]
        == index["vectorSearch"]["profiles"][0]["name"]
    )
    assert index["semantic"]["configurations"][0]["name"] == az.SEMANTIC_CONFIG
    json.dumps(index)


def test_documents_round_trip_payload():
    doc = az.to_document(PRICE, [0.1, 0.2])
    assert doc["@search.action"] == "mergeOrUpload"
    assert doc["access"] == ["org_saegil", "org_hanbit"]
    assert doc[az.VECTOR_FIELD] == [0.1, 0.2]
    assert Evidence.from_dict(json.loads(doc["payload"])) == PRICE
    assert az.VECTOR_FIELD not in az.to_document(PRICE)
    with pytest.raises(ValueError, match="벡터"):
        az.upsert_payload([PRICE, LAW], [[0.1]])


def test_org_filter_escapes_and_fails_closed():
    assert az.org_filter("org_hanbit") == "access/any(a: a eq '*' or a eq 'org_hanbit')"
    assert az.org_filter("o'rg") == "access/any(a: a eq '*' or a eq 'o''rg')"
    assert az.org_filter(None) == "access/any(a: a eq '*')"


def test_query_payload_hybrid_semantic():
    payload = az.query_payload(
        "허가 품목", org_id="org_hanbit", kinds=["law", "doc"], top=5, vector=[1, 2, 3]
    )
    assert payload["search"] == "허가 품목"
    assert payload["filter"] == (
        "(access/any(a: a eq '*' or a eq 'org_hanbit')) and (search.in(kind, 'doc,law', ','))"
    )
    assert payload["queryType"] == "semantic"
    assert payload["semanticConfiguration"] == az.SEMANTIC_CONFIG
    assert payload["vectorQueries"] == [
        {"kind": "vector", "vector": [1.0, 2.0, 3.0], "fields": az.VECTOR_FIELD, "k": 5}
    ]
    plain = az.query_payload("x", org_id="org_hanbit", semantic=False)
    assert "queryType" not in plain and "vectorQueries" not in plain


def test_create_index_and_upsert_calls():
    transport = FakeTransport()
    c = client(transport, embed=fake_embed, vector_dimensions=3)
    c.create_index()
    assert c.upsert([PRICE, LAW]) == 2
    (m1, u1, h1, b1), (m2, u2, _h2, b2) = transport.calls
    assert (m1, u1) == (
        "PUT",
        f"https://srch.example.net/indexes/evidence?api-version={az.API_VERSION}",
    )
    assert h1["api-key"] == "test-key" and b1["name"] == "evidence"
    assert (m2, u2) == (
        "POST",
        f"https://srch.example.net/indexes/evidence/docs/index?api-version={az.API_VERSION}",
    )
    assert [d["evidenceId"] for d in b2["value"]] == [PRICE.id, LAW.id]
    assert b2["value"][0][az.VECTOR_FIELD] == fake_embed([az.embedding_text(PRICE)])[0]


def test_search_sends_filter_and_drops_leaked_hits():
    body = {
        "value": [
            hit(LAW, **{"@search.score": 1.2, "@search.rerankerScore": 3.4}),
            hit(SECRET, **{"@search.score": 9.9}),  # 서버 필터가 뚫렸다고 가정
            hit(PRICE, **{"@search.score": 0.7}),
        ]
    }
    transport = FakeTransport([(200, json.dumps(body).encode())])
    results = client(transport, embed=fake_embed).search("허가 품목", org_id="org_hanbit", top=3)
    assert [r["evidence"]["id"] for r in results] == [LAW.id, PRICE.id]
    assert [r["score"] for r in results] == [3.4, 0.7]
    sent = transport.calls[0][3]
    assert "org_hanbit" in sent["filter"] and sent["top"] == 3
    assert sent["vectorQueries"][0]["k"] == 3


def test_search_without_org_returns_public_only():
    body = {"value": [hit(LAW, **{"@search.score": 1.0}), hit(PRICE, **{"@search.score": 1.0})]}
    transport = FakeTransport([(200, json.dumps(body).encode())])
    results = client(transport).search("허가", org_id=None)
    assert [r["evidence"]["id"] for r in results] == [LAW.id]
    assert "vectorQueries" not in transport.calls[0][3]


def test_http_error_raises_without_leaking_key():
    transport = FakeTransport([(403, b'{"error": {"message": "Forbidden"}}')])
    with pytest.raises(az.AzureSearchError) as info:
        client(transport).search("x", org_id="org_hanbit")
    assert info.value.status == 403
    assert "test-key" not in str(info.value)
