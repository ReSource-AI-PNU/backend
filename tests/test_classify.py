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
    name = "stub"
    version = "test"

    def classify(self, src, fps=None):
        return {
            "stage": "classify",
            "backend": "stub",
            "model_version": "test",
            "mode": "photos",
            "primary_material": "paper",
            "items": [
                {
                    "material": "paper",
                    "label_ko": "폐지",
                    "count_estimate": 1,
                    "area_ratio": 0.5,
                    "frame_coverage": 0.2,
                    "confidence": 0.8,
                },
                {
                    "material": "metal",
                    "label_ko": "금속",
                    "count_estimate": 2,
                    "area_ratio": 0.3,
                    "frame_coverage": 0.1,
                    "confidence": 0.9,
                },
            ],
            "input_domain": "aihub137_field",
            "contamination": {
                "overall_ratio": 0.12,
                "multi_material_ratio": 0.0,
                "judged_objects": 3,
                "reliable": False,
                "note": "현장 사진: 의사 라벨",
            },
            "frames_analyzed": 1,
            "tracks_total": 0,
            "needs_followup": False,
            "followup_questions": [],
            "latency_sec": 0.4,
            "detections": [
                {
                    "id": "d0",
                    "label": "metal",
                    "kind": "item",
                    "confidence": 0.9,
                    "box": [0.3, 0.4, 0.3, 0.2],
                    "ratio": 0.06,
                },
                {
                    "id": "c0",
                    "label": "이물질",
                    "kind": "contaminant",
                    "confidence": 0.6,
                    "box": [0.32, 0.42, 0.05, 0.03],
                    "ratio": 0.03,
                    "ratio_basis": "면적 기준 추정",
                },
            ],
            "rep_frame": 0,
        }

    def status(self):
        return {"backend": "stub", "version": "test"}


def test_origin_scrap_not_film():
    assert hits("금속 스크랩", PCR_TERMS) == []
    assert estimate_origin("금속 스크랩", "프레스 라인")["origin"] == "PIR"
    assert estimate_origin("원자재 포장 필름", "원료 입고")["origin"] == "PCR"


def test_classify_stream_validates_contract():
    out = agent.classify_stream(
        ["x.jpg"],
        stream_id="st_metal",
        name="금속 스크랩",
        process="프레스 라인",
        detector=StubDetector(),
    )
    assert isinstance(out.classify, ClassifyResult)
    assert out.classify.item == "금속 스크랩"  # 흐름 이름의 재질이 면적 1위(종이)보다 우선
    assert out.classify.material == "미확인"  # '스크랩'에서 LDPE 를 추정하지 않는다
    assert (
        out.classify.detections[1].kind == "contaminant"
        and out.classify.detections[0].source == "ai"
    )
    assert out.origin == "PIR" and out.origin_source == "ai"
    assert all(
        isinstance(q, Question) and q.step == "classify" and q.stream_id == "st_metal"
        for q in out.questions
    )
    js = out.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert "singleMaterial" in js["classify"] and "originSource" in js  # camelCase


