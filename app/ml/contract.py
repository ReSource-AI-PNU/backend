"""ml 내부 ClassifyResult -> 설계안 v2 데이터 계약(backend app/schemas.py = 프런트 src/lib/session/types.ts) 어댑터

계약의 원본은 backend #1 schemas.py 다. 이 모듈은 그 모양(camelCase)으로 변환만 한다.
  Stream.classify = ClassifyResult { item, detail, material, singleMaterial, confidence, estimatedKg, frames, detections[], model, durationMs }
  Stream.origin / originSource, Question[] (step="classify", streamId)

재질 -> 품목(WasteItem 4종) 매핑. 필름(plastic_film)은 '포장재'로 보내야 앱에서 단지 묶음 매각 경로를 탄다.
"""

from app.ml.origin import _match, estimate_origin, tokens

ITEM_OF = {"plastic": "폐플라스틱", "plastic_rigid": "폐플라스틱", "plastic_film": "포장재", "metal": "금속 스크랩", "wood": "폐목재", "paper": "포장재"}
DETAIL_OF = {"plastic": "플라스틱", "plastic_rigid": "경질 플라스틱", "plastic_film": "LDPE 필름(추정)", "metal": "금속 스크랩", "wood": "폐목재", "paper": "골판지·종이 포장재"}
LABEL_KO = {"plastic": "플라스틱", "plastic_rigid": "경질 플라스틱", "plastic_film": "필름", "metal": "금속", "wood": "목재", "paper": "종이·골판지", "other": "유리·전지"}
KG_PER_OBJECT = {"plastic": 2.0, "plastic_rigid": 2.0, "plastic_film": 0.5, "metal": 5.0, "wood": 8.0, "paper": 1.0}  # 수량 기반 거친 추정, 월 발생량은 사용자 확정값을 쓴다
PLASTICS = {"plastic", "plastic_rigid", "plastic_film"}
MATERIAL_OPTIONS = ["PP", "PE(HDPE)", "LDPE", "PET", "PS", "ABS", "PVC", "혼합·모름"]
MATERIAL_HINT = {"사출": "PP", "필름": "LDPE", "스트레치": "LDPE", "랩": "LDPE", "병": "PET", "페트": "PET", "발포": "PS", "스티로폼": "PS"}


def _qid(kind: str, stream_id: str) -> str:
    """답을 흐름에 되돌려 반영할 수 있도록 고정 id: classify-<종류>:<streamId> (apply_classify_answer 참고)"""
    return f"classify-{kind}:{stream_id}"


