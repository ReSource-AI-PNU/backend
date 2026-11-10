"""① 식별·분류 에이전트 (흐름 단위). 모델 호출은 app.ml.detector.get_detector() 로만 한다.

입력: 흐름의 미디어 파일 경로들, 흐름 이름·공정명, (선택) 원자재 명세 텍스트
출력: ClassifyOutput = Stream.classify + origin(AI 추정) + 되묻기 Question[]
"""
from __future__ import annotations

from app.ml.contract import to_stream_classify
from app.ml.detector import get_detector
from app.schemas import ClassifyResult, Contract, Mark, Question, StreamOrigin


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


def classify_stream(media_paths: list[str], *, stream_id: str, name: str = "", process: str = "", material_doc_text: str = "",
                    material_hint: str | None = None, detector=None) -> ClassifyOutput:
    det = detector or get_detector()
    raw = det.classify(media_paths)
    out = to_stream_classify(raw, stream_id=stream_id, name=name, process=process, material_doc_text=material_doc_text, material_hint=material_hint)
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
