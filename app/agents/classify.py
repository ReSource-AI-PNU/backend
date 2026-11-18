"""① 식별·분류 에이전트 (흐름 단위). 모델 호출은 app.ml.detector.get_detector() 로만 한다.

입력: 흐름의 미디어 파일 경로들, 흐름 이름·공정명, (선택) 원자재 명세 텍스트
출력: ClassifyOutput = Stream.classify + origin(AI 추정) + 되묻기 Question[]
"""

from __future__ import annotations

from app.ml.contract import apply_classify_answer, to_stream_classify
from app.ml.detector import get_detector
from app.schemas import ClassifyResult, Contract, Correction, Mark, Question, Stream, StreamOrigin


class ClassifyOutput(Contract):
    """① 결과. 프런트는 classify 를 Stream.classify 에, origin/originSource 를 Stream 에 넣고 questions 를 세션 질문에 합친다."""

    stream_id: str
    classify: ClassifyResult
    origin: StreamOrigin
    origin_source: Mark
    origin_confidence: float
    origin_evidence: list[str]
    questions: list[Question]
    input_domain: str | None = None
    """aihub137_field(현장 사진) / a71385_sorting(선별장) / sortwaste_conveyor / None(제로샷)"""


def classify_stream(
    media_paths: list[str],
    *,
    stream_id: str,
    name: str = "",
    process: str = "",
    material_doc_text: str = "",
    material_hint: str | None = None,
    detector=None,
) -> ClassifyOutput:
    det = detector or get_detector()
    raw = det.classify(media_paths)
    out = to_stream_classify(
        raw,
        stream_id=stream_id,
        name=name,
        process=process,
        material_doc_text=material_doc_text,
        material_hint=material_hint,
    )
    return ClassifyOutput(
        stream_id=stream_id,
        classify=ClassifyResult.model_validate(out["classify"]),
        origin=out["origin"],
        origin_source=out["originSource"],
        origin_confidence=out["originConfidence"],
        origin_evidence=out["originEvidence"],
        questions=[Question.model_validate(q) for q in out["questions"]],
        input_domain=out["meta"].get("input_domain"),
    )


class ClassifyAnswerIn(Contract):
    stream: Stream
    question_id: str
    """classify-<종류>:<streamId> (item, film, resin, mixed, origin, contamination, kg)"""
    answer: str
    by: str = "담당자"


class ClassifyAnswerOut(Contract):
    stream: Stream
    corrections: list[Correction]
    """AI 값을 사람이 바꾼 기록. Session.corrections 에 더한다(재학습 입구는 박스 수정만 라벨로 쓴다)."""


def answer_classify_question(body: ClassifyAnswerIn) -> ClassifyAnswerOut:
    """① 되묻기 답을 흐름에 반영. 답 순서가 달라도 결과가 같다(필름 답은 미확인 수지만 채우고, 수지 답이 수지를 정한다)."""
    stream, corrections = apply_classify_answer(
        body.stream.model_dump(mode="json", by_alias=True, exclude_none=True),
        body.question_id,
        body.answer,
        by=body.by,
    )
    return ClassifyAnswerOut(
        stream=Stream.model_validate(stream),
        corrections=[Correction.model_validate(c) for c in corrections],
    )
