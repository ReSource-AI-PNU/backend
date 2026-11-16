"""A2A 스레드. 회사 측 에이전트 하나와 업체 측 에이전트 하나의 대화 하나를 담는다.

규칙
- 대화는 배출 기업의 inquiry로 시작한다.
- 메시지 하나가 한 턴이다. max_turns(기본 12)를 넘기려 하면 TurnLimitReached를 내고,
  호출자는 handoff를 보낸다. handoff와 confirm은 상한과 관계없이 보낼 수 있다
  (confirm은 사람 승인 뒤의 확정 통지라 협상 턴으로 세지 않는다).
- decline, handoff, confirm 뒤에는 스레드가 닫힌다.
- confirm은 양쪽 담당자가 승인한 뒤에만 보낼 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.v3.a2a.message import Message

THREAD_STATUSES = ("not_opened", "open", "agreed", "declined", "handoff", "confirmed")
CLOSED_STATUSES = ("declined", "handoff", "confirmed")
SIDES = ("emitter", "vendor")
DEFAULT_MAX_TURNS = 12
UNCOUNTED_TYPES = ("handoff", "confirm")

# 상태별로 누가 어떤 메시지를 보낼 수 있는지
_ALLOWED: dict[str, dict[str, tuple[str, ...]]] = {
    "not_opened": {"emitter": ("inquiry", "handoff"), "vendor": ()},
    "open": {
        "emitter": ("inquiry", "question", "answer", "counter", "decline", "handoff"),
        "vendor": ("question", "answer", "offer", "counter", "conditional", "decline", "handoff"),
    },
    "agreed": {
        "emitter": ("counter", "decline", "handoff", "confirm"),
        "vendor": ("counter", "decline", "handoff", "confirm"),
    },
}
_TERMS_TYPES = ("offer", "counter", "conditional")


class ThreadError(ValueError):
    """스레드 규칙 위반."""


class InvalidTransition(ThreadError):
    """지금 상태에서 보낼 수 없는 메시지다."""


class NotParty(ThreadError):
    """보내는 쪽이나 받는 쪽이 이 스레드의 당사자가 아니다."""


class TurnLimitReached(ThreadError):
    """턴 상한에 닿았다. 호출자는 handoff를 보내야 한다."""


class ApprovalRequired(InvalidTransition):
    """양쪽 담당자가 승인하기 전에는 confirm을 보낼 수 없다."""


@dataclass
class Thread:
    id: str
    session_id: str
    emitter_org: str
    vendor_org: str
    stream_ids: tuple[str, ...] = ()
    status: str = "not_opened"
    status_reason: str | None = None
    reason_evidence: tuple[str, ...] = ()
    messages: list[Message] = field(default_factory=list)
    max_turns: int = DEFAULT_MAX_TURNS
    approvals: dict[str, bool] = field(default_factory=lambda: {"emitter": False, "vendor": False})
    agreed_seq: int | None = None  # 합의한 조건이 담긴 메시지의 seq

    def __post_init__(self) -> None:
        if self.status not in THREAD_STATUSES:
            raise ThreadError(f"알 수 없는 스레드 상태: {self.status}")
        if self.emitter_org == self.vendor_org:
            raise ThreadError("배출 기업과 업체가 같을 수 없다")
        self.stream_ids = tuple(self.stream_ids)
        self.reason_evidence = tuple(self.reason_evidence)
        self.approvals = {side: bool(self.approvals.get(side, False)) for side in SIDES}

    # ── 조회 ──────────────────────────────────────────────────────────
    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if m.type not in UNCOUNTED_TYPES)

    @property
    def closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def blocked(self) -> bool:
        """허가 품목 불일치 같은 이유로 열지 않기로 한 스레드."""
        return self.status == "not_opened" and bool(self.status_reason)

    @property
    def turn_limit_reached(self) -> bool:
        return self.turns >= self.max_turns

    def side_of(self, org_id: str) -> str:
        if org_id == self.emitter_org:
            return "emitter"
        if org_id == self.vendor_org:
            return "vendor"
        raise NotParty(f"{org_id}는 스레드 {self.id}의 당사자가 아니다")

    def counterpart(self, org_id: str) -> str:
        return self.vendor_org if self.side_of(org_id) == "emitter" else self.emitter_org

    def last_terms_message(self) -> Message | None:
        for message in reversed(self.messages):
            if message.type in _TERMS_TYPES:
                return message
        return None

    def allowed_types(self, org_id: str) -> tuple[str, ...]:
        """지금 org_id가 보낼 수 있는 메시지 종류 (턴 상한과 승인 조건까지 반영)."""
        side = self.side_of(org_id)
        if self.blocked:
            return ()
        allowed = _ALLOWED.get(self.status, {}).get(side, ())
        if self.turn_limit_reached:
            allowed = tuple(t for t in allowed if t in UNCOUNTED_TYPES)
        if not all(self.approvals.values()):
            allowed = tuple(t for t in allowed if t != "confirm")
        return allowed

    # ── 변경 ──────────────────────────────────────────────────────────
    def refuse_open(self, reason: str, evidence: tuple[str, ...] | list[str] = ()) -> None:
        """대화를 열지 않는다(예: 허가 품목 불일치). 이유와 근거를 남긴다."""
        if self.status != "not_opened" or self.messages:
            raise InvalidTransition("이미 열린 스레드는 decline으로 닫아야 한다")
        self.status_reason = reason
        self.reason_evidence = tuple(evidence)

    def check(self, message: Message) -> None:
        """append가 받아들일지 미리 확인한다. 안 되면 예외를 낸다."""
        if message.thread_id != self.id:
            raise ThreadError(f"메시지의 threadId({message.thread_id})가 스레드와 다르다")
        side = self.side_of(message.sender)
        if message.recipient != self.counterpart(message.sender):
            raise NotParty(f"받는 쪽 {message.recipient}가 이 스레드의 상대가 아니다")
        if self.closed:
            raise InvalidTransition(f"닫힌 스레드({self.status})에는 메시지를 보낼 수 없다")
        if self.blocked:
            raise InvalidTransition(f"열지 않기로 한 스레드다: {self.status_reason}")
        allowed = _ALLOWED[self.status][side]
        if message.type not in allowed:
            raise InvalidTransition(
                f"{self.status} 상태에서 {side} 쪽은 {message.type}를 보낼 수 없다"
            )
        if message.type == "confirm" and not all(self.approvals.values()):
            raise ApprovalRequired("양쪽 담당자가 승인한 뒤에만 confirm을 보낼 수 있다")
        if message.type not in UNCOUNTED_TYPES and self.turn_limit_reached:
            raise TurnLimitReached(
                f"스레드 {self.id}가 {self.max_turns}턴에 닿았다. handoff로 넘겨야 한다"
            )
        unknown = [s for s in message.stream_ids if s not in self.stream_ids]
        if unknown:
            raise ThreadError(f"이 스레드에 없는 흐름이다: {', '.join(unknown)}")

    def append(self, message: Message, *, now: str = "") -> Message:
        """규칙을 확인하고 메시지를 붙인다. seq와 created_at을 채운 메시지를 돌려준다."""
        self.check(message)
        message.seq = len(self.messages) + 1
        if now and not message.created_at:
            message.created_at = now
        self.messages.append(message)
        if message.type == "inquiry" and self.status == "not_opened":
            self.status = "open"
        elif message.type == "decline":
            self._close("declined", message)
        elif message.type == "handoff":
            self._close("handoff", message)
        elif message.type == "confirm":
            self.status = "confirmed"
        elif message.type == "counter" and self.status == "agreed":
            # 합의 뒤에 조건을 다시 내면 합의와 승인을 되돌린다
            self.status = "open"
            self.agreed_seq = None
            self.approvals = {side: False for side in SIDES}
        return message

    def _close(self, status: str, message: Message) -> None:
        self.status = status
        self.status_reason = str(message.meta.get("reason") or message.text)
        evidence = [e for claim in message.claims for e in claim.evidence]
        evidence += [e for e in message.meta.get("reasonEvidence", []) if e not in evidence]
        self.reason_evidence = tuple(dict.fromkeys(evidence))
        self.approvals = {side: False for side in SIDES}

    def mark_agreed(self) -> Message:
        """가장 최근 조건(offer, counter, conditional)에 양쪽 에이전트가 동의했다고 적는다."""
        if self.status != "open":
            raise InvalidTransition(f"{self.status} 상태에서는 합의로 넘어갈 수 없다")
        terms = self.last_terms_message()
        if terms is None:
            raise InvalidTransition("합의할 조건(offer, counter, conditional)이 아직 없다")
        self.status = "agreed"
        self.agreed_seq = terms.seq
        return terms

    def approve(self, org_id: str, value: bool = True) -> bool:
        """담당자 승인을 적는다. 양쪽이 모두 승인했으면 True를 돌려준다."""
        side = self.side_of(org_id)
        if self.status != "agreed":
            raise InvalidTransition("합의된 스레드만 승인할 수 있다")
        self.approvals[side] = bool(value)
        return all(self.approvals.values())

    # ── 직렬화 ────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        """계약 7장의 threads 항목 모양. vendorName, vendorModel은 뷰를 만드는 쪽이 붙인다."""
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "emitterOrgId": self.emitter_org,
            "vendorOrgId": self.vendor_org,
            "status": self.status,
            "statusReason": self.status_reason,
            "reasonEvidence": list(self.reason_evidence),
            "streamIds": list(self.stream_ids),
            "turns": self.turns,
            "maxTurns": self.max_turns,
            "approvals": dict(self.approvals),
            "agreedSeq": self.agreed_seq,
            "messages": [self._message_dict(m) for m in self.messages],
        }

    def _message_dict(self, message: Message) -> dict[str, Any]:
        data = message.to_dict()
        data["id"] = f"{self.id}_m{message.seq}"
        data["validation"] = message.meta.get("validation") or {
            "ok": None,
            "attempts": 0,
            "rejections": [],
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thread:
        thread = cls(
            id=str(data["id"]),
            session_id=str(data.get("sessionId", "")),
            emitter_org=str(data["emitterOrgId"]),
            vendor_org=str(data["vendorOrgId"]),
            stream_ids=tuple(data.get("streamIds") or ()),
            status=str(data.get("status", "not_opened")),
            status_reason=data.get("statusReason"),
            reason_evidence=tuple(data.get("reasonEvidence") or ()),
            max_turns=int(data.get("maxTurns", DEFAULT_MAX_TURNS)),
            approvals=dict(data.get("approvals") or {}),
            agreed_seq=data.get("agreedSeq"),
        )
        for raw in data.get("messages") or ():
            thread.messages.append(Message.from_dict({**raw, "threadId": thread.id}))
        return thread
