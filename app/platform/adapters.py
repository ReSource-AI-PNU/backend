"""Explicit HTTP contracts for A/B/C services and A2A 0.3 JSON-RPC counterparties."""

import hashlib
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.schemas import CompanyRef, Negotiation, NegotiationRound, PickupTerms


class AdapterError(Exception):
    pass


def endpoint(url):
    parsed = urlparse(url)
    local = settings.app_env in {"local", "test"} and parsed.hostname in {"127.0.0.1", "localhost"}
    if (
        (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        or parsed.username
        or parsed.fragment
    ):
        raise AdapterError("외부 서비스 주소 설정을 확인하세요")
    if not parsed.hostname:
        raise AdapterError("외부 서비스 주소가 없습니다")
    return url


def call(url, payload, token="", key="", client=None, extra_headers=None):
    headers = {"Idempotency-Key": key, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra_headers or {})
    try:
        if client is None:
            with httpx.Client(
                timeout=settings.platform_adapter_timeout, follow_redirects=False
            ) as connection:
                return call(url, payload, token, key, connection, extra_headers)
        with client.stream("POST", endpoint(url), json=payload, headers=headers) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise AdapterError(f"외부 서비스 응답 오류({response.status_code})")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 2_000_000:
                    raise AdapterError("외부 서비스 응답 크기 초과")
            import json

            data = json.loads(content)
            if not isinstance(data, dict):
                raise AdapterError("외부 응답은 JSON 객체여야 합니다")
            return data
    except (httpx.HTTPError, ValueError) as exc:
        raise AdapterError("외부 서비스 연결 또는 응답 형식 오류") from exc


def stage(step, state, client=None):
    url = settings.platform_stage_endpoints.get(step)
    if not url:
        raise AdapterError(f"{step} 서비스 주소 설정이 필요합니다")
    key = f"{state.session.id}:{state.version}:{step}"
    return call(
        url,
        {
            "contractVersion": "resource-stream-v1",
            "step": step,
            "session": state.session.model_dump(mode="json", by_alias=True),
        },
        settings.platform_stage_token,
        key,
        client,
    )


def a2a_negotiate(bundle, req, peer, key, client=None):
    from app.platform.service import now, uid, within

    g, offered = req.guardrails, req.requested
    if not within(offered, g):
        raise AdapterError("최초 제안이 가드레일 밖입니다")
    n = Negotiation(
        id=uid("neg"),
        stream_id=bundle.stream_id,
        counterparty=CompanyRef(company_id=bundle.collector_id, name=bundle.collector_name),
        guardrails=g,
        requested=offered,
        rounds=[],
        status="failed",
        started_at=now(),
        note="최대 라운드 안에 합의하지 못했습니다. 사람 확인이 필요합니다.",
    )
    context_id, task_id = None, None
    try:
        while len(n.rounds) + 2 <= g.max_rounds:
            n.rounds.append(
                NegotiationRound(
                    by="ours",
                    at=now(),
                    terms=offered,
                    within=True,
                    message="수거 조건 견적 제안 · 계약 체결 아님",
                )
            )
            rpc_id = hashlib.sha256(f"{key}:{len(n.rounds)}".encode()).hexdigest()
            message = {
                "kind": "message",
                "role": "user",
                "messageId": rpc_id,
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "schema": "resource-pickup-v1",
                            "terms": offered.model_dump(by_alias=True),
                            "weightKg": bundle.confirmed_kg,
                            "binding": False,
                        },
                    }
                ],
            }
            if context_id:
                message["contextId"] = context_id
            if task_id:
                message["taskId"] = task_id
            envelope = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "message/send",
                "params": {"message": message, "configuration": {"blocking": True}},
            }
            result = call(peer["url"], envelope, peer.get("token", ""), rpc_id, client)
            if result.get("id") != rpc_id or result.get("jsonrpc") != "2.0" or "error" in result:
                raise AdapterError("A2A 응답 ID 또는 JSON-RPC 오류")
            reply = result.get("result", {})
            context_id = reply.get("contextId", context_id)
            parts = reply.get("parts", [])
            if reply.get("kind") == "task":
                status = reply.get("status", {})
                if status.get("state") not in {"completed", "input-required"}:
                    raise AdapterError("비동기 업체 작업이 완료되지 않았습니다. 다시 확인하세요")
                task_id = reply.get("id") if status["state"] == "input-required" else None
                parts = status.get("message", {}).get("parts", []) + [
                    p for a in reply.get("artifacts", []) for p in a.get("parts", [])
                ]
            candidates = [
                p["data"]
                for p in parts
                if p.get("kind") == "data"
                and p.get("data", {}).get("schema") == "resource-pickup-v1"
            ]
            if len(candidates) != 1:
                raise AdapterError("업체 수거 조건 응답이 없거나 중복되었습니다")
            data = candidates[0]
            terms = PickupTerms.model_validate(data["terms"])
            permitted = within(terms, g) and terms.per_month <= 31
            accepted = data.get("accepted") is True and permitted
            n.rounds.append(
                NegotiationRound(
                    by="theirs",
                    at=now(),
                    terms=terms,
                    within=permitted,
                    accept=accepted,
                    message="업체 에이전트의 견적 회신",
                )
            )
            if accepted:
                n.status, n.agreed, n.note = (
                    "agreed",
                    terms,
                    "업체 견적 합의안 · 사람 승인 전 미확정",
                )
                break
            if not permitted:
                n.note = "업체 조건이 가드레일 밖입니다. 사람 확인이 필요합니다."
                break
            offered = terms
    except (AdapterError, ValidationError, KeyError, TypeError, AttributeError) as exc:
        n.note = str(exc) if isinstance(exc, AdapterError) else "업체 수거 조건 형식 오류"
    return n
