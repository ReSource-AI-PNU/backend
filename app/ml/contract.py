"""ml 내부 ClassifyResult -> 설계안 v2 데이터 계약(backend app/schemas.py = 프런트 src/lib/session/types.ts) 어댑터

계약의 원본은 backend #1 schemas.py 다. 이 모듈은 그 모양(camelCase)으로 변환만 한다.
  Stream.classify = ClassifyResult { item, detail, material, singleMaterial, confidence, estimatedKg, frames, detections[], model, durationMs }
  Stream.origin / originSource, Question[] (step="classify", streamId)

재질 -> 품목(WasteItem 4종) 매핑. 필름(plastic_film)은 '포장재'로 보내야 앱에서 단지 묶음 매각 경로를 탄다.
"""
import uuid

from app.ml.origin import _match, estimate_origin, tokens

ITEM_OF = {"plastic": "폐플라스틱", "plastic_rigid": "폐플라스틱", "plastic_film": "포장재", "metal": "금속 스크랩", "wood": "폐목재", "paper": "포장재"}
DETAIL_OF = {"plastic": "플라스틱", "plastic_rigid": "경질 플라스틱", "plastic_film": "LDPE 필름(추정)", "metal": "금속 스크랩", "wood": "폐목재", "paper": "골판지·종이 포장재"}
LABEL_KO = {"plastic": "플라스틱", "plastic_rigid": "경질 플라스틱", "plastic_film": "필름", "metal": "금속", "wood": "목재", "paper": "종이·골판지", "other": "유리·전지"}
KG_PER_OBJECT = {"plastic": 2.0, "plastic_rigid": 2.0, "plastic_film": 0.5, "metal": 5.0, "wood": 8.0, "paper": 1.0}  # 수량 기반 거친 추정, 월 발생량은 사용자 확정값을 쓴다
MATERIAL_OPTIONS = ["PP", "PE(HDPE)", "LDPE", "PET", "PS", "ABS", "PVC", "혼합·모름"]
MATERIAL_HINT = {"사출": "PP", "필름": "LDPE", "스트레치": "LDPE", "랩": "LDPE", "병": "PET", "페트": "PET", "발포": "PS", "스티로폼": "PS"}


def _qid():
    return "q" + uuid.uuid4().hex[:8]