def test_classify_api_with_stub(monkeypatch):
    monkeypatch.setattr(agent, "get_detector", lambda: StubDetector())
    client = TestClient(app)
    r = client.post(
        "/api/v1/classify",
        data={"stream_id": "s1", "name": "불량 성형품", "process": "검사 공정"},
        files=[("files", ("a.jpg", io.BytesIO(b"\xff\xd8\xff\xd9"), "image/jpeg"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["classify"]["item"] in ("폐플라스틱", "금속 스크랩", "폐목재", "포장재")
    assert any("수지" in q["text"] or "월 발생량" in q["text"] for q in body["questions"])


def test_classify_api_rejects_unknown_ext(monkeypatch):
    monkeypatch.setattr(agent, "get_detector", lambda: StubDetector())
    r = TestClient(app).post(
        "/api/v1/classify",
        data={"stream_id": "s1"},
        files=[("files", ("a.txt", io.BytesIO(b"x"), "text/plain"))],
    )
    assert r.status_code == 415


def test_contaminant_names_assigned_only_to_contaminants():
    from app.ml.contaminant_names import assign_names

    dets = [
        {"id": "d0", "kind": "item", "label": "plastic", "box": [0.1, 0.1, 0.5, 0.5]},
        {"id": "c0", "kind": "contaminant", "label": "이물질", "box": [0.2, 0.2, 0.1, 0.1]},
        {"id": "c1", "kind": "contaminant", "label": "이물질", "box": [0.8, 0.8, 0.1, 0.1]},
    ]
    preds = [
        {"name": "테이프", "score": 0.4, "xyxy": [200, 200, 300, 300]},
        {"name": "라벨", "score": 0.6, "xyxy": [190, 190, 310, 310]},
        {"name": "탄화물", "score": 0.9, "xyxy": [0, 0, 50, 50]},
    ]
    out = assign_names(preds, dets, 1000, 1000)
    assert out[0]["label"] == "plastic" and "name_source" not in out[0]
    assert (
        out[1]["label"] == "라벨" and out[1]["name_source"] == "owlv2"
    )  # 겹치는 것 중 점수 높은 이름
    assert out[2]["label"] == "이물질"  # 안 겹치면 그대로


def _raw(items, primary):
    return {
        "stage": "classify",
        "backend": "stub",
        "model_version": "test",
        "mode": "photos",
        "primary_material": primary,
        "items": [
            {
                "material": m,
                "label_ko": m,
                "count_estimate": 1,
                "area_ratio": a,
                "frame_coverage": a,
                "confidence": c,
            }
            for m, a, c in items
        ],
        "input_domain": "aihub137_field",
        "contamination": None,
        "frames_analyzed": 1,
        "tracks_total": 0,
        "needs_followup": False,
        "followup_questions": [],
        "latency_sec": 0.1,
        "detections": [],
        "rep_frame": 0,
    }


def _texts(out):
    return [q["text"] for q in out["questions"]]


def test_film_name_routes_to_packaging_even_with_v3_model():
    from app.ml.contract import to_stream_classify

    # v3 모델은 plastic 하나뿐이다. 사람이 쓴 흐름 이름 '스트레치 필름'이 포장재 경로를 정한다
    out = to_stream_classify(
        _raw([("plastic", 0.7, 0.9)], "plastic"),
        stream_id="s",
        name="스트레치 필름",
        process="포장",
    )
    assert out["classify"]["item"] == "포장재" and out["classify"]["material"] == "LDPE"
    assert out["classify"]["confidence"] == 0.9
    assert not any("필름·비닐로 보입니다" in t for t in _texts(out))  # 이름으로 이미 확인됨


def test_v5_split_plastic_beats_paper_and_asks_film_confirmation():
    from app.ml.contract import to_stream_classify

    # 경질 0.25 + 필름 0.35 = 플라스틱 0.6 > 종이 0.4. 면적이 큰 필름이 1위, 이름에 단서가 없으니 확인 질문
    out = to_stream_classify(
        _raw(
            [("paper", 0.4, 0.8), ("plastic_film", 0.35, 0.6), ("plastic_rigid", 0.25, 0.9)],
            "paper",
        ),
        stream_id="s",
        name="라인 폐기물",
    )
    assert out["classify"]["item"] == "포장재"
    assert (
        out["classify"]["material"] == "미확인" and "마대" in out["classify"]["detail"]
    )  # 수지는 단정하지 않음
    assert any("필름·비닐로 보입니다" in t for t in _texts(out))
    assert next(q for q in out["questions"] if "수지" in q["text"])["suggested"] == "LDPE"
    ClassifyResult.model_validate(out["classify"])


def test_sprue_name_forces_rigid_over_film_prediction():
    from app.ml.contract import to_stream_classify

    out = to_stream_classify(
        _raw([("plastic_film", 0.6, 0.5)], "plastic_film"),
        stream_id="s",
        name="스프루·러너",
        process="사출 1라인",
    )
    assert out["classify"]["item"] == "폐플라스틱" and out["classify"]["material"] == "PP"
    assert not any("필름·비닐로 보입니다" in t for t in _texts(out))


def test_name_photo_conflict_asks_item_and_drops_resin_hint():
    from app.ml.contract import to_stream_classify

    # 흙 묻은 플라스틱 관을 모델이 목재로 본 실제 사례. 이름은 플라스틱(스티로폼)을 말한다
    out = to_stream_classify(
        _raw([("wood", 0.4, 0.58)], "wood"), stream_id="s", name="스티로폼 완충재", process="입고"
    )
    assert (
        out["classify"]["item"] == "폐목재" and out["classify"]["material"] == "미확인"
    )  # 목재에 PS 를 붙이지 않음
    q = next(q for q in out["questions"] if "흐름 이름은" in q["text"])
    assert q["suggested"] == "폐플라스틱"


def test_film_detail_not_duplicated():
    from app.ml.contract import to_stream_classify

    out = to_stream_classify(
        _raw([("plastic_film", 0.6, 0.9)], "plastic_film"), stream_id="s", name="스트레치 필름"
    )
    assert out["classify"]["detail"] == "LDPE 필름(추정)"


def _stream(out, sid="s"):
    return {
        "id": sid,
        "name": "x",
        "process": "y",
        "media": [],
        "classify": out["classify"],
        "origin": out["origin"],
        "originSource": out["originSource"],
        "monthlyKg": 0,
        "monthlyKgConfirmed": False,
    }


def test_question_ids_are_stable_and_origin_always_asked():
    from app.ml.contract import to_stream_classify

    out = to_stream_classify(
        _raw([("plastic", 0.7, 0.9)], "plastic"),
        stream_id="st1",
        name="스프루·러너",
        process="사출 1라인",
    )
    ids = [q["id"] for q in out["questions"]]
    assert "classify-origin:st1" in ids and "classify-kg:st1" in ids and "classify-resin:st1" in ids
    assert len(ids) == len(set(ids))
    for q in out["questions"]:
        Question.model_validate(q)


def test_scrap_names_never_become_film():
    from app.ml.contract import to_stream_classify

    # 프런트 목업에서 실제로 난 버그: '스크랩'의 '랩'이 필름으로 잡힘
    for name, items, item in [
        ("금속 스크랩", [("metal", 0.6, 0.9), ("plastic", 0.3, 0.8)], "금속 스크랩"),
        ("플라스틱 스크랩", [("plastic", 0.7, 0.9)], "폐플라스틱"),
        ("플라스틱 스크랩", [("plastic_film", 0.5, 0.6), ("plastic_rigid", 0.2, 0.9)], "포장재"),
    ]:
        out = to_stream_classify(_raw(items, items[0][0]), stream_id="s", name=name)
        assert out["classify"]["item"] == item, (name, items)
        assert out["classify"]["material"] != "LDPE" and "LDPE" not in out["classify"]["detail"], (
            name,
            items,
        )
    # 반대로 '랩'이 단어로 오면 필름
    out = to_stream_classify(
        _raw([("plastic", 0.7, 0.9)], "plastic"), stream_id="s", name="랩 필름"
    )
    assert out["classify"]["item"] == "포장재" and out["classify"]["material"] == "LDPE"


def test_film_split_keeps_single_material_true():
    from app.ml.contract import to_stream_classify

    # 필름만: 수지 미확인이어도 singleMaterial=True (false 면 묶음이 아니라 열분해로 감)
    out = to_stream_classify(
        _raw([("plastic_film", 0.8, 0.6)], "plastic_film"), stream_id="s", name="라인 폐기물"
    )
    assert out["classify"]["singleMaterial"] is True and out["classify"]["material"] == "미확인"
    # v5 가 한 더미를 경질 0.3 + 필름 0.4 로 나눠도 플라스틱 하나로 센다
    out = to_stream_classify(
        _raw([("plastic_film", 0.4, 0.6), ("plastic_rigid", 0.3, 0.8)], "plastic_film"),
        stream_id="s",
        name="라인 폐기물",
    )
    assert out["classify"]["singleMaterial"] is True
    assert not any(q["id"].startswith("classify-mixed") for q in out["questions"])


def test_answers_fill_film_resin_origin_in_any_order():
    from app.ml.contract import apply_classify_answer, to_stream_classify

    out = to_stream_classify(
        _raw([("plastic_film", 0.8, 0.6)], "plastic_film"),
        stream_id="s",
        name="라인 폐기물",
        process="출하",
    )
    st = _stream(out)
    a, c1 = apply_classify_answer(st, "classify-film:s", "LDPE 필름·비닐")
    a, _ = apply_classify_answer(a, "classify-origin:s", "입고 자재의 포장재 (PCR)")
    a, _ = apply_classify_answer(a, "classify-kg:s", "400")
    assert (
        a["classify"]["item"] == "포장재" and a["classify"]["material"] == "LDPE"
    )  # 매칭 정규식 /LDPE|PE/ 에 걸림
    assert (
        a["origin"] == "PCR"
        and a["originSource"] == "human"
        and a["monthlyKg"] == 400
        and a["monthlyKgConfirmed"]
    )
    assert any(c["field"] == "classify.material" and c["after"] == "LDPE" for c in c1)
    # 순서가 달라도 같은 결과: 수지 PP 를 먼저 답하면 필름 답이 덮어쓰지 않는다
    b, _ = apply_classify_answer(st, "classify-resin:s", "PP")
    b, _ = apply_classify_answer(b, "classify-film:s", "LDPE 필름·비닐")
    c, _ = apply_classify_answer(st, "classify-film:s", "LDPE 필름·비닐")
    c, _ = apply_classify_answer(c, "classify-resin:s", "PP")
    assert b["classify"]["material"] == c["classify"]["material"] == "PP"
    # PP 마대
    d, _ = apply_classify_answer(st, "classify-film:s", "PP 마대·직조")
    assert d["classify"]["item"] == "포장재" and d["classify"]["material"] == "PP"


def test_classify_answer_api_validates_stream_and_corrections():
    from app.ml.contract import to_stream_classify
    from app.schemas import Correction, Stream

    out = to_stream_classify(
        _raw([("plastic_film", 0.8, 0.6)], "plastic_film"), stream_id="s", name="라인 폐기물"
    )
    client = TestClient(app)
    r = client.post(
        "/api/v1/classify/answer",
        json={"stream": _stream(out), "questionId": "classify-film:s", "answer": "LDPE 필름·비닐"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    Stream.model_validate(body["stream"])
    [Correction.model_validate(c) for c in body["corrections"]]
    assert body["stream"]["classify"]["material"] == "LDPE"
    r = client.post(
        "/api/v1/classify/answer",
        json={"stream": _stream(out), "questionId": "classify-film:s", "answer": "모름"},
    )
    assert r.status_code == 422
