"""보관장 3D 스캔 재구성 서버(H100 계산 인스턴스, 포트 8090). 앱 VM의 백엔드만 부른다.

계약(맥 ~/rs-work/scan/CONTRACT.md 2절):
  GET  /health                → {"ok", "version", "models", "busy"}
  POST /jobs (frames 여러 개) → 202 {"jobId", "status": "queued"}
  GET  /jobs/{jobId}          → {"jobId", "status", "stage", "progress", "error", "result"}
  GET  /jobs/{jobId}/points   → RSPC 점군 바이트

- 모델(VGGT-1B, MoGe-2)은 시작할 때 한 번 올리고, 작업은 한 번에 하나씩 차례로 돈다(같은 GPU에서 Ollama가 돈다).
- 계산 인스턴스의 사설 주소에만 묶는다(run.sh). 서브넷 NSG도 8090을 앱 VNet에만 연다.
- 작업 결과는 임시 디스크 /mnt/recon/jobs/<id>/에 24시간 둔다(껐다 켜면 지워진다. 백엔드는 done을 보자마자 가져간다).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import recon_pipeline as pipeline
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(os.environ.get("RECON_JOBS", "/mnt/recon/jobs"))
MIN_FRAMES, MAX_FRAMES = 12, 96
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
KEEP_SEC = 24 * 3600
JOB_ID = re.compile(r"^[0-9a-f]{16}$")

log = logging.getLogger("recon")
app = FastAPI(title="rsai-recon")
_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_queue: queue.Queue[str] = queue.Queue()
_state: dict[str, Any] = {"models": None, "loading": True, "error": None, "running": None}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {k: job.get(k) for k in ("jobId", "status", "stage", "progress", "error", "result")}


def _load() -> None:
    try:
        _state["models"] = pipeline.load_models()
    except Exception as exc:
        _state["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("모델을 올리지 못했다")
    finally:
        _state["loading"] = False


def _worker() -> None:
    while True:
        job_id = _queue.get()
        job = _jobs.get(job_id)
        if job is None:
            continue
        folder = ROOT / job_id
        _state["running"] = job_id

        def progress(stage: str, ratio: float, job: dict[str, Any] = job) -> None:
            job.update(stage=stage, progress=round(ratio, 2))

        try:
            while _state["loading"]:
                time.sleep(1)
            if _state["models"] is None:
                raise pipeline.ReconError("MODEL_UNAVAILABLE", "재구성 모델을 올리지 못했습니다.")
            job.update(status="running", stage="geometry", progress=0.05)
            blobs = [p.read_bytes() for p in sorted((folder / "frames").iterdir())]
            out = pipeline.run(_state["models"], blobs, progress=progress, max_frames=MAX_FRAMES)
            (folder / "points.bin").write_bytes(out["points"])
            (folder / "result.json").write_text(json.dumps(out["result"], ensure_ascii=False))
            shutil.rmtree(folder / "frames", ignore_errors=True)
            job.update(status="done", stage="done", progress=1.0, result=out["result"])
        except pipeline.ReconError as exc:
            job.update(status="failed", error={"code": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 작업 하나가 실패해도 서버(작업 차례)는 계속 돈다
            log.error("작업 %s 실패: %s", job_id, traceback.format_exc())
            job.update(
                status="failed", error={"code": "RECON_FAILED", "message": f"{type(exc).__name__}"}
            )
        finally:
            _state["running"] = None
            (folder / "status.json").write_text(json.dumps(_public(job), ensure_ascii=False))
            try:
                import torch

                torch.cuda.empty_cache()
            except (ImportError, RuntimeError) as exc:
                log.warning("GPU 메모리를 비우지 못했다: %s", exc)


def _cleanup() -> None:
    now = time.time()
    for folder in ROOT.iterdir() if ROOT.exists() else []:
        if folder.is_dir() and now - folder.stat().st_mtime > KEEP_SEC:
            shutil.rmtree(folder, ignore_errors=True)
            _jobs.pop(folder.name, None)


@app.on_event("startup")
def _startup() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_load, daemon=True).start()
    threading.Thread(target=_worker, daemon=True).start()


@app.get("/health")
def health() -> dict[str, Any]:
    models = _state["models"]
    return {
        "ok": models is not None,
        "loading": _state["loading"],
        "version": pipeline.VERSION,
        "models": dict(models.names) if models is not None else None,
        "busy": _state["running"] is not None or not _queue.empty(),
    }


@app.post("/jobs")
async def create_job(request: Request):
    if _state["models"] is None and not _state["loading"]:
        return _error(503, "MODEL_UNAVAILABLE", "재구성 모델을 올리지 못했습니다.")
    try:
        form = await request.form(max_files=MAX_FRAMES + 8)
    except Exception:  # noqa: BLE001 - starlette가 multipart 오류를 여러 종류로 낸다
        return _error(400, "INVALID_MULTIPART", "multipart/form-data 본문을 읽을 수 없습니다.")
    files = [f for f in form.getlist("frames") if not isinstance(f, str)]
    if len(files) < MIN_FRAMES:
        return _error(422, "TOO_FEW_FRAMES", f"장면 사진이 {MIN_FRAMES}장 이상 필요합니다.")
    if len(files) > MAX_FRAMES:
        return _error(422, "TOO_MANY_FRAMES", f"장면 사진은 {MAX_FRAMES}장까지 받습니다.")
    blobs, total = [], 0
    for f in files:
        data = await f.read()
        total += len(data)
        if not data or len(data) > MAX_FRAME_BYTES or total > MAX_TOTAL_BYTES:
            return _error(413, "TOO_LARGE", "사진이 너무 큽니다.")
        blobs.append(data)
    try:
        pipeline.decode_images(blobs)
    except pipeline.ReconError as exc:
        return _error(422, exc.code, str(exc))
    _cleanup()
    job_id = uuid.uuid4().hex[:16]
    folder = ROOT / job_id / "frames"
    folder.mkdir(parents=True)
    for i, data in enumerate(blobs):
        (folder / f"f{i:03d}.img").write_bytes(data)
    job = {
        "jobId": job_id,
        "status": "queued",
        "stage": None,
        "progress": 0.0,
        "error": None,
        "result": None,
    }
    with _lock:
        _jobs[job_id] = job
    _queue.put(job_id)
    return JSONResponse({"jobId": job_id, "status": "queued"}, status_code=202)


def _find(job_id: str) -> dict[str, Any] | None:
    if not JOB_ID.match(job_id):
        return None
    job = _jobs.get(job_id)
    if job is not None:
        return job
    status = ROOT / job_id / "status.json"
    if status.exists():  # 서버를 다시 띄운 뒤에도 끝난 결과는 디스크에서 읽는다
        return json.loads(status.read_text())
    return None


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _find(job_id)
    if job is None:
        return _error(404, "JOB_NOT_FOUND", "그런 작업이 없습니다.")
    return _public(job)


@app.get("/jobs/{job_id}/points")
def get_points(job_id: str):
    job = _find(job_id)
    path = ROOT / job_id / "points.bin"
    if job is None or job.get("status") != "done" or not path.exists():
        return _error(404, "POINTS_NOT_READY", "점군이 아직 없습니다.")
    return FileResponse(path, media_type="application/octet-stream")