def to_stream_classify(res: dict, *, stream_id: str, name: str = "", process: str = "", material_doc_text: str = "",
                       material_hint: str | None = None) -> dict:
    """res: ml.infer.track.run() 결과(dict). 반환: {"classify": {...}, "origin": "PIR|PCR", "originSource": "ai", "questions": [...], "meta": {...}}"""
    items = res.get("items", [])
    primary = res.get("primary_material")
    dets = res.get("detections", [])
    # 흐름 이름에 재질 단어가 있고 그 재질이 탐지되었으면 면적 1위 대신 그 재질을 품목으로 (캔 아래 종이박스가 면적을 차지하는 사고 방지)
    NAME_MAT = {"금속": "metal", "스크랩": "metal", "캔": "metal", "철": "metal", "알루미늄": "metal", "목재": "wood", "나무": "wood", "팔레트": "wood",
                "골판지": "paper", "박스": "paper", "상자": "paper", "종이": "paper", "필름": "plastic_film", "랩": "plastic_film", "비닐": "plastic_film",
                "스트레치": "plastic_film", "플라스틱": "plastic", "스프루": "plastic_rigid", "러너": "plastic_rigid", "퍼지": "plastic_rigid",
                "성형품": "plastic_rigid", "사출": "plastic_rigid", "스티로폼": "plastic_rigid", "발포": "plastic_rigid"}
    area: dict = {}
    for i in items:
        area[i["material"]] = area.get(i["material"], 0.0) + float(i.get("area_ratio", 0) or 0)
    detected = set(area)
    found_plastics = [m for m in ("plastic", "plastic_rigid", "plastic_film") if m in detected]
    # v5 모델(경질·필름 분리): 두 플라스틱 면적을 합쳐 다른 재질과 비교한다 (둘로 나뉘어 금속·종이에 밀리는 일 방지)
    plastic_area = sum(area[m] for m in found_plastics)
    if found_plastics and plastic_area >= max((a for m, a in area.items() if m not in PLASTICS), default=0.0):
        primary = max(found_plastics, key=lambda m: area[m])
    model_primary = primary
    named = None
    named_term = None  # 이름이 가리킨 재질 종류 (plastic_film/plastic_rigid 면 사람이 종류까지 말한 것)
    conflict_term = None  # 이름은 이 재질을 말하는데 사진에서 그 재질이 안 보임
    for tok in tokens(name):
        for k, v in NAME_MAT.items():
            if not _match(tok, k):
                continue
            if v in PLASTICS:
                # 흐름 이름은 사람이 쓴 값이다. 플라스틱이 보이면 이름이 말하는 종류(필름/경질)를 따른다
                if not found_plastics:
                    cand = None
                elif v == "plastic":
                    cand = primary if primary in PLASTICS else found_plastics[0]
                elif v == "plastic_rigid":
                    cand = "plastic_rigid" if "plastic_rigid" in detected else ("plastic" if "plastic" in detected else "plastic_rigid")
                else:
                    cand = "plastic_film"
            else:
                cand = v if v in detected else None
            if cand:
                named = cand; named_term = v; break
            conflict_term = conflict_term or v
        if named:
            break
    if named and named != primary:
        primary = named
    # 품목: 면적 1위 재질을 4종으로 매핑. other(유리·전지)는 대회 품목 밖 -> 되묻기
    item = ITEM_OF.get(primary)
    questions: list[dict] = []
    if item is None:
        item = "폐플라스틱"
        questions.append({"id": _qid("item", stream_id), "step": "classify", "streamId": stream_id, "kind": "choice", "options": ["폐플라스틱", "금속 스크랩", "폐목재", "포장재"],
                          "text": "이 흐름의 품목을 골라 주세요.", "why": "사진에서 품목을 확정하지 못했습니다(유리·전지 등 대회 품목 밖이거나 신뢰도가 낮음)."})
    elif named is None and conflict_term is not None and ITEM_OF.get(conflict_term) not in (None, item):
        want = ITEM_OF[conflict_term]
        questions.append({"id": _qid("item", stream_id), "step": "classify", "streamId": stream_id, "kind": "choice", "options": ["폐플라스틱", "금속 스크랩", "폐목재", "포장재"],
                          "text": f"흐름 이름은 {want} 쪽인데 사진에서는 {item}(으)로 보입니다. 어느 쪽이 맞나요?", "suggested": want,
                          "why": "현장 사진에서는 모델이 재질을 헷갈릴 수 있습니다(예: 흙 묻은 플라스틱 관을 목재로). 품목에 따라 처리 경로가 달라집니다."})
    # 재질(수지 종류)은 사진으로 확정하지 않는다: 힌트(공정명·명세) + 확인 질문
    mat = material_hint
    if not mat and primary in PLASTICS:
        for tok in tokens(name + " " + process + " " + material_doc_text):
            for k, v in MATERIAL_HINT.items():
                if _match(tok, k):
                    mat = v; break
            if mat:
                break
    # 필름으로 보여도 수지는 단정하지 않는다: 현장 '필름' 중에는 PP 직조 마대·방수포도 많다(v5 표본 확인). 추천값만 LDPE
    material = mat or "미확인"
    if item in ("폐플라스틱", "포장재") and primary not in ("paper",):
        questions.append({"id": _qid("resin", stream_id), "step": "classify", "streamId": stream_id, "kind": "choice", "options": MATERIAL_OPTIONS,
                          "text": "이 흐름의 수지 종류는 무엇인가요?", "suggested": mat or ("LDPE" if primary == "plastic_film" else "PP"),
                          "why": "사진으로는 PP·PE·ABS를 가를 수 없습니다. 재투입·매각 단가가 수지에 따라 달라집니다. 원자재 구매 명세가 있으면 올려 주세요."})
    if model_primary == "plastic_film" and named_term not in ("plastic_film", "plastic_rigid"):
        questions.append({"id": _qid("film", stream_id), "step": "classify", "streamId": stream_id, "kind": "choice", "options": FILM_OPTIONS,
                          "text": "사진에서 필름·비닐로 보입니다. 맞나요?", "suggested": "LDPE 필름·비닐",
                          "why": "필름은 포장재로 묶어 파는 경로, 경질은 재투입·매각 경로를 탑니다. 사진 기반 필름 판별은 아직 정확도가 낮아 확인이 필요합니다."})
    # 단일/혼합: 면적 15% 이상인 재질이 둘 이상이면 혼합 후보. 확정은 공정 정보·답변으로
    groups: dict = {}
    for i in items:
        g = "plastic" if i["material"] in PLASTICS else i["material"]
        groups[g] = groups.get(g, 0.0) + float(i.get("area_ratio", 0) or 0)
    big = [{"material": g, "area_ratio": a} for g, a in groups.items() if a >= 0.15]
    single = len(big) <= 1
    if not single:
        questions.append({"id": _qid("mixed", stream_id), "step": "classify", "streamId": stream_id, "kind": "choice", "options": ["단일 재질", "여러 재질이 섞임"],
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
    conf = next((i["confidence"] for i in items if i["material"] == primary), None) if primary else None
    if conf is None:
        conf = max((i["confidence"] for i in items if i["material"] in PLASTICS), default=0.0) if primary in PLASTICS else 0.0
    if primary == "plastic_film":
        detail = "LDPE 필름(추정)" if material == "LDPE" else f"{material + ' ' if material != '미확인' else ''}필름·비닐·마대(추정)"
    else:
        detail = f"{material if material != '미확인' else ''} {DETAIL_OF.get(primary, '')}".strip() or (name or "미확인")
    classify = {"item": item, "detail": detail, "material": material, "singleMaterial": single, "confidence": round(float(conf), 3),
                "estimatedKg": round(float(est), 1), "frames": int(res.get("frames_analyzed", 0)), "detections": detections,
                "model": {"name": res.get("backend", "trained"), "version": res.get("model_version", "")},
                "durationMs": int(float(res.get("latency_sec", 0)) * 1000)}
    org = estimate_origin(name, process, material_doc_text, dets)
    if "question" in org:  # origin.py 가 항상 붙인다 (AI 추정은 담당자 확인 필수)
        questions.append({"id": _qid("origin", stream_id), "streamId": stream_id, **org.pop("question")})
    # 이물질 비율 확인(현장 사진은 의사 라벨 학습이라 확인 요망)
    c = res.get("contamination")
    if c and not c.get("reliable"):
        questions.append({"id": _qid("contamination", stream_id), "step": "classify", "streamId": stream_id, "kind": "number", "unit": "%",
                          "text": ("사진에서는 이물질이 거의 보이지 않습니다. 실제로도 이물질이 없나요? 있다면 대략 몇 %인가요?" if c["overall_ratio"] < 0.01 else
                                   "사진 기준 이물질 비율을 약 {}%로 추정했습니다(면적 기준). 실제와 비슷한가요?".format(int(c["overall_ratio"] * 100))),
                          "why": c.get("note", ""), "suggested": str(int(c["overall_ratio"] * 100))})
    # 월 발생량 확정
    questions.append({"id": _qid("kg", stream_id), "step": "classify", "streamId": stream_id, "kind": "number", "unit": "kg",
                      "text": "이 흐름의 월 발생량은 얼마인가요?", "why": "경로별 월 손익과 트럭 적재율 계산의 기준값입니다.",
                      "suggested": str(int(est)) if est else ""})
    return {"classify": classify, "origin": org["origin"], "originSource": "ai", "originConfidence": org["confidence"],
            "originEvidence": org["evidence"], "questions": questions,
            "meta": {"input_domain": res.get("input_domain"), "contamination": c, "items": items}}


FILM_OPTIONS = ["LDPE 필름·비닐", "PP 마대·직조", "경질 플라스틱"]
ITEM_OPTIONS = ["폐플라스틱", "금속 스크랩", "폐목재", "포장재"]
ORIGIN_OF_ANSWER = {"공정에서 나온 부산물 (PIR)": "PIR", "입고 자재의 포장재 (PCR)": "PCR"}


def apply_classify_answer(stream: dict, question_id: str, answer: str, *, by: str = "담당자", at: str = "") -> tuple:
    """① 되묻기 답을 흐름(Stream, camelCase dict)에 반영한다. 반환: (새 흐름 dict, Correction 초안 list)

    질문 id 는 'classify-<종류>:<streamId>' (item, film, resin, mixed, origin, contamination, kg).
    답이 어떤 순서로 와도 결과가 같도록: film 답은 item·detail 을 정하고 material 은 '미확인'일 때만 채운다.
    resin 답은 material 을 정한다('혼합·모름'은 덮어쓰지 않는다).
    AI 값을 사람이 바꾼 항목은 Correction 초안으로 돌려준다(현장 지식 카드·재학습 입구용).
    """
    import copy
    from datetime import datetime

    kind = question_id.split(":", 1)[0]
    kind = kind.removeprefix("classify-")
    s = copy.deepcopy(stream); c = s["classify"]; changes = []

    def setv(obj, key, val, field):
        if obj.get(key) != val:
            changes.append((field, obj.get(key), val)); obj[key] = val

    unknown = c.get("material") in (None, "", "미확인")
    if kind == "film":
        if answer == "LDPE 필름·비닐":
            setv(c, "item", "포장재", "classify.item"); setv(c, "detail", "LDPE 필름", "classify.detail")
            if unknown:
                setv(c, "material", "LDPE", "classify.material")
        elif answer == "PP 마대·직조":
            setv(c, "item", "포장재", "classify.item"); setv(c, "detail", "PP 마대·직조 포대", "classify.detail")
            if unknown:
                setv(c, "material", "PP", "classify.material")
        elif answer == "경질 플라스틱":
            setv(c, "item", "폐플라스틱", "classify.item")
            setv(c, "detail", "경질 플라스틱" if unknown else f"{c['material']} 경질 플라스틱", "classify.detail")
        else:
            raise ValueError(f"필름 질문의 답이 아님: {answer}")
    elif kind == "resin":
        if answer not in MATERIAL_OPTIONS:
            raise ValueError(f"수지 질문의 답이 아님: {answer}")
        if answer != "혼합·모름":
            setv(c, "material", "HDPE" if answer == "PE(HDPE)" else answer, "classify.material")
    elif kind == "item":
        if answer not in ITEM_OPTIONS:
            raise ValueError(f"품목 질문의 답이 아님: {answer}")
        if c.get("item") != answer:
            setv(c, "item", answer, "classify.item"); setv(c, "detail", answer, "classify.detail")
            if answer not in ("폐플라스틱", "포장재"):
                setv(c, "material", "미확인", "classify.material")
    elif kind == "mixed":
        setv(c, "singleMaterial", answer == "단일 재질", "classify.singleMaterial")
    elif kind == "origin":
        if answer not in ORIGIN_OF_ANSWER:
            raise ValueError(f"출처 질문의 답이 아님: {answer}")
        setv(s, "origin", ORIGIN_OF_ANSWER[answer], "origin"); s["originSource"] = "human"
    elif kind == "kg":
        kg = float(answer)
        if kg < 0:
            raise ValueError("월 발생량은 0 이상")
        setv(s, "monthlyKg", kg, "monthlyKg"); s["monthlyKgConfirmed"] = True
    elif kind == "contamination":
        pass  # 계약에 흐름 단위 이물 비율 필드가 없다(Detection.ratio 는 박스별). 답은 Question.answer 로만 남는다
    else:
        raise ValueError(f"모르는 질문 id: {question_id}")
    at = at or datetime.now().isoformat(timespec="seconds")
    corrections = [{"id": f"corr-{kind}:{s.get('id', '')}:{i}", "at": at, "by": by, "step": "classify", "streamId": s.get("id"),
                    "field": f, "before": "" if b is None else str(b), "after": str(a)} for i, (f, b, a) in enumerate(changes)]
    return s, corrections
