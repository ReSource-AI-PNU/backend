"""Azure AI Search 어댑터 (하이브리드 + 시맨틱 랭커).

HTTP는 urllib로 하고, transport와 embed를 주입받는다.
- transport(method, url, headers, body) -> (status, body_bytes)
- embed(texts) -> 벡터 목록 (색인과 질의에 같은 모델을 쓴다. 설계는 Ollama bge-m3, 1024차원)
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from app.v3.evidence.model import PUBLIC, Evidence

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]
Embed = Callable[[list[str]], list[list[float]]]

API_VERSION = "2024-07-01"
VECTOR_FIELD = "contentVector"
SEMANTIC_CONFIG = "rsai-semantic"
VECTOR_PROFILE = "rsai-hnsw-profile"
VECTOR_ALGORITHM = "rsai-hnsw"
KOREAN_ANALYZER = "ko.microsoft"
UPSERT_BATCH = 500
SELECT_FIELDS = "id,evidenceId,kind,access,payload"


class AzureSearchError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Azure AI Search 호출 실패 (HTTP {status}): {body[:300]}")
        self.status = status
        self.body = body


def doc_key(evidence_id: str) -> str:
    """문서 키. Azure 키에는 영문, 숫자, _, -, = 만 쓸 수 있어 근거 ID를 URL-safe base64로 바꾼다."""
    return base64.urlsafe_b64encode(evidence_id.encode("utf-8")).decode("ascii")


def index_definition(
    name: str, *, vector_dimensions: int = 1024, semantic_config: str = SEMANTIC_CONFIG
) -> dict[str, Any]:
    def text(field_name: str) -> dict[str, Any]:
        return {
            "name": field_name,
            "type": "Edm.String",
            "searchable": True,
            "analyzer": KOREAN_ANALYZER,
        }

    def plain(field_name: str, edm: str = "Edm.String", **flags: bool) -> dict[str, Any]:
        return {"name": field_name, "type": edm, "searchable": False, **flags}

    return {
        "name": name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "searchable": False},
            plain("evidenceId", filterable=True),
            plain("kind", filterable=True, facetable=True),
            text("label"),
            text("excerpt"),
            text("source"),
            plain("asOf", filterable=True, sortable=True),
            plain("validUntil", filterable=True),
            plain("access", "Collection(Edm.String)", filterable=True),
            plain("unit"),
            plain("version", "Edm.Int32"),
            plain("payload"),
            {
                "name": VECTOR_FIELD,
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": vector_dimensions,
                "vectorSearchProfile": VECTOR_PROFILE,
            },
        ],
        "vectorSearch": {
            "algorithms": [{"name": VECTOR_ALGORITHM, "kind": "hnsw"}],
            "profiles": [{"name": VECTOR_PROFILE, "algorithm": VECTOR_ALGORITHM}],
        },
        "semantic": {
            "configurations": [
                {
                    "name": semantic_config,
                    "prioritizedFields": {
                        "titleField": {"fieldName": "label"},
                        "prioritizedContentFields": [{"fieldName": "excerpt"}],
                        "prioritizedKeywordsFields": [{"fieldName": "source"}],
                    },
                }
            ]
        },
    }


def embedding_text(evidence: Evidence) -> str:
    return "\n".join(part for part in (evidence.label, evidence.excerpt, evidence.source) if part)


def to_document(evidence: Evidence, vector: Sequence[float] | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "@search.action": "mergeOrUpload",
        "id": doc_key(evidence.id),
        "evidenceId": evidence.id,
        "kind": evidence.kind,
        "label": evidence.label,
        "excerpt": evidence.excerpt,
        "source": evidence.source,
        "asOf": evidence.as_of,
        "validUntil": evidence.valid_until,
        "access": list(evidence.access),
        "unit": evidence.unit,
        "version": evidence.version,
        "payload": json.dumps(evidence.to_dict(), ensure_ascii=False, sort_keys=True),
    }
    if vector is not None:
        document[VECTOR_FIELD] = [float(x) for x in vector]
    return document


def upsert_payload(
    evidences: Sequence[Evidence], vectors: Sequence[Sequence[float]] | None = None
) -> dict[str, Any]:
    if vectors is not None and len(vectors) != len(evidences):
        raise ValueError("근거 수와 벡터 수가 다르다")
    return {
        "value": [
            to_document(e, None if vectors is None else vectors[i]) for i, e in enumerate(evidences)
        ]
    }


def org_filter(org_id: str | None) -> str:
    """access에 "*" 또는 org_id가 있는 문서만. org_id가 None이면 공개 근거만 통과시킨다."""
    if org_id is None:
        return f"access/any(a: a eq '{PUBLIC}')"
    return f"access/any(a: a eq '{PUBLIC}' or a eq '{_quote(org_id)}')"


def query_payload(
    query: str,
    *,
    org_id: str | None,
    kinds: Iterable[str] | None = None,
    top: int = 10,
    vector: Sequence[float] | None = None,
    semantic: bool = True,
    semantic_config: str = SEMANTIC_CONFIG,
) -> dict[str, Any]:
    filters = [org_filter(org_id)]
    if kinds is not None:
        joined = ",".join(sorted({_quote(k) for k in kinds}))
        filters.append(f"search.in(kind, '{joined}', ',')")
    payload: dict[str, Any] = {
        "search": query,
        "filter": " and ".join(f"({f})" for f in filters),
        "top": top,
        "select": SELECT_FIELDS,
    }
    if semantic:
        payload["queryType"] = "semantic"
        payload["semanticConfiguration"] = semantic_config
    if vector is not None:
        payload["vectorQueries"] = [
            {
                "kind": "vector",
                "vector": [float(x) for x in vector],
                "fields": VECTOR_FIELD,
                "k": top,
            }
        ]
    return payload


def from_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """검색 결과 한 건을 {"evidence", "score"}로 바꾼다. 시맨틱 점수가 있으면 그것을 쓴다."""
    score = hit.get("@search.rerankerScore")
    if score is None:
        score = hit.get("@search.score", 0.0)
    return {"evidence": json.loads(hit["payload"]), "score": float(score)}


class AzureSearchClient:
    def __init__(
        self,
        endpoint: str,
        index: str,
        api_key: str,
        *,
        transport: Transport | None = None,
        embed: Embed | None = None,
        api_version: str = API_VERSION,
        vector_dimensions: int = 1024,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.index = index
        self._api_key = api_key
        self._transport = transport or urllib_transport
        self._embed = embed
        self.api_version = api_version
        self.vector_dimensions = vector_dimensions

    def create_index(self) -> dict[str, Any]:
        body = index_definition(self.index, vector_dimensions=self.vector_dimensions)
        return self._call("PUT", f"/indexes/{self.index}", body)

    def upsert(self, evidences: Sequence[Evidence]) -> int:
        """근거를 색인에 올린다. embed가 있으면 벡터도 함께 넣는다. 올린 문서 수를 돌려준다."""
        total = 0
        for start in range(0, len(evidences), UPSERT_BATCH):
            batch = list(evidences[start : start + UPSERT_BATCH])
            vectors = self._embed([embedding_text(e) for e in batch]) if self._embed else None
            self._call("POST", f"/indexes/{self.index}/docs/index", upsert_payload(batch, vectors))
            total += len(batch)
        return total

    def search(
        self,
        query: str,
        *,
        org_id: str | None,
        kinds: Iterable[str] | None = None,
        top: int = 10,
        semantic: bool = True,
    ) -> list[dict[str, Any]]:
        vector = self._embed([query])[0] if self._embed and query.strip() else None
        payload = query_payload(
            query, org_id=org_id, kinds=kinds, top=top, vector=vector, semantic=semantic
        )
        response = self._call("POST", f"/indexes/{self.index}/docs/search", payload)
        allowed = {PUBLIC} if org_id is None else {PUBLIC, org_id}
        results = []
        for hit in response.get("value", []):
            # 서버 필터가 잘못돼도 다른 조직의 근거가 새지 않도록 한 번 더 거른다.
            if allowed.isdisjoint(hit.get("access") or ()):
                continue
            results.append(from_hit(hit))
        return results

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        url = f"{self.endpoint}{path}?api-version={self.api_version}"
        headers = {"Content-Type": "application/json", "api-key": self._api_key}
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, raw = self._transport(method, url, headers, data)
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if status >= 300:
            raise AzureSearchError(status, text)
        return json.loads(text) if text.strip() else {}


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _quote(value: str) -> str:
    return value.replace("'", "''")
