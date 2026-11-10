"""6단계 데이터 계약 (backend#1).

프런트 `src/lib/session/types.ts`·`src/lib/domain.ts`와 같은 모양이다. 진단 단위는 사진 한 장이 아니라
"공장의 폐플라스틱 흐름(Stream) 여러 개"다 (설계안 v2 5장).

- JSON 필드 이름은 프런트와 같은 camelCase다. 파이썬 속성은 snake_case로 쓰고 별칭으로 바꾼다.
- 금액은 원 단위 정수, 중량은 kg 실수, 탄소는 tCO₂e 실수다.
- 계약에 없는 필드는 받지 않는다(extra="forbid"). 프런트와 어긋나면 테스트가 바로 실패한다.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        use_attribute_docstrings=True,
    )


# ── 공통 값 ─────────────────────────────────────────────────────────────

WasteItem = Literal["폐플라스틱", "금속 스크랩", "폐목재", "포장재"]
"""대회 공지의 네 품목. 앱은 LDPE 포장 필름과 골판지를 '포장재'로 묶는다(법정 분류로 필름은 폐합성수지)."""

StorageType = Literal["실내", "야외·지붕", "야외·노출"]
CompanyRole = Literal["배출", "처리업체", "수요처"]
LngLat = tuple[float, float]

Won = int
"""원 단위 정수. 처리비처럼 나가는 돈은 음수다."""

Ratio = Annotated[float, Field(ge=0, le=1)]
Box = tuple[Ratio, Ratio, Ratio, Ratio]
"""사진 크기에 대한 비율(0~1): [x, y, w, h]"""

FlowStep = Literal["classify", "feasibility", "regulation", "matching", "cost-carbon", "proposal"]
SessionStatus = Literal["analyzing", "in-progress", "awaiting-approval", "sent", "completed"]
Mark = Literal["ai", "human"]
"""형광펜(ai) / 먹(human). AI 원래 값은 지우지 않는다."""

StreamOrigin = Literal["PIR", "PCR"]
"""PIR = 공정 부산물(스프루, 불량품, 퍼지), PCR = 공장이 쓰고 버린 포장재(스트레치 필름, 마대)."""

StreamRoute = Literal["reuse", "sell", "bundle", "pyrolysis", "incinerate"]
"""흐름별 갈 곳. 화면과 문서에서는 '경로'라고 부른다."""

STREAM_ROUTE_LABEL: dict[str, str] = {
    "reuse": "공정 재투입",
    "sell": "매각",
    "bundle": "단지 묶음 매각",
    "pyrolysis": "열분해",
    "incinerate": "소각",
}

ScenarioId = Literal["A", "B", "C", "D"]
"""공장 시나리오. 흐름의 '경로'(StreamRoute)와 구분해 '시나리오'라고 부른다."""


# ── 회사·산업단지 ──────────────────────────────────────────────────────


class WasteStream(Contract):
    """회사 프로필에 적는 품목별 월 배출량. 진단의 Stream과 다르다."""

    item: WasteItem
    detail: str | None = None
    monthly_kg: float = Field(ge=0)
    storage: StorageType | None = None


class Company(Contract):
    """산업단지 입주 기업(시연은 가상 기업)."""

    id: str
    name: str
    building_osm_id: int
    location: LngLat
    industry: str
    role: CompanyRole
    waste_streams: list[WasteStream]
    permits: list[WasteItem] | None = None
    employees: str | None = None
    is_demo: bool | None = None


class Organization(Contract):
    """우리 서비스에 가입한 회사 (backend#23 기업 프로필)."""

    id: str
    owner_id: str
    name: str
    biz_no: str
    industry: str
    complex_id: str
    role: CompanyRole
    contact_title: str
    waste_streams: list[WasteStream]
    created_at: str
    updated_at: str


class Member(Contract):
    id: str
    org_id: str
    name: str
    email: str
    role: Literal["관리자", "승인권자", "담당자"]
    status: Literal["active", "invited"]
    invited_at: str


# ── ① 식별·분류 ────────────────────────────────────────────────────────


class Media(Contract):
    id: str
    kind: Literal["image", "video", "pdf", "sample"]
    """sample이면 url은 "sample:<variant>" (시연용 일러스트)"""
    name: str
    url: str
    size: int | None = None


class DetectionOriginal(Contract):
    label: str
    box: Box
    ratio: Ratio | None = None


class Detection(Contract):
    id: str
    label: str
    kind: Literal["item", "contaminant"]
    confidence: Ratio
    box: Box
    source: Mark
    original: DetectionOriginal | None = None
    """사람이 고쳤으면 AI 원래 값"""
    ratio: Ratio | None = None
    """이물이면 면적 기준 추정 비율. 매입처는 무게로 등급을 매기므로 확정값이 아니다."""
    removed: bool | None = None


class ModelRef(Contract):
    name: str
    version: str


class ClassifyResult(Contract):
    item: WasteItem
    detail: str
    """예: "PP 사출 스프루·러너" """
    material: str
    """예: "PP", "PP+ABS", "LDPE". 사진만으로 확정하지 않고 원자재 명세나 답변으로 확정한다."""
    single_material: bool
    confidence: Ratio
    estimated_kg: float = Field(ge=0)
    """사진 한 장 기준 추정. 월 발생량은 Stream.monthlyKg로 따로 확정한다."""
    frames: int = Field(ge=0)
    detections: list[Detection]
    model: ModelRef
    duration_ms: int = Field(ge=0)


class Stream(Contract):
    """공장의 폐플라스틱 흐름 하나. 진단의 기본 단위."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "st_sprue",
                    "name": "스프루·러너",
                    "process": "사출 1·2라인",
                    "media": [
                        {"id": "m1", "kind": "image", "name": "sprue.jpg", "url": "blob://..."}
                    ],
                    "classify": {
                        "item": "폐플라스틱",
                        "detail": "PP 사출 스프루·러너",
                        "material": "PP",
                        "singleMaterial": True,
                        "confidence": 0.93,
                        "estimatedKg": 600,
                        "frames": 1,
                        "detections": [],
                        "model": {"name": "rtdetr-waste", "version": "v3.2"},
                        "durationMs": 1800,
                    },
                    "origin": "PIR",
                    "originSource": "ai",
                    "monthlyKg": 600,
                    "monthlyKgConfirmed": True,
                }
            ]
        }
    )

    id: str
    name: str
    process: str
    media: list[Media]
    classify: ClassifyResult
    origin: StreamOrigin
    origin_source: Mark
    monthly_kg: float = Field(ge=0)
    monthly_kg_confirmed: bool
    """사람이 확인하기 전에는 추정값이다."""


# ── 되묻기 ─────────────────────────────────────────────────────────────


class Question(Contract):
    """정보가 부족할 때 담당자에게 되묻는 질문. 흐름별 질문이면 streamId가 있다."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "pcr-proof:st_film",
                    "step": "regulation",
                    "streamId": "st_film",
                    "text": "원자재 포장 필름의 입고 기록(원자재 납품서·포장 사양)이 있나요?",
                    "why": "공장이 쓰고 버린 포장재를 사용 후(PCR) 재생원료로 팔려면 출처를 증명해야 합니다.",
                    "kind": "choice",
                    "options": ["있음", "없음"],
                }
            ]
        }
    )

    id: str
    step: FlowStep
    stream_id: str | None = None
    text: str
    why: str
    kind: Literal["number", "choice"]
    unit: str | None = None
    options: list[str] | None = None
    suggested: str | None = None
    answer: str | None = None