def to_stream_classify(res: dict, *, stream_id: str, name: str = "", process: str = "", material_doc_text: str = "",
                       material_hint: str | None = None) -> dict:
    """res: ml.infer.track.run() 결과(dict). 반환: {"classify": {...}, "origin": "PIR|PCR", "originSource": "ai", "questions": [...], "meta": {...}}"""
    items = res.get("items", [])
    primary = res.get("primary_material")
    dets = res.get("detections", [])
    # 흐름 이름에 재질 단어가 있고 그 재질이 탐지되었으면 면적 1위 대신 그 재질을 품목으로 (캔 아래 종이박스가 면적을 차지하는 사고 방지)
    NAME_MAT = {"금속": "metal", "스크랩": "metal", "캔": "metal", "철": "metal", "알루미늄": "metal", "목재": "wood", "나무": "wood", "팔레트": "wood",
                "골판지": "paper", "박스": "paper", "상자": "paper", "종이": "paper", "필름": "plastic_film", "랩": "plastic_film", "비닐": "plastic_film",
                "플라스틱": "plastic", "스프루": "plastic", "러너": "plastic", "퍼지": "plastic", "성형품": "plastic", "사출": "plastic"}
    detected = {i["material"] for i in items}
    named = None
    for tok in tokens(name):
        for k, v in NAME_MAT.items():
            if _match(tok, k):
                cand = v if v in detected else ("plastic" if v.startswith("plastic") and "plastic" in detected else None)
                if cand:
                    named = cand; break
        if named:
            break
    if named and named != primary:
        primary = named
    # 품목: 면적 1위 재질을 4종으로 매핑. other(유리·전지)는 대회 품목 밖 -> 되묻기
    item = ITEM_OF.get(primary)
    questions: list[dict] = []
    if item is None:
        item = "폐플라스틱"
        questions.append({"id": _qid(), "step": "classify", "streamId": stream_id, "kind": "choice", "options": ["폐플라스틱", "금속 스크랩", "폐목재", "포장재"],
                          "text": "이 흐름의 품목을 골라 주세요.", "why": "사진에서 품목을 확정하지 못했습니다(유리·전지 등 대회 품목 밖이거나 신뢰도가 낮음)."})
    # 재질(수지 종류)은 사진으로 확정하지 않는다: 힌트(공정명·명세) + 확인 질문
    mat = material_hint
    if not mat:
        for tok in tokens(name + " " + process + " " + material_doc_text):
            for k, v in MATERIAL_HINT.items():
                if _match(tok, k):
                    mat = v; break
            if mat:
                break
    if primary in ("plastic_film",):
        mat = mat or "LDPE"
    material = mat or "미확인"
    if item in ("폐플라스틱", "포장재") and primary not in ("paper",):
        questions.append({"id": _qid(), "step": "classify", "streamId": stream_id, "kind": "choice", "options": MATERIAL_OPTIONS,
                          "text": "이 흐름의 수지 종류는 무엇인가요?", "suggested": mat or "PP",
                          "why": "사진으로는 PP·PE·ABS를 가를 수 없습니다. 재투입·매각 단가가 수지에 따라 달라집니다. 원자재 구매 명세가 있으면 올려 주세요."})
    # 단일/혼합: 면적 15% 이상인 재질이 둘 이상이면 혼합 후보. 확정은 공정 정보·답변으로
    big = [i for i in items if i.get("area_ratio", 0) >= 0.15]
    single = len(big) <= 1
    if not single:
        questions.append({"id": _qid(), "step": "classify", "streamId": stream_id, "kind": "choice", "options": ["단일 재질", "여러 재질이 섞임"],
                          "text": "사진에 " + "·".join(LABEL_KO.get(b["material"], b["material"]) for b in big) + "이(가) 함께 보입니다. 한 흐름에 섞여 나오나요?",
                          "why": "섞인 흐름은 기계적 재활용이 어려워 열분해나 소각으로 갑니다.", "suggested": "여러 재질이 섞임"})
    # detections -> 계약 Detection
    detections = []
    for d in dets:
        detections.append({"id": d["id"], "label": LABEL_KO.get(d["label"], d["label"]) if d["kind"] == "item" else d["label"],
                           "kind": d["kind"], "confidence": d["confidence"], "box": d["box"], "source": "ai",
                           **({"ratio": d["ratio"]} if d.get("ratio") is not None else {})})
    # estimatedKg: 수량 × 재질별 거친 평균. 월 발생량(monthlyKg)은 별도 확정 질문
    est = sum(KG_PER_OBJECT.get(i["material"], 2.0) * i.get("count_estimate", 0) for i in items)
    conf = next((i["confidence"] for i in items if i["material"] == primary), 0.0) if primary else 0.0
    detail = f"{material if material != '미확인' else ''} {DETAIL_OF.get(primary, '')}".strip() or (name or "미확인")
    classify = {"item": item, "detail": detail, "material": material, "singleMaterial": single, "confidence": round(float(conf), 3),
                "estimatedKg": round(float(est), 1), "frames": int(res.get("frames_analyzed", 0)), "detections": detections,
                "model": {"name": res.get("backend", "trained"), "version": res.get("model_version", "")},
                "durationMs": int(float(res.get("latency_sec", 0)) * 1000)}
    org = estimate_origin(name, process, material_doc_text, dets)
    if "question" in org:
        questions.append({"id": _qid(), "streamId": stream_id, **org.pop("question")})
    # 이물질 비율 확인(현장 사진은 의사 라벨 학습이라 확인 요망)
    c = res.get("contamination")
    if c and not c.get("reliable"):
        questions.append({"id": _qid(), "step": "classify", "streamId": stream_id, "kind": "number", "unit": "%",
                          "text": "사진 기준 이물질 비율을 약 {}%로 추정했습니다(면적 기준). 실제와 비슷한가요?".format(int(c["overall_ratio"] * 100)),
                          "why": c.get("note", ""), "suggested": str(int(c["overall_ratio"] * 100))})
    # 월 발생량 확정
    questions.append({"id": _qid(), "step": "classify", "streamId": stream_id, "kind": "number", "unit": "kg",
                      "text": "이 흐름의 월 발생량은 얼마인가요?", "why": "경로별 월 손익과 트럭 적재율 계산의 기준값입니다.",
                      "suggested": str(int(est)) if est else ""})
    return {"classify": classify, "origin": org["origin"], "originSource": "ai", "originConfidence": org["confidence"],
            "originEvidence": org["evidence"], "questions": questions,
            "meta": {"input_domain": res.get("input_domain"), "contamination": c, "items": items}}
