"""① 식별 API·어댑터 테스트. torch 없이 돈다(스텁 탐지기)."""
import io
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agents import classify as agent
from app.main import app
from app.ml.origin import PCR_TERMS, estimate_origin, hits
from app.schemas import ClassifyResult, Question


class StubDetector:
    name = "stub"; version = "test"

    def classify(self, src, fps=None):
        return {"stage": "classify", "backend": "stub", "model_version": "test", "mode": "photos", "primary_material": "paper",
                "items": [{"material": "paper", "label_ko": "폐지", "count_estimate": 1, "area_ratio": 0.5, "frame_coverage": 0.2, "confidence": 0.8},
                          {"material": "metal", "label_ko": "금속", "count_estimate": 2, "area_ratio": 0.3, "frame_coverage": 0.1, "confidence": 0.9}],
                "input_domain": "aihub137_field", "contamination": {"overall_ratio": 0.12, "multi_material_ratio": 0.0, "judged_objects": 3, "reliable": False, "note": "현장 사진: 의사 라벨"},
                "frames_analyzed": 1, "tracks_total": 0, "needs_followup": False, "followup_questions": [], "latency_sec": 0.4,
                "detections": [{"id": "d0", "label": "metal", "kind": "item", "confidence": 0.9, "box": [0.3, 0.4, 0.3, 0.2], "ratio": 0.06},
                               {"id": "c0", "label": "이물질", "kind": "contaminant", "confidence": 0.6, "box": [0.32, 0.42, 0.05, 0.03], "ratio": 0.03, "ratio_basis": "면적 기준 추정"}],
                "rep_frame": 0}

    def status(self):
        return {"backend": "stub", "version": "test"}


def test_origin_scrap_not_film():
    assert hits("금속 스크랩", PCR_TERMS) == []
    assert estimate_origin("금속 스크랩", "프레스 라인")["origin"] == "PIR"
    assert estimate_origin("원자재 포장 필름", "원료 입고")["origin"] == "PCR"


def test_classify_stream_validates_contract():
    out = agent.classify_stream(["x.jpg"], stream_id="st_metal", name="금속 스크랩", process="프레스 라인", detector=StubDetector())
    assert isinstance(out.classify, ClassifyResult)
    assert out.classify.item == "금속 스크랩"            # 흐름 이름의 재질이 면적 1위(종이)보다 우선
    assert out.classify.material == "미확인"             # '스크랩'에서 LDPE 를 추정하지 않는다
    assert out.classify.detections[1].kind == "contaminant" and out.classify.detections[0].source == "ai"
    assert out.origin == "PIR" and out.origin_source == "ai"
    assert all(isinstance(q, Question) and q.step == "classify" and q.stream_id == "st_metal" for q in out.questions)
    js = out.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert "singleMaterial" in js["classify"] and "originSource" in js  # camelCase


def test_classify_api_with_stub(monkeypatch):
    monkeypatch.setattr(agent, "get_detector", lambda: StubDetector())
    client = TestClient(app)
    r = client.post("/api/v1/classify", data={"stream_id": "s1", "name": "불량 성형품", "process": "검사 공정"},
                    files=[("files", ("a.jpg", io.BytesIO(b"\xff\xd8\xff\xd9"), "image/jpeg"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["classify"]["item"] in ("폐플라스틱", "금속 스크랩", "폐목재", "포장재")
    assert any("수지" in q["text"] or "월 발생량" in q["text"] for q in body["questions"])


def test_classify_api_rejects_unknown_ext(monkeypatch):
    monkeypatch.setattr(agent, "get_detector", lambda: StubDetector())
    r = TestClient(app).post("/api/v1/classify", data={"stream_id": "s1"}, files=[("files", ("a.txt", io.BytesIO(b"x"), "text/plain"))])
    assert r.status_code == 415


def test_contaminant_names_assigned_only_to_contaminants():
    from app.ml.contaminant_names import assign_names

    dets = [{"id": "d0", "kind": "item", "label": "plastic", "box": [0.1, 0.1, 0.5, 0.5]},
            {"id": "c0", "kind": "contaminant", "label": "이물질", "box": [0.2, 0.2, 0.1, 0.1]},
            {"id": "c1", "kind": "contaminant", "label": "이물질", "box": [0.8, 0.8, 0.1, 0.1]}]
    preds = [{"name": "테이프", "score": 0.4, "xyxy": [200, 200, 300, 300]}, {"name": "라벨", "score": 0.6, "xyxy": [190, 190, 310, 310]},
             {"name": "탄화물", "score": 0.9, "xyxy": [0, 0, 50, 50]}]
    out = assign_names(preds, dets, 1000, 1000)
    assert out[0]["label"] == "plastic" and "name_source" not in out[0]
    assert out[1]["label"] == "라벨" and out[1]["name_source"] == "owlv2"  # 겹치는 것 중 점수 높은 이름
    assert out[2]["label"] == "이물질"  # 안 겹치면 그대로
