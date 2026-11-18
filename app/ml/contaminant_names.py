"""이물 이름 붙이기 (선택 기능). 상태 모델이 잡은 '이물질' 박스에 OWLv2 로 이름을 단다.

이름은 네 가지만: 라벨, 테이프, 금속 인서트, 탄화물. (ABS 혼입 같은 '재질 혼입' 판정은 사진으로 못 하므로 제외)
비율(ratio)은 그대로 '면적 기준 추정' 이다. 매입처는 무게로 등급을 매기므로 확정값이 아니다.
켜는 법: track.py --name-contaminants --owlv2 weights/owlv2-base  /  backend: CONTAMINANT_NAMER_DIR
"""

NAME_PROMPTS = {
    "라벨": ["a paper label", "a sticker label on plastic", "a printed label"],
    "테이프": ["adhesive tape", "packing tape stuck on an object", "a strip of tape"],
    "금속 인서트": [
        "a metal insert",
        "a metal screw",
        "a metal bolt",
        "a metal nut embedded in plastic",
    ],
    "탄화물": ["burnt black residue", "charred plastic", "scorched black mark"],
}


def _xyxy(box_norm, W, H):
    x, y, w, h = box_norm
    return [x * W, y * H, (x + w) * W, (y + h) * H]


def _overlap(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    aa = (a[2] - a[0]) * (a[3] - a[1])
    bb = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (aa + bb - inter + 1e-9), inter / (min(aa, bb) + 1e-9)


def assign_names(
    preds: list[dict],
    detections: list[dict],
    W: int,
    H: int,
    iou_thr=0.3,
    cover_thr=0.5,
    max_area_ratio=3.0,
    min_score=0.2,
) -> list[dict]:
    """preds: [{"name","score","xyxy"}] (픽셀). detections: 내부 DetectionOut dict (box 정규화). 이물 박스에만 이름을 단다.
    겹침 규칙: IoU>=0.3, 또는 작은 쪽 기준 덮임>=0.5 이되 OWLv2 박스가 이물 박스의 3배보다 크면 제외(배경 전체를 잡은 큰 박스가 모든 이물에 같은 이름을 다는 것 방지)."""
    for d in detections:
        if d.get("kind") != "contaminant":
            continue
        b = _xyxy(d["box"], W, H)
        best = None
        d_area = (b[2] - b[0]) * (b[3] - b[1])
        for p in preds:
            if p["score"] < min_score:
                continue
            iou, cover = _overlap(b, p["xyxy"])
            p_area = (p["xyxy"][2] - p["xyxy"][0]) * (p["xyxy"][3] - p["xyxy"][1])
            ok = iou >= iou_thr or (
                cover >= cover_thr and p_area <= max_area_ratio * max(d_area, 1.0)
            )
            if ok and (best is None or p["score"] > best["score"]):
                best = p
        if best is not None:
            d["label"] = best["name"]
            d["name_source"] = "owlv2"
            d["name_confidence"] = round(float(best["score"]), 3)
    return detections


class ContaminantNamer:
    def __init__(self, model_dir: str, dev: str | None = None, thr: float = 0.12):
        import torch
        from transformers import Owlv2ForObjectDetection, Owlv2Processor

        self.torch = torch
        self.dev = dev or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.proc = Owlv2Processor.from_pretrained(model_dir)
        self.model = Owlv2ForObjectDetection.from_pretrained(model_dir).to(self.dev).eval()
        self.texts = [p for ps in NAME_PROMPTS.values() for p in ps]
        self.names = [n for n, ps in NAME_PROMPTS.items() for _ in ps]
        self.thr = thr

    def predict(self, frame_bgr) -> list[dict]:
        from PIL import Image

        pil = Image.fromarray(frame_bgr[:, :, ::-1])
        W, H = pil.size
        S = max(W, H)
        inputs = self.proc(text=[self.texts], images=pil, return_tensors="pt").to(self.dev)
        with self.torch.no_grad():
            o = self.model(**inputs)
        r = self.proc.post_process_object_detection(
            o, threshold=self.thr, target_sizes=self.torch.tensor([[S, S]])
        )[0]
        return [
            {"name": self.names[int(l)], "score": float(s), "xyxy": [float(v) for v in b]}
            for b, s, l in zip(r["boxes"].cpu(), r["scores"].cpu(), r["labels"].cpu())
        ]

    def name(self, frame_bgr, detections: list[dict]) -> list[dict]:
        if not any(d.get("kind") == "contaminant" for d in detections):
            return detections
        H, W = frame_bgr.shape[:2]
        return assign_names(self.predict(frame_bgr), detections, W, H)
