"""claim 풀. 이번 턴에 쓸 수 있는 claim만 담고, 모델 출력의 claimRefs를 claims로 바꾼다.

모델은 값을 직접 적지 않고 풀의 ID만 고른다. 풀에는 근거가 붙어 있고 보내는 조직이 그 근거를
볼 수 있는 값만 들어간다. 풀에 없는 ID를 쓰면 UnknownClaimRef로 거부한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.v3 import names
from app.v3.a2a.message import Claim, Message
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.evidence.units import UnitError, same_value
from app.v3.textutil import josa
from app.v3.untrusted import stream_label

_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")
# 배출 기업의 현행 처리 단가에서 나온 값. 업체와 나누는 대화의 풀에는 넣지 않는다.
# 배출 기업의 현행 처리(단가, 방법)에서 나온 값. 상대 조직에 인용하지 않는다. 탄소 변화와 그 환산액은
# 현행 처리 방법(입력이 없으면 혼합 소각 가정)에 견준 값이라 여기에 든다
PRIVATE_KEYS = (
    "currentUnitCost",
    "currentUnitPrice",
    "monthlyNow",
    "difference",
    "carbonDifference",
    "kauValue",
    "transportCarbon",
    "carbonTotal",
)
CRITERION_FIELDS = (
    ("unitPrice", "unitPriceKrwPerKg", "원/kg", "단가"),
    ("minMonthlyKg", "minMonthlyKg", "kg/월", "최소 물량"),
    ("maxContaminationPct", "maxContaminationPct", "%", "오염도 상한"),
    ("maxMoisturePct", "maxMoisturePct", "%", "수분 상한"),
)
FLEET_FIELDS = (
    ("capacityKg", "kg", "차량 적재 중량"),
    ("capacityM3", "m3", "차량 적재 부피"),
    ("costPerKm", "원/km", "km당 운송비"),
)


class UnknownClaimRef(ValueError):
    """모델이 풀에 없는 claim ID를 썼다."""

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = list(missing)
        super().__init__(f"풀에 없는 claim ID: {', '.join(self.missing)}")

    def rejection(self) -> dict[str, Any]:
        return {
            "code": "UNKNOWN_CLAIM_REF",
            "message": (
                f"claimRefs의 {josa(', '.join(self.missing), '은는')} claim 풀에 없습니다. "
                "풀(pool)에 있는 ID만 쓰고, 필요한 값이 없으면 도구를 부르거나 '확인 필요'로 두세요."
            ),
            "claimId": self.missing[0] if self.missing else None,
            "detail": {"missing": self.missing},
        }


def safe_id(*parts: Any) -> str:
    """자리표시자에 쓸 수 있는 ID(영문으로 시작, 영문·숫자·밑줄)."""
    text = "_".join(str(p) for p in parts if p not in (None, ""))
    text = _UNSAFE.sub("_", text).strip("_")
    if not text or not text[0].isalpha():
        text = f"c_{text}" if text else "c"
    return text


def is_private(key: str) -> bool:
    return key in PRIVATE_KEYS or key.startswith("sensitivity")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class PoolEntry:
    claim: Claim
    label: str = ""
    source: str = ""  # stream | profile | tool | message
    tags: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {**self.claim.to_dict(), "label": self.label, "source": self.source, **self.tags}

    def brief(self) -> dict[str, Any]:
        """모델에게 보여 주는 모양. 근거 ID와 내부 표시는 빼고, 고를 때 필요한 것만 남긴다.

        근거 ID는 모델이 고를 일이 없고(코드가 claim에서 채운다) 날짜와 해시가 들어 있어 본문에
        숫자로 옮겨 적기 쉽다. name은 사람이 읽는 이름이다(흐름·기준 값은 풀의 설명, 도구와 메시지
        값은 코드가 키에서 정한 이름).
        """
        claim = self.claim
        name = self.label if self.source in ("stream", "profile") else claim.label or self.label
        out: dict[str, Any] = {"id": claim.id, "name": name, "value": claim.value}
        if claim.unit:
            out["unit"] = claim.unit
        if claim.kind != "number":
            out["kind"] = claim.kind
        if claim.role:
            out["role"] = claim.role
        for key in ("streamId", "streamIds", "criterionId", "tool", "seq"):
            if self.tags.get(key) is not None:
                out[key] = self.tags[key]
        out["source"] = self.source
        return out


class ClaimPool:
    def __init__(self, registry: Any, org_id: str) -> None:
        self.registry = registry
        self.org_id = org_id
        self._entries: dict[str, PoolEntry] = {}

    # ── 조회 ──────────────────────────────────────────────────────────
    def __contains__(self, claim_id: object) -> bool:
        return claim_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, claim_id: str) -> PoolEntry | None:
        return self._entries.get(claim_id)

    def entries(self) -> list[PoolEntry]:
        return list(self._entries.values())

    def find(self, **tags: Any) -> list[str]:
        """태그가 모두 맞는 claim ID. source도 태그처럼 쓸 수 있다."""
        found = []
        for claim_id, entry in self._entries.items():
            view = {"source": entry.source, **entry.tags}
            if all(view.get(k) == v for k, v in tags.items()):
                found.append(claim_id)
        return found

    def first(self, **tags: Any) -> str | None:
        found = self.find(**tags)
        return found[0] if found else None

    def describe(self) -> list[dict[str, Any]]:
        """모델에게 보여 주는 풀 목록(PoolEntry.brief)."""
        return [entry.brief() for entry in self._entries.values()]

    # ── 모델 출력 → claims ────────────────────────────────────────────
    def resolve(self, claim_refs: Iterable[Any]) -> tuple[Claim, ...]:
        """claimRefs를 claims로 바꾼다. 풀에 없는 ID가 하나라도 있으면 UnknownClaimRef."""
        refs = list(dict.fromkeys(str(ref) for ref in claim_refs or ()))
        missing = [ref for ref in refs if ref not in self._entries]
        if missing:
            raise UnknownClaimRef(missing)
        return tuple(self._entries[ref].claim for ref in refs)

    def claims_from_output(self, output: dict[str, Any]) -> tuple[Claim, ...]:
        refs = output.get("claimRefs")
        if refs is None:
            refs = []
        if not isinstance(refs, list):
            raise UnknownClaimRef([str(refs)])
        return self.resolve(refs)

    # ── 채우기 ────────────────────────────────────────────────────────
    def _visible(self, evidence_ids: Iterable[str] | None) -> list[str]:
        out: list[str] = []
        for evidence_id in evidence_ids or ():
            if evidence_id in out:
                continue
            try:
                self.registry.get(evidence_id, self.org_id)
            except (EvidenceNotFound, EvidenceForbidden):
                continue
            out.append(evidence_id)
        return out

    def add(
        self,
        claim_id: str,
        *,
        value: Any,
        evidence: Iterable[str] | None,
        kind: str = "number",
        unit: str | None = None,
        label: str = "",
        source: str = "",
        name: str | None = None,
        **tags: Any,
    ) -> str | None:
        """근거가 없거나 첫 근거(값의 출처)를 이 조직이 볼 수 없으면 넣지 않고 None을 돌려준다.

        name은 사람이 읽는 이름을 정할 키다(names.py). 없으면 field, key, claim ID 순으로 본다.
        """
        wanted = list(evidence or ())
        visible = self._visible(wanted)
        if value is None or not wanted or not visible or visible[0] != wanted[0]:
            return None
        # 사람이 읽는 이름과 구실은 코드가 키에서 정한다(상대가 보낸 이름은 쓰지 않는다)
        key = name or tags.get("field") or tags.get("key") or claim_id
        named, role = names.describe(key, value, kind)
        base = safe_id(claim_id)
        claim_id, number = base, 1
        while True:
            claim = Claim(claim_id, kind, value, tuple(visible), unit, label=named, role=role)
            old = self._entries.get(claim_id)
            if old is None or old.claim == claim:
                break
            number += 1
            claim_id = f"{base}_{number}"
        self._entries[claim_id] = PoolEntry(claim, label, source, dict(tags))
        return claim_id

    def add_fact(
        self,
        claim_id: str,
        value: Any,
        unit: str | None,
        evidence: Iterable[str] | None,
        **extra: Any,
    ) -> str | None:
        """근거 목록에서 값이 같은 근거를 찾아 첫 근거로 세운다. 없으면 넣지 않는다."""
        if not _is_number(value):
            return None
        visible = self._visible(evidence)
        primary = None
        for evidence_id in visible:
            found = self.registry.get(evidence_id, self.org_id)
            if not _is_number(found.value):
                continue
            try:
                if same_value(float(value), unit, float(found.value), found.unit):
                    primary = evidence_id
                    break
            except UnitError:
                continue
        if primary is None:
            return None
        ordered = [primary, *[e for e in visible if e != primary]]
        return self.add(claim_id, value=value, unit=unit, evidence=ordered, **extra)

    def add_stream(self, stream: dict[str, Any], *, private: bool = False) -> dict[str, str]:
        """흐름의 값(근거가 붙은 것). private이면 현행 처리 단가도 넣는다. {필드: claim ID}."""
        sid = stream["id"]
        # 풀의 이름은 모델 입력에 실린다. 담당자가 넣은 흐름 이름 대신 보이는 이름을 쓴다(untrusted)
        name = stream_label(stream)
        measure, photo = stream.get("measure") or {}, stream.get("photo") or {}
        answers = stream.get("answers") or {}
        answer_ev = answers.get("evidence") or []
        facts = [
            (
                "monthlyKg",
                stream.get("monthlyKg"),
                "kg/월",
                stream.get("monthlyKgEvidence"),
                "월 발생량",
            ),
            (
                "contaminationPct",
                measure.get("contaminationPct"),
                "%",
                measure.get("evidence"),
                "오염도(측정)",
            ),
            (
                "photoContaminationPct",
                photo.get("contaminationPct"),
                "%",
                photo.get("evidence"),
                "사진 추정 오염 비율",
            ),
            ("moisturePct", measure.get("moisturePct"), "%", measure.get("evidence"), "수분(측정)"),
        ]
        # 담당자가 질문에 답한 값은 측정값이 아니다. claim ID와 이름을 answered…로 따로 둔다
        # ("오염도(담당자 답)"). 돌려주는 사전의 키는 측정값과 같은 항목 이름이다
        if measure.get("moisturePct") is None:
            facts.append(
                (
                    "moisturePct",
                    answers.get("moisturePct"),
                    "%",
                    answer_ev,
                    "수분(담당자 답변)",
                    "answeredMoisturePct",
                )
            )
        if measure.get("contaminationPct") is None and photo.get("contaminationPct") is None:
            facts.append(
                (
                    "contaminationPct",
                    answers.get("contaminationPct"),
                    "%",
                    answer_ev,
                    "오염도(담당자 답변)",
                    "answeredContaminationPct",
                )
            )
        if stream.get("monthlyKg") is None:
            facts.append(("monthlyKg", answers.get("monthlyKg"), "kg/월", answer_ev, "월 발생량"))
        if private:
            current = stream.get("currentTreatment") or {}
            if current.get("sale"):  # 지금 팔고 있다: 받는 kg당 금액(+)
                facts.append(
                    (
                        "currentUnitPrice",
                        current.get("salePriceKrwPerKg"),
                        "원/kg",
                        current.get("evidence"),
                        "현행 판매 단가",
                    )
                )
            else:
                facts.append(
                    (
                        "currentUnitCost",
                        current.get("unitCostKrwPerKg"),
                        "원/kg",
                        current.get("evidence"),
                        "현행 처리 단가",
                    )
                )
        out: dict[str, str] = {}
        for field_name, value, unit, evidence, label, *key in facts:
            if field_name in out:
                continue
            claim_id = self.add_fact(
                f"{sid}_{key[0] if key else field_name}",
                value,
                unit,
                evidence,
                label=f"{name} {label}",
                source="stream",
                streamId=sid,
                field=field_name,
                name=key[0] if key else None,
            )
            if claim_id:
                out[field_name] = claim_id
        return out

    def add_vendor_profile(self, profile: dict[str, Any]) -> dict[str, dict[str, str]]:
        """업체 프로필의 값(매입 기준, 차량). {기준 ID: {필드: claim ID}}, 차량은 "fleet" 아래."""
        vendor = profile.get("orgId")
        out: dict[str, dict[str, str]] = {}
        for criterion in profile.get("criteria") or []:
            cid = str(criterion.get("id"))
            fields: dict[str, str] = {}
            for field_name, key, unit, label in CRITERION_FIELDS:
                claim_id = self.add_fact(
                    f"{cid}_{field_name}",
                    criterion.get(key),
                    unit,
                    criterion.get("evidence"),
                    label=f"매입 기준 {cid} {label}",
                    source="profile",
                    vendorOrgId=vendor,
                    criterionId=cid,
                    field=field_name,
                )
                if claim_id:
                    fields[field_name] = claim_id
            out[cid] = fields
        fleet = profile.get("fleet") or {}
        fleet_fields: dict[str, str] = {}
        for field_name, unit, label in FLEET_FIELDS:
            claim_id = self.add_fact(
                f"fleet_{field_name}",
                fleet.get(field_name),
                unit,
                fleet.get("evidence"),
                label=label,
                source="profile",
                vendorOrgId=vendor,
                field=field_name,
            )
            if claim_id:
                fleet_fields[field_name] = claim_id
        out["fleet"] = fleet_fields
        return out

    def add_tool_result(
        self, result: dict[str, Any], prefix: str, *, skip_private: bool = False, **tags: Any
    ) -> dict[str, str]:
        """도구가 돌려준 claims. {도구의 claim ID: 풀의 claim ID}."""
        out: dict[str, str] = {}
        for claim in result.get("claims") or []:
            key = str(claim["id"])
            if skip_private and is_private(key):
                continue
            claim_id = self.add(
                safe_id(prefix, key),
                kind=claim.get("kind", "number"),
                value=claim.get("value"),
                unit=claim.get("unit"),
                evidence=claim.get("evidence"),
                label=key,
                source="tool",
                tool=result.get("tool"),
                key=key,
                **tags,
            )
            if claim_id:
                out[key] = claim_id
        return out

    def add_message(self, message: Message | dict[str, Any]) -> dict[str, str]:
        """스레드에 이미 붙은 메시지의 claims. {메시지의 claim ID: 풀의 claim ID}."""
        data = message.to_dict() if isinstance(message, Message) else message
        seq = data.get("seq") or 0
        priced = {
            str(item.get("claimId")): item.get("streamId")
            for item in ((data.get("meta") or {}).get("terms") or {}).get("items") or []
        }
        out: dict[str, str] = {}
        for claim in data.get("claims") or []:
            key = str(claim["id"])
            tags: dict[str, Any] = {"seq": seq, "sender": data.get("from"), "key": key}
            if key in priced:
                tags.update(field="unitPrice", streamId=priced[key])
            claim_id = self.add(
                safe_id(f"m{seq}", key),
                kind=claim.get("kind", "number"),
                value=claim.get("value"),
                unit=claim.get("unit"),
                evidence=claim.get("evidence"),
                label=f"{seq}번 메시지의 {key}",
                source="message",
                **tags,
            )
            if claim_id:
                out[key] = claim_id
        return out
