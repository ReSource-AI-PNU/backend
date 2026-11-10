"""① 식별·분류 모델 인터페이스 (backend#8). 백엔드는 이 모듈의 get_detector() 로만 모델을 부른다.

우선순위
  1. trained  : 로컬 디렉터리(MATERIAL_MODEL_DIR, CONDITION_MODEL_DIR, DOMAIN_CLF_DIR)에 RT-DETRv2 가중치가 있으면 사용.
                가중치는 git 에 없다. scripts/fetch_models.py 가 MODEL_BLOB_URL(Azure Blob SAS) 에서 받아 온다.
  2. zeroshot : 가중치가 없고 Azure OpenAI 설정이 있으면 멀티모달 제로샷(backend#4).
  3. 둘 다 없으면 ModelUnavailable -> API 는 503 으로 답한다.

반환값은 ml 내부 결과(dict, app.ml.internal.ClassifyResult). 계약(app.schemas.ClassifyResult) 변환은 app.ml.contract 가 맡는다.
학습 코드는 이 저장소에 없다(별도 학습 저장소). 여기는 추론만.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from app.core.config import settings

log = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    pass


class BaseDetector:
    name = "base"
    version = ""

    def classify(self, src: str | list[str], fps: float = 4.0) -> dict:
        raise NotImplementedError

    def status(self) -> dict:
        return {"backend": self.name, "version": self.version}


class TrainedDetector(BaseDetector):
    """RT-DETRv2 재질 모델 + 상태(이물) 모델 (+ 도메인 게이트). 프로세스당 한 번 로드."""

    name = "trained"

    def __init__(self, material_dir: str, condition_dir: str | None, domain_clf_dir: str | None, fps: float = 4.0):
        from app.ml.track import Detector  # torch 는 여기서만 import (ml extra 없이도 서버는 뜨게)

        self.mat = Detector(material_dir)
        self.cond = Detector(condition_dir) if condition_dir and Path(condition_dir).exists() else None
        if domain_clf_dir and (Path(domain_clf_dir) / "clf.pkl").exists():
            os.environ.setdefault("DOMAIN_CLF_DIR", domain_clf_dir)
        self.fps = fps
        self.version = f"{self.mat.version}+{self.cond.version if self.cond else 'nocond'}"

    def classify(self, src, fps: float | None = None, out_dir: str | None = None) -> dict:
        from app.ml.track import run

        tmp = None
        if isinstance(src, (list, tuple)):
            if len(src) == 1 and Path(src[0]).suffix.lower() in (".mp4", ".mov", ".avi", ".mkv"):
                src = src[0]
            else:
                tmp = Path(tempfile.mkdtemp(prefix="classify_"))
                for i, p in enumerate(src):
                    shutil.copy(p, tmp / f"{i:03d}{Path(p).suffix.lower()}")
                src = str(tmp)
        try:
            return run(self.mat, src, out_dir, fps or self.fps, None, self.cond, save_vis=out_dir is not None)
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def status(self) -> dict:
        return {"backend": self.name, "version": self.version, "material_dir": str(self.mat.dir), "condition": self.cond is not None}


class ZeroShotDetector(BaseDetector):
    """Azure OpenAI 멀티모달 제로샷 (backend#4). 학습 모델이 없을 때의 대체 경로. 같은 내부 결과 형식을 돌려준다."""

    name = "zeroshot"
    version = "azure-openai"
    MATERIALS = ("plastic", "plastic_film", "metal", "wood", "paper", "other")

    def __init__(self):
        if not (settings.azure_openai_endpoint and settings.azure_openai_api_key and settings.azure_openai_deployment):
            raise ModelUnavailable("Azure OpenAI 설정 없음 (AZURE_OPENAI_ENDPOINT/API_KEY/DEPLOYMENT)")

    @staticmethod
    def _frames(src, max_frames=6) -> list[bytes]:
        import cv2

        p = Path(src)
        files = sorted(p.glob("*.jp*g")) + sorted(p.glob("*.png")) if p.is_dir() else [p]
        out = []
        for f in files:
            if f.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv"):
                cap = cv2.VideoCapture(str(f)); n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                for idx in [int(n * k / max_frames) for k in range(max_frames)]:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx); ok, fr = cap.read()
                    if ok:
                        out.append(cv2.imencode(".jpg", cv2.resize(fr, (768, int(768 * fr.shape[0] / fr.shape[1]))))[1].tobytes())
            else:
                im = cv2.imread(str(f))
                if im is not None:
                    out.append(cv2.imencode(".jpg", cv2.resize(im, (768, int(768 * im.shape[0] / im.shape[1]))))[1].tobytes())
            if len(out) >= max_frames:
                break
        return out

    def classify(self, src, fps: float | None = None) -> dict:
        import httpx

        t0 = time.time()
        if isinstance(src, (list, tuple)):
            tmp = Path(tempfile.mkdtemp(prefix="zs_"))
            for i, p in enumerate(src):
                shutil.copy(p, tmp / f"{i:03d}{Path(p).suffix.lower()}")
            src = str(tmp)
        frames = self._frames(src)
        prompt = (
            "You are inspecting industrial waste photos from a Korean factory. Return JSON only: "
            '{"items":[{"material":one of plastic|plastic_film|metal|wood|paper|other,"count":int,"area_ratio":0-1,"confidence":0-1,'
            '"boxes":[[x,y,w,h] normalized 0-1]}],"contaminants":[{"label":"tape|label|metal insert|char|dirt","box":[x,y,w,h],"ratio":0-1}],'
            '"primary":"material","notes":"short"}'
        )
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}} for b in frames
        ]
        url = f"{settings.azure_openai_endpoint.rstrip('/')}/openai/deployments/{settings.azure_openai_deployment}/chat/completions?api-version=2024-10-21"
        r = httpx.post(url, headers={"api-key": settings.azure_openai_api_key}, timeout=60,
                       json={"messages": [{"role": "user", "content": content}], "response_format": {"type": "json_object"}, "max_tokens": 800})
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        items, dets = [], []
        KO = {"plastic": "폐플라스틱(합성수지)", "plastic_film": "포장 필름", "metal": "금속 스크랩", "wood": "폐목재", "paper": "폐지·포장재", "other": "기타(유리·전지)"}
        for it in data.get("items", []):
            m = it.get("material", "other")
            items.append({"material": m, "label_ko": KO.get(m, m), "count_estimate": int(it.get("count", 1)), "area_ratio": float(it.get("area_ratio", 0)),
                          "frame_coverage": float(it.get("area_ratio", 0)), "confidence": float(it.get("confidence", 0.5)),
                          "contaminated_ratio": None, "multi_material_ratio": None})
            for b in it.get("boxes", []):
                dets.append({"id": f"d{len(dets)}", "label": m, "kind": "item", "confidence": float(it.get("confidence", 0.5)), "box": [float(v) for v in b][:4], "ratio": None})
        for c in data.get("contaminants", []):
            dets.append({"id": f"c{len(dets)}", "label": c.get("label", "이물질"), "kind": "contaminant", "confidence": 0.5, "box": [float(v) for v in c.get("box", [0, 0, 0, 0])][:4],
                         "ratio": float(c["ratio"]) if c.get("ratio") is not None else None, "ratio_basis": "면적 기준 추정"})
        items.sort(key=lambda d: -d["area_ratio"])
        primary = data.get("primary") or (items[0]["material"] if items else None)
        return {"stage": "classify", "backend": "zeroshot:azure-openai", "model_version": settings.azure_openai_deployment, "mode": "photos",
                "primary_material": primary if primary in self.MATERIALS else None, "items": items, "input_domain": None, "contamination": None,
                "frames_analyzed": len(frames), "tracks_total": 0, "needs_followup": not items, "followup_questions": [],
                "latency_sec": round(time.time() - t0, 2), "detections": dets, "rep_frame": 0}


_cache: dict = {}


def get_detector(backend: str | None = None) -> BaseDetector:
    """CLASSIFIER_BACKEND=auto|trained|zeroshot. auto 는 가중치가 있으면 trained, 없으면 zeroshot."""
    backend = backend or settings.classifier_backend
    if backend in _cache:
        return _cache[backend]
    if backend in ("auto", "trained"):
        mat = settings.material_model_dir
        if Path(mat).exists() and (Path(mat) / "config.json").exists():
            try:
                det = TrainedDetector(mat, settings.condition_model_dir, settings.domain_clf_dir)
                _cache[backend] = det
                return det
            except ImportError as e:  # torch 미설치
                if backend == "trained":
                    raise ModelUnavailable(f"ml extra 미설치: pip install '.[ml]' ({e})")
                log.warning("trained 모델을 쓸 수 없어 zeroshot 으로 대체: %s", e)
        elif backend == "trained":
            raise ModelUnavailable(f"재질 모델 디렉터리 없음: {mat}. scripts/fetch_models.py 또는 MATERIAL_MODEL_DIR 확인")
    det = ZeroShotDetector()
    _cache[backend] = det
    return det
