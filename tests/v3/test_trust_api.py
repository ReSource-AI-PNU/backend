"""공개 신뢰 페이지용 GET /api/v3/trust: 인증 없이 최신 평가 요약을 돌려준다."""

import json

from fastapi.testclient import TestClient

from app.main import app
from app.v3 import api


def test_trust_is_public_and_returns_latest_summary(monkeypatch):
    monkeypatch.setenv("V3_AUTH", "bff")
    monkeypatch.setenv("V3_BFF_KEY", "a-key-that-the-test-never-sends-0123")
    response = TestClient(app).get("/api/v3/trust")
    assert response.status_code == 200
    assert response.json() == json.loads(api.TRUST_FILE.read_text(encoding="utf-8"))


def test_trust_unavailable_is_a_plain_error(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "TRUST_FILE", tmp_path / "missing.json")
    response = TestClient(app).get("/api/v3/trust")
    assert response.status_code == 503
    assert "평가 결과" in response.text
