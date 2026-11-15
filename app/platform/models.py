import math
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas import (
    Contract,
    FlowStep,
    Guardrails,
    Negotiation,
    PickupTerms,
    Session,
    Stream,
    WasteItem,
)

Kg = Annotated[float, Field(gt=0, le=1_000_000, allow_inf_nan=False)]
Minute = Annotated[int, Field(ge=0, le=1440)]


class PickupWindow(Contract):
    start: Minute = 480
    end: Minute = 1080
    service_minutes: int = Field(default=15, ge=0, le=180)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("수거 종료 시간은 시작 시간 이후여야 합니다")
        return self


class MaterialSupply(Contract):
    item: WasteItem
    material: str = Field(min_length=1, max_length=30)
    form: Literal["film", "rigid", "paper", "other"]
    origin: Literal["PIR", "PCR"]
    monthly_kg: Kg


class CollectorPolicy(Contract):
    permits: list[WasteItem]
    materials: list[str]
    forms: list[Literal["film", "rigid", "paper", "other"]]
    routes: list[Literal["sell", "bundle", "pyrolysis"]] = Field(
        default_factory=lambda: ["sell", "bundle"]
    )
    min_kg: Kg
    truck_kg: Kg
    days: list[Literal["월", "화", "수", "목", "금"]] = Field(min_length=1)
    max_per_kg: int = Field(ge=0, le=100000)
    max_per_month: int = Field(ge=1, le=31)
    permit_verified: bool = False
    evidence: str = Field(min_length=1, max_length=1000)
    minimum_basis: Literal["month", "pickup"] = "pickup"


class CompanyProfile(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=100)
    complex_id: str = Field(pattern=r"^[a-z0-9-]{1,100}$")
    location: tuple[
        Annotated[float, Field(ge=-180, le=180)], Annotated[float, Field(ge=-90, le=90)]
    ]
    role: Literal["배출", "처리업체", "수요처"]
    supplies: list[MaterialSupply] = Field(default_factory=list)
    collector: CollectorPolicy | None = None
    visibility: Literal["private", "complex"] = "complex"
    is_demo: bool = False
    pickup_window: PickupWindow = Field(default_factory=PickupWindow)


class CreateSession(Contract):
    company_id: str
    title: str = Field(default="폐플라스틱 흐름 진단", min_length=1, max_length=100)
    streams: list[Stream] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_streams(self):
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", s.id) for s in self.streams):
            raise ValueError("흐름 ID는 영문·숫자·밑줄·하이픈 1~100자여야 합니다")
        if len({s.id for s in self.streams}) != len(self.streams):
            raise ValueError("흐름 ID가 중복되었습니다")
        if any(not math.isfinite(s.monthly_kg) or s.monthly_kg > 1_000_000 for s in self.streams):
            raise ValueError("월 물량은 유한한 0~1,000,000kg 값이어야 합니다")
        return self


class Versioned(Contract):
    version: int = Field(ge=1)


class AdvanceRequest(Versioned):
    step: FlowStep


class MatchRequest(Versioned):
    stream_id: str
    form: Literal["film", "rigid", "paper", "other"]
    route: Literal["bundle", "sell", "pyrolysis"] = "bundle"
    radius_m: int = Field(default=15000, ge=100, le=100000)


class InviteRequest(Versioned):
    bundle_id: str
    confirm: Literal[True]


class ReplyRequest(Versioned):
    decision: Literal["accepted", "declined"]
    reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def decline_reason(self):
        if self.decision == "declined" and not self.reason.strip():
            raise ValueError("불참 사유를 입력하세요")
        return self


class NegotiationRequest(Versioned):
    bundle_id: str
    guardrails: Guardrails
    requested: PickupTerms

    @model_validator(mode="after")
    def bounded(self):
        g = self.guardrails
        if not 1 <= g.max_rounds <= 12 or not g.days or not set(g.days) <= set("월화수목금"):
            raise ValueError("가능 요일과 최대 1~12회 협상 범위를 확인하세요")
        if g.min_per_kg < 0 or g.min_per_month > 31:
            raise ValueError("단가·횟수 범위를 확인하세요")
        return self


class ApprovalRequest(Versioned):
    confirm: Literal[True]


class CommandRequest(Versioned):
    text: str = Field(min_length=1, max_length=200)


class AnswerRequest(Versioned):
    stream_id: str
    monthly_kg: Kg
    origin: Literal["PIR", "PCR"]


class QuestionReply(Versioned):
    question_id: str
    answer: str = Field(min_length=1, max_length=2000)


class ProposalApproval(ApprovalRequest):
    route_id: Literal["A", "B", "C", "D"]


class BundleMember(Contract):
    company_id: str
    name: str
    kg: float | None
    location: tuple[float, float] | None = None
    state: Literal["self", "not-sent", "pending", "accepted", "declined"]
    reason: str = ""


class Visit(Contract):
    company_id: str
    kg: float | None
    arrival_minute: int | None = None


class Trip(Contract):
    visits: list[Visit]
    kg: float
    distance_m: int


class Bundle(Contract):
    id: str
    stream_id: str
    material: str
    origin: Literal["PIR", "PCR"]
    route: Literal["bundle", "sell", "pyrolysis"]
    collector_id: str
    collector_name: str
    min_kg: float
    truck_kg: float
    members: list[BundleMember]
    candidate_kg: float
    confirmed_kg: float
    ready: bool
    trips: list[Trip]
    excluded: list[str]
    distance_basis: str = "직선거리 추정 · 실제 도로 경로 아님"
    schedule: str = "미확정 · 사람 승인 필요"
    is_demo: bool
    form: Literal["film", "rigid", "paper", "other"] = "film"
    radius_m: int = 15000
    pickups_per_month: int = 1
    pickup_slot: Literal["오전", "오후"] | None = None
    minimum_basis: Literal["month", "pickup"] = "month"


class PlatformState(Contract):
    version: int
    session: Session
    bundles: list[Bundle] = Field(default_factory=list)


class InboxItem(Contract):
    session_id: str
    version: int
    bundle: Bundle


class CommandResult(Contract):
    message: str
    state: PlatformState


class NegotiationResult(Contract):
    negotiation: Negotiation
    state: PlatformState


class ProgressResult(Contract):
    state: PlatformState
    blocked_step: FlowStep | None = None
    reason: str | None = None
