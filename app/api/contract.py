"""계약 확인용 읽기 전용 API. 프런트 타입 생성(frontend#2)과 /docs 예시에 쓴다.

예시 데이터는 프런트 시연 앱이 만든 (주)한빛정밀 네 흐름 진단을 그대로 저장한 것이다(app/examples).
"""

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter

from app.schemas import (
    Contract,
    DatasetVersion,
    KnowledgeCard,
    LabelTask,
    MaterialPassport,
    Member,
    ModelVersion,
    Organization,
    OutboxMessage,
    Session,
    TrainingRun,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

router = APIRouter(prefix="/contract", tags=["contract"])


class DemoCollections(Contract):
    """세션 밖에서 쓰는 컬렉션 예시"""

    knowledge_cards: list[KnowledgeCard]
    label_tasks: list[LabelTask]
    model_versions: list[ModelVersion]
    training_runs: list[TrainingRun]
    dataset_versions: list[DatasetVersion]
    outbox: list[OutboxMessage]
    organizations: list[Organization]
    members: list[Member]
    passports: list[MaterialPassport] | None = None


@lru_cache
def demo_session() -> Session:
    return Session.model_validate_json((EXAMPLES / "demo_session.json").read_text(encoding="utf-8"))


@lru_cache
def demo_collections() -> DemoCollections:
    text = (EXAMPLES / "demo_collections.json").read_text(encoding="utf-8")
    return DemoCollections.model_validate_json(text)


@router.get("/session-example", response_model=Session, response_model_exclude_unset=True)
def session_example() -> Session:
    """승인과 실제 결과 기록까지 끝난 시연 진단 한 건"""
    return demo_session()


@router.get(
    "/collections-example", response_model=DemoCollections, response_model_exclude_none=True
)
def collections_example() -> DemoCollections:
    """현장 지식, 라벨 검수, 모델·데이터셋, 발송함, 회사·멤버 예시"""
    return demo_collections()
