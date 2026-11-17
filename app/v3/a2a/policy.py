"""대화 정책: 질문 정책, 대화 개설 조건, 배출 기업 사이의 정보 차단."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.v3.a2a.message import Message
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound

UNKNOWN = "unknown"  # "모름". 유효한 답이다


def question_key(question: dict[str, Any]) -> tuple[str, str]:
    return (str(question.get("streamId") or ""), str(question.get("field") or ""))


@dataclass(frozen=True)
class QuestionPolicy:
    """배출 기업 담당자에게 하는 질문은 묶음당 최대 3개, 진단당 최대 2묶음이다.

    history는 이미 담당자에게 보여 준 질문 목록이다(계약 7장 questions 모양에 answer를 더한 것).
    """

    max_per_batch: int = 3
    max_batches: int = 2

    def batches_used(self, history: list[dict[str, Any]]) -> int:
        return len({int(q.get("batch", 1)) for q in history})

    def has_open_batch(self, history: list[dict[str, Any]]) -> bool:
        return any(not q.get("answered") for q in history)

    def can_ask(self, history: list[dict[str, Any]]) -> bool:
        """새 묶음을 낼 수 있는가. 답을 기다리는 묶음이 있으면 새로 내지 않는다."""
        return self.batches_used(history) < self.max_batches and not self.has_open_batch(history)

    def already_asked(self, question: dict[str, Any], history: list[dict[str, Any]]) -> bool:
        key = question_key(question)
        return any(question_key(q) == key for q in history)

    def next_batch(
        self,
        pending: list[dict[str, Any]],
        history: list[dict[str, Any]],
        *,
        new_info: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    ) -> list[dict[str, Any]]:
        """다음에 보여 줄 질문 묶음. 낼 수 없거나 낼 것이 없으면 빈 목록이다.

        이미 물은 항목은 답이 "모름"이어도 다시 묻지 않는다. 새 정보가 생긴 항목만
        new_info에 (streamId, field)로 넣어 다시 물을 수 있다. 개수를 채우려고 질문을 보태지 않는다.
        """
        if not self.can_ask(history):
            return []
        reask = set(new_info)
        chosen: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for question in pending:
            key = question_key(question)
            if key in seen:
                continue
            if self.already_asked(question, history) and key not in reask:
                continue
            seen.add(key)
            chosen.append(question)
            if len(chosen) == self.max_per_batch:
                break
        batch = self.batches_used(history) + 1
        return [{**q, "batch": batch, "answered": False} for q in chosen]

    def record_answer(self, question: dict[str, Any], value: Any) -> dict[str, Any]:
        """답을 적는다. 빈 답은 받지 않고, "모름"은 유효한 답으로 적는다."""
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("빈 답은 적을 수 없다. 모르면 'unknown'을 보낸다")
        unknown = isinstance(value, str) and value.strip().lower() in (UNKNOWN, "모름")
        return {
            **question,
            "answered": True,
            "answer": UNKNOWN if unknown else value,
            "unknown": unknown,
        }

    def unresolved(
        self, pending: list[dict[str, Any]], history: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """더 물을 수 없어 "확인 필요"로 남길 항목: 모름으로 답했거나, 한도 때문에 묻지 못한 것."""
        left = [q for q in history if q.get("unknown")]
        if self.batches_used(history) >= self.max_batches:
            left += [q for q in pending if not self.already_asked(q, history)]
        return left


# ── 대화 개설 ────────────────────────────────────────────────────────────


def permit_evidence(permit_result: dict[str, Any] | None) -> list[str]:
    """허가 품목 대조 결과에 딸린 근거 ID."""
    if not permit_result:
        return []
    ids: list[str] = []
    for check in permit_result.get("checks") or []:
        ids += list(check.get("evidence") or [])
    for claim in permit_result.get("claims") or []:
        ids += list(claim.get("evidence") or [])
    output = permit_result.get("output") or {}
    # match_permit은 허가증 근거를 output.permitEvidence에 담는다
    ids += list(output.get("permitEvidence") or [])
    ids += list(output.get("evidence") or [])
    return list(dict.fromkeys(ids))


def can_open_thread(permit_result: dict[str, Any] | None) -> tuple[bool, str]:
    """허가 품목이 맞을 때만 대화를 연다. match_permit 도구의 결과를 받는다.

    match_permit의 실제 결과 모양: output.matched(True | False | None), output.openThread,
    output.permitEvidence, checks[0]은 key "permit"에 status met | unmet | conditional,
    허가 품목이 올라와 있지 않으면 status가 needs_information이다.

    허가 품목을 확인하지 못한 경우에도 열지 않는다. 연 뒤에 불일치를 알게 되면
    호출자는 근거와 함께 decline으로 닫는다.
    """
    if not permit_result:
        return False, "허가 품목 대조 결과가 없어 대화를 열지 않습니다."
    output = permit_result.get("output") or {}
    checks = permit_result.get("checks") or []
    unmet = [c for c in checks if c.get("status") == "unmet"]
    matched = output.get("matched")
    if output.get("openThread") is False and matched is None and not unmet:
        return False, "업체의 허가 품목을 확인하지 못해 대화를 열지 않습니다."
    if matched is False or unmet:
        detail = unmet[0].get("text") if unmet else output.get("reason")
        reason = "업체의 허가 품목에 이 폐기물이 없어 대화를 열지 않습니다."
        return False, f"{reason} {detail}".strip() if detail else reason
    if permit_result.get("status") == "needs_information" or (matched is None and not checks):
        return False, "업체의 허가 품목을 확인하지 못해 대화를 열지 않습니다."
    return True, "허가 품목이 폐기물과 맞습니다."


# ── 배출 기업 사이의 정보 차단 ───────────────────────────────────────────

_OWNED_KINDS = ("doc", "measure", "human")
_COMPANY_MARKS = re.compile(r"\(주\)|㈜|주식회사|\s+")


def evidence_owner(evidence: Any) -> str | None:
    """근거를 가진 조직. meta.ownerOrgId가 우선이고, 없으면 ID의 조직 자리에서 읽는다."""
    owner = (evidence.meta or {}).get("ownerOrgId")
    if owner:
        return str(owner)
    parts = evidence.id.split(":")
    if evidence.kind in _OWNED_KINDS and len(parts) >= 2:
        return parts[1]
    return None


def _owners(evidence_id: str, registry: Any, depth: int = 4) -> set[str]:
    """근거와 그 계산 사슬(meta.inputEvidence)에 걸린 조직들."""
    try:
        evidence = registry.get(evidence_id, None)
    except (EvidenceNotFound, EvidenceForbidden):
        return set()
    owners = set()
    owner = evidence_owner(evidence)
    if owner:
        owners.add(owner)
    owners.update(str(o) for o in (evidence.meta or {}).get("orgIds") or [])
    if depth > 0:
        for child in (evidence.meta or {}).get("inputEvidence") or []:
            owners |= _owners(str(child), registry, depth - 1)
    return owners


def _bare(name: str) -> str:
    return _COMPANY_MARKS.sub("", name)


def check_disclosure(message: Message, registry: Any, store: Any) -> list[dict[str, Any]]:
    """업체가 한 배출 기업에게 보내는 메시지에 다른 배출 기업의 이름이나 물량이 없는지 본다.

    문제가 없으면 빈 목록. 문제는 {"code", "message", "orgId", ...} 꼴이다.
    업체는 여러 배출 기업의 물량을 자기 노선에 묶을 수 있지만, 그 사실을 상대에게 알릴 때
    다른 배출 기업을 드러내면 안 된다.
    """
    emitters = {org["id"]: org for org in store.list_orgs(role="emitter")}
    if message.recipient not in emitters or message.sender in emitters:
        return []
    others = {oid: org for oid, org in emitters.items() if oid != message.recipient}
    problems: list[dict[str, Any]] = []

    # 본문과, 본문 밖에 실려 화면으로 나가는 문장(닫는 이유, 받지 않는 이유, 조건 문장)을 함께 본다
    text = "\n".join([message.text or "", *[found for _, found in message.free_texts()]])
    bare_text = _bare(text)
    for oid, org in others.items():
        name = str(org.get("name") or "")
        if oid in text or (name and (name in text or _bare(name) in bare_text)):
            problems.append(
                {
                    "code": "OTHER_EMITTER_NAMED",
                    "message": "다른 배출 기업의 이름이 본문에 들어 있습니다.",
                    "orgId": oid,
                }
            )
        for stream in store.list_streams(oid):
            if stream["id"] in text or stream["id"] in message.stream_ids:
                problems.append(
                    {
                        "code": "OTHER_EMITTER_STREAM",
                        "message": "다른 배출 기업의 폐기물이 메시지에 들어 있습니다.",
                        "orgId": oid,
                        "streamId": stream["id"],
                    }
                )

    for claim in message.claims:
        for evidence_id in claim.evidence:
            for owner in sorted(_owners(evidence_id, registry) & set(others)):
                problems.append(
                    {
                        "code": "OTHER_EMITTER_EVIDENCE",
                        "message": "다른 배출 기업의 근거를 인용했습니다.",
                        "orgId": owner,
                        "claimId": claim.id,
                        "evidenceId": evidence_id,
                    }
                )
    return problems
