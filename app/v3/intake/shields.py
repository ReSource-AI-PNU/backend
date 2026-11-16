"""Content Safety Prompt Shields. 업로드 문서는 근거로만 쓰고 지시로 해석하지 않는다.

문서 안에서 지시문 주입이 탐지되면 그 문서를 쓰지 않고 담당자에게 넘긴다(handoff).
"""

from __future__ import annotations

import json
from typing import Any

from app.v3.intake._http import Transport, default_transport

API_VERSION = "2024-09-01"
MAX_DOCUMENT_CHARS = 10000
MAX_DOCUMENTS_PER_CALL = 5


class ShieldError(RuntimeError):
    """Prompt Shields가 설정돼 있는데 호출하지 못했다. 검사하지 못한 문서를 통과시키지 않는다."""


class IntakeBlocked(Exception):
    """문서에서 지시문 주입이 탐지됐다. 호출자는 이 문서를 쓰지 말고 handoff로 넘긴다."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("업로드한 문서에서 지시문 주입이 탐지돼 담당자 확인으로 넘깁니다.")
        self.result = result


class PromptShieldsClient:
    def __init__(self, endpoint: str = "", api_key: str = "", transport: Transport | None = None):
        self.endpoint = endpoint.strip().rstrip("/")
        self.api_key = api_key
        self.transport = transport or default_transport

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.api_key)

    def shield(self, documents: list[str], user_prompt: str = "") -> dict[str, Any]:
        url = f"{self.endpoint}/contentsafety/text:shieldPrompt?api-version={API_VERSION}"
        body = json.dumps(
            {"userPrompt": user_prompt, "documents": documents}, ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Ocp-Apim-Subscription-Key": self.api_key}
        try:
            status, _, raw = self.transport("POST", url, headers, body)
        except OSError as error:
            raise ShieldError(f"Prompt Shields에 연결하지 못했습니다: {error}") from error
        if status != 200:
            raise ShieldError(f"Prompt Shields가 {status}를 돌려줬습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
            analysis = payload["documentsAnalysis"]
            flags = [bool(item["attackDetected"]) for item in analysis]
        except (ValueError, KeyError, TypeError) as error:
            raise ShieldError("Prompt Shields 응답 형식이 잘못됐습니다.") from error
        if len(flags) != len(documents):
            raise ShieldError("Prompt Shields가 문서 수와 다른 수의 결과를 돌려줬습니다.")
        return {"documents": flags}


def _chunks(text: str) -> list[str]:
    return [text[i : i + MAX_DOCUMENT_CHARS] for i in range(0, len(text), MAX_DOCUMENT_CHARS)]


def check_document(text: str, client: PromptShieldsClient | None) -> dict[str, Any]:
    """문서 글 전체를 검사한다.

    클라이언트가 설정되지 않았으면 막지 않고 shieldUnavailable=True를 돌려준다.
    호출자는 이 표시를 화면과 실행 기록에 그대로 남겨, 검사하지 않은 문서임을 숨기지 않는다.
    설정돼 있는데 호출에 실패하면 ShieldError가 난다(검사 못 한 문서를 통과시키지 않는다).
    """
    chunks = _chunks(text or "")
    if client is None or not client.configured:
        return {
            "attackDetected": False,
            "shieldUnavailable": True,
            "checkedChunks": 0,
            "attackChunks": [],
            "note": "Prompt Shields가 설정되지 않아 주입 검사를 하지 않았습니다.",
        }
    attack: list[int] = []
    for start in range(0, len(chunks), MAX_DOCUMENTS_PER_CALL):
        batch = chunks[start : start + MAX_DOCUMENTS_PER_CALL]
        flags = client.shield(batch)["documents"]
        attack.extend(start + offset for offset, flagged in enumerate(flags) if flagged)
    return {
        "attackDetected": bool(attack),
        "shieldUnavailable": False,
        "checkedChunks": len(chunks),
        "attackChunks": attack,
        "note": (
            "문서에서 지시문 주입이 탐지됐습니다. 이 문서는 쓰지 않고 담당자에게 넘깁니다."
            if attack
            else "지시문 주입이 탐지되지 않았습니다."
        ),
    }


def ensure_safe(text: str, client: PromptShieldsClient | None) -> dict[str, Any]:
    """check_document와 같지만 탐지되면 IntakeBlocked를 낸다."""
    result = check_document(text, client)
    if result["attackDetected"]:
        raise IntakeBlocked(result)
    return result