class QuestionAnswer(Contract):
    """되묻기 답변. 답하면 해당 단계를 다시 판정한다."""

    question_id: str
    answer: str


# ── ② 경로 판정 ────────────────────────────────────────────────────────


class Reason(Contract):
    text: str
    source: str
    knowledge_card_id: str | None = None


class FieldAction(Contract):
    """경로에 필요한 현장 작업. 인건비는 월 손익에서 뺀다."""

    title: str
    labor_hours: float = Field(ge=0)
    labor_cost: Won


class LadderStep(Contract):
    label: str
    per_kg: Won


class RouteAlternative(Contract):
    route: StreamRoute
    per_kg: Won
    note: str


class StreamDecision(Contract):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "streamId": "st_sprue",
                    "route": "reuse",
                    "reason": "깨끗한 단일 PP라 분쇄해서 같은 공정에 다시 넣을 수 있습니다.",
                    "currentPerKg": -250,
                    "afterPerKg": 900,
                    "monthlyNow": -150000,
                    "monthlyAfter": 510000,
                    "carbonNowT": 0.47,
                    "carbonAfterT": -0.96,
                    "action": {
                        "title": "색별로 분리해 분쇄기에 넣으세요",
                        "laborHours": 2,
                        "laborCost": 30000,
                    },
                    "ladder": [
                        {"label": "현재(혼합 소각)", "perKg": -250},
                        {"label": "분쇄 재투입", "perKg": 900},
                    ],
                    "alternatives": [
                        {"route": "sell", "perKg": 300, "note": "재투입보다 가치가 낮음"}
                    ],
                }
            ]
        }
    )

    stream_id: str
    route: StreamRoute
    reason: str
    current_per_kg: Won
    """현행(혼합 소각) kg당 손익. 처리비라 음수"""
    after_per_kg: Won
    """추천 경로 kg당 손익. 재투입이면 신재 대체 가치"""
    monthly_now: Won
    monthly_after: Won
    """after_per_kg × 월 발생량 − 현장 작업 인건비"""
    carbon_now_t: float
    carbon_after_t: float
    action: FieldAction | None = None
    ladder: list[LadderStep]
    alternatives: list[RouteAlternative]
    ai_route: StreamRoute | None = None
    """경로를 사람이 바꿨으면 AI 원래 경로"""


