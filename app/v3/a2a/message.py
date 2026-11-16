"""A2A 메시지. 본문의 숫자와 조항 자리는 자리표시자 {{claimId}}로 두고, 값과 근거는 claims에 넣는다."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MESSAGE_TYPES = (
    "inquiry",
    "question",
    "answer",
    "offer",
    "counter",
    "conditional",
    "decline",
    "handoff",
    "confirm",
)
CLAIM_KINDS = ("number", "clause")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")

STAGES = {
    1: ("identify", "식별·분류"),
    2: ("feasibility", "가능성 판단"),
    3: ("regulation", "규정 검토"),
    4: ("matching", "업종·기업 탐색"),
    5: ("cost-carbon", "비용·탄소 비교"),
    6: ("proposal", "최적안 제안"),
}


@dataclass(frozen=True)
class Claim:
    """본문 자리표시자 하나에 들어갈 값. number면 value가 숫자, clause면 원문에서 그대로 따온 문자열이다."""

    id: str
    kind: str
    value: float | str
    evidence: tuple[str, ...]
    unit: str | None = None
    # 사람이 읽는 이름과 값의 구실(app/v3/names.py). 선택 필드이고 근거 검증기는 보지 않는다.
    # 같은 값인지 견줄 때도 보지 않는다(이름만 다른 claim은 같은 claim이다).
    label: str | None = field(default=None, compare=False)
    role: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in CLAIM_KINDS:
            raise ValueError(f"알 수 없는 claim 종류: {self.kind}")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "evidence": list(self.evidence),
        }
        if self.label:
            out["label"] = self.label
        if self.role:
            out["role"] = self.role
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Claim:
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            value=data["value"],
            unit=data.get("unit"),
            evidence=tuple(data.get("evidence") or ()),
            label=str(data["label"]) if data.get("label") else None,
            role=str(data["role"]) if data.get("role") else None,
        )


@dataclass
class Message:
    thread_id: str
    sender: str  # 보내는 조직 ID
    recipient: str  # 받는 조직 ID
    type: str
    stage: int  # 1~6
    text: str
    stream_ids: tuple[str, ...] = ()
    claims: tuple[Claim, ...] = ()
    seq: int = 0
    created_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in MESSAGE_TYPES:
            raise ValueError(f"알 수 없는 메시지 종류: {self.type}")
        if self.stage not in STAGES:
            raise ValueError(f"단계는 1~6이어야 한다: {self.stage}")
        self.stream_ids = tuple(self.stream_ids)
        self.claims = tuple(self.claims)

    def free_texts(self) -> list[tuple[str, str]]:
        """본문 밖에 실려 화면과 문서로 나가는 문장: (자리, 글).

        닫는 이유(meta.reason), 받지 않는 흐름의 이유(meta.excluded), 조건 문장과 요일
        (meta.terms.conditions, days)이다. 본문과 같은 규칙으로 검사한다(근거 검증기, 정보 차단).
        """
        meta = self.meta if isinstance(self.meta, dict) else {}
        out: list[tuple[str, str]] = []
        if meta.get("reason") is not None:
            out.append(("reason", str(meta["reason"])))
        excluded = meta.get("excluded")
        for item in excluded if isinstance(excluded, list) else [excluded] if excluded else []:
            values = item.values() if isinstance(item, dict) else [item]
            out += [("excluded", str(value)) for value in values if value is not None]
        terms = meta.get("terms")
        if isinstance(terms, dict):
            for key in ("conditions", "days"):
                found = terms.get(key)
                for value in found if isinstance(found, list) else [found] if found else []:
                    out.append((f"terms.{key}", str(value)))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "from": self.sender,
            "to": self.recipient,
            "type": self.type,
            "stage": self.stage,
            "streamIds": list(self.stream_ids),
            "text": self.text,
            "claims": [c.to_dict() for c in self.claims],
            "seq": self.seq,
            "createdAt": self.created_at,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            thread_id=str(data["threadId"]),
            sender=str(data["from"]),
            recipient=str(data["to"]),
            type=str(data["type"]),
            stage=int(data["stage"]),
            text=str(data.get("text", "")),
            stream_ids=tuple(data.get("streamIds") or ()),
            claims=tuple(Claim.from_dict(c) for c in data.get("claims") or ()),
            seq=int(data.get("seq", 0)),
            created_at=str(data.get("createdAt", "")),
            meta=dict(data.get("meta") or {}),
        )
