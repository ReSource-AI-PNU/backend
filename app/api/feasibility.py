"""Stateless synthetic economics preview; no session is created or modified."""

from typing import Annotated, Self

from fastapi import APIRouter, Body
from pydantic import Field, model_validator

from app.agents.economics_adapter import evaluate_streams
from app.api.contract import demo_session
from app.demo.synthetic import CONFIG
from app.schemas import Contract, FeasibilityResult, Stream, StreamRoute

router = APIRouter(prefix="/feasibility", tags=["feasibility"])


class FeasibilityPreviewRequest(Contract):
    """Shared streams and optional route overrides; prices remain server-side demo values."""

    streams: list[Stream] = Field(min_length=1)
    overrides: dict[str, StreamRoute] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stream_ids(self) -> Self:
        ids = [stream.id for stream in self.streams]
        if len(set(ids)) != len(ids):
            raise ValueError("stream IDs must be unique")
        if set(self.overrides) - set(ids):
            raise ValueError("override IDs must refer to supplied streams")
        return self


DEMO_REQUEST = {
    "streams": [s.model_dump(mode="json", by_alias=True, exclude_unset=True)
                for s in demo_session().streams],
    "overrides": {},
}


@router.post("/preview", response_model=FeasibilityResult)
def preview(
    request: Annotated[
        FeasibilityPreviewRequest,
        Body(openapi_examples={
            "synthetic_factory": {
                "summary": "가상 PP 사출 공장 네 흐름",
                "description": "서버의 synthetic/demo 단가와 규칙으로 계산합니다.",
                "value": DEMO_REQUEST,
            }
        }),
    ],
) -> FeasibilityResult:
    """Calculate a synthetic preview without storing or changing a session."""
    return evaluate_streams(request.streams, CONFIG, request.overrides)
