"""근거 레코드. 화면, 대화, 문서의 모든 숫자와 조항은 이 레코드의 ID에 연결된다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KINDS = ("law", "ref", "doc", "measure", "model", "human", "calc", "assumption")
PUBLIC = "*"

KIND_LABELS = {
    "law": "법령",
    "ref": "기준값",
    "doc": "문서",
    "measure": "측정값",
    "model": "모델 판정",
    "human": "사람 확인",
    "calc": "계산",
    "assumption": "가정값",
}


@dataclass(frozen=True)
class Evidence:
    """근거 하나.

    value가 숫자면 unit과 함께 수치 근거가 되고, 조항 근거는 value 없이 excerpt(원문)를 쓴다.
    access는 이 근거를 볼 수 있는 조직 ID 목록이고 "*"는 모두에게 열려 있다는 뜻이다.
    """

    id: str
    kind: str
    excerpt: str
    source: str
    as_of: str  # 기준일 YYYY-MM-DD
    value: float | str | None = None
    unit: str | None = None
    label: str = ""
    valid_until: str | None = None  # 이 날짜가 지나면 만료 (YYYY-MM-DD)
    access: tuple[str, ...] = (PUBLIC,)
    version: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"알 수 없는 근거 종류: {self.kind}")
        if not self.id or not self.id.startswith(self.kind + ":"):
            raise ValueError(f"근거 ID는 '{self.kind}:'로 시작해야 한다: {self.id!r}")
        if not self.as_of:
            raise ValueError("기준일(as_of)이 비어 있다")
        if isinstance(self.value, bool):
            raise TypeError("근거 값에 bool을 쓸 수 없다")
        object.__setattr__(self, "access", tuple(self.access))

    def visible_to(self, org_id: str | None) -> bool:
        """org_id가 None이면 시스템 내부 호출로 보고 항상 볼 수 있다."""
        return org_id is None or PUBLIC in self.access or org_id in self.access

    def expired(self, today: str) -> bool:
        # ISO 날짜는 문자열 비교로 충분하다
        return bool(self.valid_until) and today > self.valid_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "kindLabel": KIND_LABELS[self.kind],
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "excerpt": self.excerpt,
            "source": self.source,
            "asOf": self.as_of,
            "validUntil": self.valid_until,
            "access": list(self.access),
            "version": self.version,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Evidence:
        return cls(
            id=data["id"],
            kind=data["kind"],
            excerpt=data.get("excerpt", ""),
            source=data.get("source", ""),
            as_of=data.get("asOf") or data.get("as_of") or "",
            value=data.get("value"),
            unit=data.get("unit"),
            label=data.get("label", ""),
            valid_until=data.get("validUntil") or data.get("valid_until"),
            access=tuple(data.get("access") or (PUBLIC,)),
            version=int(data.get("version", 1)),
            meta=dict(data.get("meta") or {}),
        )