class FeasibilityResult(Contract):
    decisions: list[StreamDecision]
    total_now: Won
    """공장 월 손익: 현행 전부 소각"""
    total_after: Won
    """공장 월 손익: 흐름별 최적안"""
    carbon_now_t: float
    carbon_after_t: float
    reasons: list[Reason]
    note: str


# ── ③ 규정 검토 ────────────────────────────────────────────────────────


class RegulationItem(Contract):
    id: str
    stream_id: str | None = None
    label: str
    status: Literal["ok", "no", "ask"]
    result: str
    citation: str
    """「법령명」 제○조 형식의 인용"""
    excerpt: str


class Conclusion(Contract):
    tone: Literal["gain", "warn", "loss"]
    text: str


class RegulationResult(Contract):
    items: list[RegulationItem]
    conclusion: Conclusion


# ── ④ 업체·묶음 매칭 ───────────────────────────────────────────────────


class MatchingMember(Contract):
    company_id: str
    name: str
    kg: float = Field(ge=0)
    """다른 회사의 물량은 배출 기업 화면에서 숨긴다(분담 비율만 공개)."""
    distance_m: float = Field(ge=0)
    is_self: bool | None = None


class Collector(Contract):
    company_id: str
    name: str
    distance_m: float = Field(ge=0)
    permits: list[WasteItem]


class ExcludedCompany(Contract):
    company_id: str
    name: str
    reason: str


class CostShare(Contract):
    company_id: str
    name: str
    share_pct: float = Field(ge=0, le=100)


