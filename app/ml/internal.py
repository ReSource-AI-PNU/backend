"""① 식별·분류 출력 계약 (backend#1 schemas.py 의 ClassifyResult 초안)

트랙 D가 backend#1 을 확정하면 이 파일을 그쪽으로 옮기고 필드명을 맞춘다.
② 가능성 판단이 쓰는 값: primary_material, items[].contaminated_ratio, contamination.overall_ratio
"""
from typing import Literal

from pydantic import BaseModel, Field

Material = Literal["plastic", "metal", "wood", "paper", "other"]


class DetectedItem(BaseModel):
    material: Material
    label_ko: str
    count_estimate: int = Field(description="영상: 트랙 수, 사진: 한 장에서 본 최대 개수")
    area_ratio: float = Field(description="탐지된 전체 면적 중 이 재질 비율 (0~1)")
    frame_coverage: float = Field(description="화면 대비 면적 비율 (0~1)")
    confidence: float = Field(description="val 세트로 보정한 정밀도 기준 신뢰도 (0~1)")
    contaminated_ratio: float | None = Field(None, description="이 재질 객체 중 이물질 묻은 비율")
    multi_material_ratio: float | None = Field(None, description="라벨·뚜껑 등 다른 재질이 붙은 비율")


class Contamination(BaseModel):
    reliable: bool = Field(description="학습·검증된 도메인(선별장)이면 True. 컨베이어 등은 참고값")
    overall_ratio: float = Field(description="이물질 객체 면적 / 상태 판정된 전체 면적")
    multi_material_ratio: float
    judged_objects: int
    note: str = ""


class DetectionOut(BaseModel):
    id: str
    label: str
    kind: Literal["item", "contaminant"]
    confidence: float
    box: list[float] = Field(description="대표 프레임 기준 정규화 [x, y, w, h] (0~1)")
    ratio: float | None = Field(None, description="item: 프레임 대비 면적, contaminant: 들어 있는 재질 박스 대비 면적 (면적 기준 추정)")
    ratio_basis: str | None = None
    conds: list[str] | None = None


class ClassifyResult(BaseModel):
    stage: Literal["classify"] = "classify"
    detections: list[DetectionOut] = []
    rep_frame: int | None = None
    backend: str
    model_version: str
    mode: Literal["video", "photos"]
    primary_material: Material | None
    items: list[DetectedItem]
    input_domain: str | None = Field(None, description="aihub137_field(현장 사진) / a71385_sorting(선별장) / sortwaste_conveyor")
    contamination: Contamination | None = Field(None, description="현장 사진처럼 상태 모델 학습 도메인 밖이면 None (되묻기로 대체)")
    frames_analyzed: int
    tracks_total: int
    needs_followup: bool
    followup_questions: list[str]
    latency_sec: float
