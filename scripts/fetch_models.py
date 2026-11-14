"""모델 가중치 내려받기. MODEL_BLOB_URL(Azure Blob SAS 또는 공개 URL)의 tar.gz 를 models/ 에 푼다.

tar 안 구조: material/ (config.json, model.safetensors, threshold.json), condition/ (… + field_ok.json), domain_clf/ (clf.pkl)
URL 이 없거나 이미 models/material/config.json 이 있으면 아무것도 하지 않는다(제로샷 대체 또는 기존 가중치 사용).
"""
import os
import sys
import tarfile
import tempfile
from pathlib import Path

url = os.getenv("MODEL_BLOB_URL", "")
dst = Path(os.getenv("MODELS_DIR", "models"))
if (dst / "material" / "config.json").exists():
    print(f"models already present at {dst}")
    sys.exit(0)
if not url:
    print("MODEL_BLOB_URL 없음 -> 학습 모델 없이 시작 (CLASSIFIER_BACKEND=auto 면 Azure OpenAI 제로샷)")
    sys.exit(0)
import httpx

dst.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes(1024 * 1024):
            tmp.write(chunk)
    path = tmp.name
with tarfile.open(path) as tf:
    tf.extractall(dst, filter="data")
os.unlink(path)
print("models fetched:", sorted(p.name for p in dst.iterdir()))