class BundleGroup(Contract):
    """묶음 매각 흐름 하나에 대한 순회 수거 그룹. 공동 보관은 금지다."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "streamId": "st_film",
                    "mode": "순회 수거",
                    "item": "포장재",
                    "minKg": 1000,
                    "truckKg": 1000,
                    "fillRate": 1.23,
                    "soloFillRate": 0.3,
                    "group": [
                        {
                            "companyId": "noksan-001",
                            "name": "(주)한빛정밀",
                            "kg": 300,
                            "distanceM": 0,
                            "isSelf": True,
                        }
                    ],
                    "totalKg": 1230,
                    "collector": {
                        "companyId": "noksan-010",
                        "name": "(주)새길자원",
                        "distanceM": 3182,
                        "permits": ["폐플라스틱", "포장재"],
                    },
                    "excluded": [
                        {
                            "companyId": "noksan-025",
                            "name": "(주)미소환경",
                            "reason": "폐플라스틱(필름) 허가 품목 없음",
                        }
                    ],
                    "schedule": "매월 둘째·넷째 주 화요일 오전, 사업장별 순회",
                    "costShare": [
                        {"companyId": "noksan-001", "name": "(주)한빛정밀", "sharePct": 24.4}
                    ],
                }
            ]
        }
    )

    stream_id: str
    mode: Literal["순회 수거", "공동처리"]
    """기본값 순회 수거: 각 회사가 같은 허가 업체와 따로 계약하고, 업체가 사업장을 차례로 돈다."""
    item: WasteItem
    min_kg: float = Field(ge=0)
    """업체가 한 번 출장할 만한 물량(품목별 가정값)"""
    truck_kg: float = Field(gt=0)
    fill_rate: float = Field(ge=0)
    """묶음 합계 / 트럭 적재량. 1을 넘으면 트럭 여러 대 분량"""
    solo_fill_rate: float = Field(ge=0)
    group: list[MatchingMember]
    total_kg: float = Field(ge=0)
    collector: Collector
    excluded: list[ExcludedCompany]
    schedule: str
    cost_share: list[CostShare]
    visit_order: list[str] | None = None
    """수거 업체가 출발해 도는 사업장 순서(그룹 기업 id). 가장 가까운 곳부터 돈다(backend#14에서 OR-Tools로 대체)"""
    route_m: float | None = Field(default=None, ge=0)
    """수거 업체 출발 → 순회 → 복귀 거리(m). 직선거리에 도로 계수 1.3을 곱한 가정값"""


class StreamPartner(Contract):
    """묶음이 아닌 흐름의 거래처(매각처·열분해 업체). 재투입·소각은 거래처가 없다."""

    stream_id: str
    route: StreamRoute
    company_id: str | None = None
    name: str
    distance_m: float = Field(ge=0)
    location: str
    """단지 밖 업체면 위치 설명"""
    note: str


class MatchingResult(Contract):
    complex_id: str
    self_location: LngLat
    bundles: list[BundleGroup]
    partners: list[StreamPartner]


# ── ⑤ 비용·탄소 비교 ───────────────────────────────────────────────────


class ScenarioLine(Contract):
    """시나리오 안의 흐름별 내역"""

    stream_id: str
    route: StreamRoute
    monthly_krw: Won
    carbon_t: float


class Route(Contract):
    """공장 시나리오 A·B·C·D. 프런트 타입 이름(Route)을 그대로 쓴다."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "A",
                    "name": "흐름별 최적안",
                    "description": "스프루·러너 공정 재투입 · 불량 성형품 매각 · 퍼지·색 혼합 열분해 · 원자재 포장 필름 단지 묶음 매각",
                    "monthlyKrw": 595000,
                    "carbonT": -2.06,
                    "kauKrw": 18540,
                    "condition": "분리 보관 구역, 분쇄기 사용, 단지 3개사 순회 수거 동의",
                    "recommended": True,
                    "breakdown": [
                        {
                            "streamId": "st_sprue",
                            "route": "reuse",
                            "monthlyKrw": 510000,
                            "carbonT": -0.96,
                        }
                    ],
                }
            ]
        }
    )

    id: ScenarioId
    name: str
    description: str
    monthly_krw: Won
    carbon_t: float
    kau_krw: Won
    """줄인 탄소를 배출권 가격으로 바꾼 참고값"""
    condition: str
    recommended: bool | None = None
    breakdown: list[ScenarioLine]


class Assumption(Contract):
    label: str
    value: str


class CostCarbonResult(Contract):
    routes: list[Route]
    assumptions: list[Assumption]
    baseline: Literal["신재 대비", "매립 대비"]


# ── ⑥ 이행안·승인 ──────────────────────────────────────────────────────

DocumentKind = Literal["plan", "grant", "pickup", "quote", "joint"]
"""plan 이행 계획서, grant 지원사업 신청 초안, pickup 순회 수거 요청서,
quote 매각 견적 요청서·열분해 위탁 요청서(title로 구분), joint 공동처리 신고서 초안(선택)"""


class DocumentField(Contract):
    label: str
    value: str


class DocumentDraft(Contract):
    id: str
    kind: DocumentKind
    title: str
    to: str
    fields: list[DocumentField]
    body: str
    external: bool
    """외부로 보내는 서류인가(승인 게이트 대상)"""
    optional: bool | None = None
    stream_id: str | None = None


class ProposalResult(Contract):
    recommended: ScenarioId
    alternatives: list[ScenarioId]
    summary: str
    documents: list[DocumentDraft]


class SentTo(Contract):
    name: str
    kind: str


class Approval(Contract):
    approved_by: str
    approved_at: str
    route_id: ScenarioId
    sent_to: list[SentTo]


class Outcome(Contract):
    """수거 후 업체가 기록한 실제 결과. 단가 보정의 정답 신호이고, 탐지 모델 학습에는 쓰지 않는다."""

    recorded_at: str
    recorded_by: str
    recorder_role: CompanyRole | Literal["담당자"]
    stream_id: str | None = None
    accepted: bool
    actual_grade: str
    actual_per_kg: Won
    actual_kg: float = Field(ge=0)
    reason: str | None = None


class Correction(Contract):
    """사람이 판정을 고친 기록. 이유는 현장 지식 카드로 승격될 수 있다."""

    id: str
    at: str
    by: str
    step: FlowStep
    stream_id: str | None = None
    field: str
    before: str
    after: str
    rationale: str | None = None
    knowledge_card_id: str | None = None


class Session(Contract):
    id: str
    org_id: str
    owner_id: str
    complex_id: str
    title: str
    created_at: str
    updated_at: str
    status: SessionStatus
    step: FlowStep
    """사용자가 마지막으로 연 단계"""
    streams: list[Stream]
    questions: list[Question]
    feasibility: FeasibilityResult | None = None
    regulation: RegulationResult | None = None
    matching: MatchingResult | None = None
    cost_carbon: CostCarbonResult | None = None
    proposal: ProposalResult | None = None
    selected_route: ScenarioId | None = None
    approval: Approval | None = None
    outcome: Outcome | None = None
    """가장 최근에 기록한 실제 결과(하위 호환). 흐름별 결과는 outcomes"""
    outcomes: list[Outcome] | None = None
    """흐름별 실제 결과. 같은 흐름은 최신 기록 하나만 남긴다."""
    corrections: list[Correction]


class MessageReply(Contract):
    """요청서에 대한 받는 쪽의 회신. 묶음 참여 제안이면 참여 여부다."""

    decision: Literal["accepted", "declined"]
    at: str
    by: str
    reason: str | None = None


class OutboxMessage(Contract):
    """승인 후 업체에 보낸 요청(시연은 모의 메일함)."""

    id: str
    session_id: str
    org_id: str
    from_org: str
    to_company_id: str
    to_name: str
    kind: Literal["quote", "bundle-invite", "pickup"]
    stream_id: str | None = None
    subject: str
    sent_at: str
    status: Literal["sent", "read", "replied", "completed"]
    reply: MessageReply | None = None


# ── 현장 지식·재학습 ───────────────────────────────────────────────────


class KnowledgeCard(Contract):
    """암묵지: 현장 숙련자의 판단 기준. 조건 → 판단 → 근거"""

    id: str
    item: WasteItem
    condition: str
    judgment: str
    rationale: str
    step: FlowStep
    author: str
    org_name: str
    created_at: str
    status: Literal["proposed", "reviewing", "adopted", "rejected"]
    agrees: list[str]
    source_session_id: str | None = None
    cited_count: int = Field(ge=0)


class LabelTask(Contract):
    id: str
    session_id: str
    kind: Literal["box", "ratio", "grade", "outcome", "origin", "route"]
    title: str
    before: str
    after: str
    source: Literal["현장 수정", "업체 실제 결과"]
    created_at: str
    status: Literal["pending", "accepted", "rejected"]
    reviewer: str | None = None


class ModelMetrics(Contract):
    map50: float = Field(alias="mAP50")
    contamination_mae: float
    verdict_agreement: float
    latency_ms: float


class ModelVersion(Contract):
    id: str
    name: str
    version: str
    created_at: str
    dataset_version: str
    status: Literal["production", "candidate", "archived", "training"]
    metrics: ModelMetrics


class TrainingRun(Contract):
    id: str
    started_at: str
    finished_at: str | None = None
    dataset_version: str
    base_model: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: Ratio
    result_model_id: str | None = None
    triggered_by: str


class DatasetVersion(Contract):
    id: str
    created_at: str
    images: int = Field(ge=0)
    new_labels: int = Field(ge=0)
    from_corrections: int = Field(ge=0)
    from_outcomes: int = Field(ge=0)
    note: str
    label_task_ids: list[str]
    """이 버전에 들어간 검수 라벨"""
    created_by: str
