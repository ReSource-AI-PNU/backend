"""탐지 채점 서버(app/ml/score_api.py): 키 검사, 입력 검사, v3 탐지 클라이언트와의 계약."""

import base64

import pytest
from fastapi.testclient import TestClient

from app.ml import score_api
from app.v3.intake.detector import DetectorClient

KEY = "test-detector-key-0123456789"
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeDetector:
    def __init__(self):
        self.seen = []

    def classify(self, src):
        self.seen.append(src)
        import numpy as np

        return {
            "stage": "classify",
            "backend": "trained:test",
            "model_version": "v3-test",
            "mode": "photos",
            "primary_material": "plastic",
            "items": [{"material": "plastic", "area_ratio": np.float32(0.5), "confidence": 0.9, "count_estimate": 1}],
            "contamination": {"overall_ratio": 0.06, "reliable": True, "note": ""},
            "detections": [{"id": "d0", "label": "plastic", "kind": "item", "confidence": 0.9, "box": [0, 0, 1, 1], "ratio": None}],
            "frames_analyzed": 1,
            "tracks_total": 0,
            "rep_frame": 0,
            "input_domain": None,
            "latency_sec": 0.1,
        }


@pytest.fixture
def fake(monkeypatch):
    detector = FakeDetector()
    monkeypatch.setenv("DETECTOR_API_KEY", KEY)
    monkeypatch.setattr(score_api, "get_detector", lambda: detector)
    return detector


def body(filename="a.png", data=PNG_1PX):
    return {"input_data": {"filename": filename, "media_type": "image", "content_base64": base64.b64encode(data).decode()}}


def test_rejects_requests_without_the_key(fake):
    client = TestClient(score_api.app)
    assert client.post("/score", json=body()).status_code == 401
    assert client.post("/score", json=body(), headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert fake.seen == []


def test_refuses_everything_when_no_key_is_set(monkeypatch):
    monkeypatch.delenv("DETECTOR_API_KEY", raising=False)
    client = TestClient(score_api.app)
    assert client.post("/score", json=body(), headers={"Authorization": "Bearer anything"}).status_code == 503


def test_checks_the_input(fake):
    client = TestClient(score_api.app)
    auth = {"Authorization": f"Bearer {KEY}"}
    assert client.post("/score", json=body("notes.txt"), headers=auth).status_code == 415
    bad = body()
    bad["input_data"]["content_base64"] = "not base64!"
    assert client.post("/score", json=bad, headers=auth).status_code == 400
    assert client.post("/score", json=body(data=b""), headers=auth).status_code == 400


def test_v3_detector_client_reads_the_answer(fake):
    client = TestClient(score_api.app)

    def transport(method, url, headers, payload):
        response = client.request(method, "/score", headers=headers, content=payload)
        return response.status_code, dict(response.headers), response.content

    result = DetectorClient("http://detector/score", KEY, transport).detect(PNG_1PX, "defect.png")
    assert result["mode"] == "photos"
    assert result["framesAnalyzed"] == 1
    assert fake.seen and fake.seen[0].endswith("input.png")


def test_other_photo_formats_are_converted_to_png(fake):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    ok, encoded = cv2.imencode(".bmp", np.zeros((4, 4, 3), dtype=np.uint8))
    assert ok
    client = TestClient(score_api.app)
    response = client.post("/score", json=body("photo.bmp", encoded.tobytes()), headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 200
    assert fake.seen[-1].endswith("input.png")
