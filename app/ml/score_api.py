"""탐지 모델 채점 서버. v3 사진·영상 경로(app/v3/intake/detector.py)가 부르는 엔드포인트다.

설계는 Azure ML 관리형 온라인 엔드포인트지만, 구독에 엔드포인트를 둘 자리가 없어(infra/README.md) 이 앱을
H100 계산 인스턴스에서 systemd 단위 rsai-detector로 GPU로 띄운다(infra/h100/detector, 2026-11-22부터).
그 전에는 앱 VM에서 같은 백엔드 이미지를 APP_ROLE=detector로 띄워 CPU로 추론했다.

요청:  POST /score  {"input_data": {"filename", "media_type": "image | video", "content_base64"}}
       Authorization: Bearer <DETECTOR_API_KEY>
응답:  app/ml/track.py run() 결과. app/v3/intake/detector.py read_result가 읽는 모양 그대로다.
모델은 app.ml.detector.get_detector()로만 부른다(v1 /classify와 같은 가중치, 같은 설정).
추론은 한 번에 하나씩 한다(GPU 하나를 Ollama·재구성 서버와 나눠 쓴다).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.ml.detector import ModelUnavailable, get_detector
from app.v3.intake.detector import media_type

MAX_BYTES = 200 * 1024 * 1024
# track.frames가 사진으로 읽는 확장자. 그 밖의 사진(webp, bmp)은 png로 바꿔 넘긴다
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")

# 같은 모델 객체를 여러 요청이 동시에 부르지 않게 한다(FastAPI는 동기 처리기를 스레드 풀에서 돌린다)
_INFER = threading.Lock()

app = FastAPI(title="Re:Source AI detector", docs_url=None, redoc_url=None, openapi_url=None)


class ScoreInput(BaseModel):
    filename: str
    media_type: str | None = None
    content_base64: str


class ScoreRequest(BaseModel):
    input_data: ScoreInput


def check_key(authorization: str | None) -> None:
    """DETECTOR_API_KEY와 같은 Bearer 토큰만 받는다. 키가 설정되지 않았으면 아무 요청도 받지 않는다."""
    key = os.getenv("DETECTOR_API_KEY", "")
    if not key:
        raise HTTPException(503, "DETECTOR_API_KEY가 설정되지 않았습니다.")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), key):
        raise HTTPException(401, "탐지 서버 키가 맞지 않습니다.")


def plain(value: Any) -> Any:
    """numpy 수 같은 값을 JSON으로 보낼 수 있는 파이썬 값으로 바꾼다."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "item") and callable(value.item):
        return value.item()
    return value


def write_input(folder: Path, filename: str, data: bytes) -> Path:
    """받은 파일을 track.run이 읽는 꼴로 쓴다. 사진은 jpg·png로 맞춘다."""
    suffix = Path(filename).suffix.lower()
    if media_type(filename) == "image" and suffix not in PHOTO_SUFFIXES:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise HTTPException(400, "사진을 읽지 못했습니다.")
        path = folder / "input.png"
        cv2.imwrite(str(path), image)
        return path
    path = folder / f"input{suffix}"
    path.write_bytes(data)
    return path


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/score")
def score(body: ScoreRequest, authorization: str | None = Header(default=None)) -> JSONResponse:
    check_key(authorization)
    filename = Path(body.input_data.filename or "").name
    if media_type(filename) is None:
        raise HTTPException(415, "사진이나 영상 파일이 아닙니다.")
    try:
        data = base64.b64decode(body.input_data.content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(400, "content_base64를 읽지 못했습니다.") from error
    if not data:
        raise HTTPException(400, "파일이 비어 있습니다.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "파일이 200MB를 넘습니다.")
    folder = Path(tempfile.mkdtemp(prefix="score_"))
    try:
        path = write_input(folder, filename, data)
        try:
            with _INFER:
                result = get_detector().classify(str(path))
        except ModelUnavailable as error:
            raise HTTPException(503, f"탐지 모델을 쓸 수 없습니다: {error}") from error
        return JSONResponse(plain(result))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
