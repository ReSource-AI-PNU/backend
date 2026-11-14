"""① 식별·분류 API. 흐름 하나의 사진·영상을 받아 Stream.classify 와 출처 추정, 되묻기 질문을 돌려준다."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.agents.classify import (
    ClassifyAnswerIn,
    ClassifyAnswerOut,
    ClassifyOutput,
    answer_classify_question,
    classify_stream,
)
from app.ml.detector import ModelUnavailable, get_detector

router = APIRouter(tags=["classify"])
ALLOWED = {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
MAX_BYTES = 200 * 1024 * 1024


@router.get("/models")
def models() -> dict:
    """현재 쓰는 식별 모델 (trained | zeroshot) 과 버전"""
    try:
        return {"ok": True, **get_detector().status()}
    except ModelUnavailable as e:
        return {"ok": False, "error": str(e)}


@router.post("/classify", response_model=ClassifyOutput, response_model_exclude_none=True)
async def classify(
    files: Annotated[list[UploadFile], File(description="사진 여러 장 또는 30초 이내 영상 1개")],
    stream_id: Annotated[str, Form()],
    name: Annotated[str, Form(description="흐름 이름 (예: 스프루·러너)")] = "",
    process: Annotated[str, Form(description="공정명 (예: 사출 1·2라인)")] = "",
    material_doc_text: Annotated[str, Form(description="원자재 명세·MSDS 에서 뽑은 텍스트 (선택, backend#9)")] = "",
    material_hint: Annotated[str | None, Form(description="사용자가 아는 수지 (예: PP)")] = None,
) -> ClassifyOutput:
    tmp = Path(tempfile.mkdtemp(prefix="upload_"))
    try:
        paths = []
        for i, f in enumerate(files):
            ext = Path(f.filename or "").suffix.lower()
            if ext not in ALLOWED:
                raise HTTPException(415, f"지원하지 않는 형식: {ext}")
            dst = tmp / f"{i:03d}{ext}"
            size = 0
            with dst.open("wb") as w:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "파일이 200MB 를 넘습니다")
                    w.write(chunk)
            paths.append(str(dst))
        try:
            return classify_stream(paths, stream_id=stream_id, name=name, process=process, material_doc_text=material_doc_text, material_hint=material_hint)
        except ModelUnavailable as e:
            raise HTTPException(503, f"식별 모델을 쓸 수 없습니다: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@router.post("/classify/answer", response_model=ClassifyAnswerOut, response_model_exclude_none=True)
def classify_answer(body: ClassifyAnswerIn) -> ClassifyAnswerOut:
    """① 되묻기 답(필름·수지·품목·혼합·출처·월 발생량)을 흐름에 반영하고 Correction 초안을 돌려준다."""
    try:
        return answer_classify_question(body)
    except ValueError as e:
        raise HTTPException(422, str(e))
