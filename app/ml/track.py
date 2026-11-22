"""영상/사진 -> 재질 탐지(RT-DETR) + 상태 탐지(이물질 모델) -> ByteTrack -> ClassifyResult (backend#7)

- 영상: --fps 간격으로 프레임 추출(30초 제한), ByteTrack으로 같은 객체를 묶어 트랙별 재질 다수결.
- 사진 여러 장: 장마다 독립 판정 후 집계(수량은 한 장에서 본 최대 개수).
- 이물질: --cond-model 이 있으면 상태 박스(clean/contaminated/multi_material)를 재질 박스에 IoU>=0.5로 붙여
  재질별 이물질 비율과 전체 이물질 비율을 낸다.
- confidence 는 calibrate.py 가 val 세트로 만든 보정표(점수 -> 실제 정밀도)를 거친 값.

예) python ml/infer/track.py --model runs/v2_material/best --cond-model runs/v2_condition/best --src clip.mp4
"""
import argparse
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import supervision as sv
import torch
from transformers import RTDetrV2ForObjectDetection

KO = {"plastic": "폐플라스틱(합성수지)", "metal": "금속 스크랩", "wood": "폐목재", "paper": "폐지·포장재", "other": "기타(유리·전지)"}


class Detector:
    def __init__(self, model_dir, size=640, dev=None):
        self.dev = dev or ("cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu"))
        self.m = RTDetrV2ForObjectDetection.from_pretrained(model_dir).to(self.dev).eval()
        self.names = [self.m.config.id2label[i] for i in range(len(self.m.config.id2label))]
        self.size = size
        tf = Path(model_dir) / "threshold.json"
        t = json.loads(tf.read_text()) if tf.exists() else {}
        cal = t.get("calibration")
        self.cal = (np.array([c[0] for c in cal]), np.array([c[1] for c in cal])) if cal else None
        self.threshold = t.get("threshold", 0.3)
        self.version = Path(model_dir).resolve().parent.name
        self.dir = Path(model_dir)
        fo = Path(model_dir) / "field_ok.json"
        self.field_note = json.loads(fo.read_text()) if fo.exists() else None

    def calibrate(self, raw):
        """raw sigmoid 점수 -> val 세트에서 측정한 실제 정밀도"""
        return np.interp(raw, *self.cal) if self.cal is not None else raw

    def _to_det(self, p, b, W, H, conf):
        sc, cl = p.max(-1)
        keep = sc > conf
        b, sc, cl = b[keep], sc[keep], cl[keep]
        xyxy = torch.stack([(b[:, 0] - b[:, 2] / 2) * W, (b[:, 1] - b[:, 3] / 2) * H,
                            (b[:, 0] + b[:, 2] / 2) * W, (b[:, 1] + b[:, 3] / 2) * H], 1).numpy().reshape(-1, 4)
        det = sv.Detections(xyxy=xyxy, confidence=self.calibrate(sc.numpy()).astype(np.float32),
                            class_id=cl.numpy().astype(int), data={"raw_score": sc.numpy()})
        return det.with_nms(threshold=0.6, class_agnostic=True)

    @torch.no_grad()
    def batch(self, frames, conf=None):
        """프레임 여러 장을 한 번의 forward 로 처리 (영상 지연 단축: 호출 오버헤드가 프레임당 31ms 중 절반 이상)"""
        conf = self.threshold if conf is None else conf
        xs = [cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2RGB), (self.size, self.size)) for f in frames]
        x = torch.from_numpy(np.stack(xs).astype(np.float32) / 255.0).permute(0, 3, 1, 2).to(self.dev)
        out = self.m(pixel_values=x)
        P = out.logits.sigmoid().cpu(); B = out.pred_boxes.cpu()
        return [self._to_det(P[i], B[i], f.shape[1], f.shape[0], conf) for i, f in enumerate(frames)]

    @torch.no_grad()
    def __call__(self, bgr, conf=None):
        return self.batch([bgr], conf)[0]


class DomainGate:
    """입력이 어느 도메인인지(현장 사진/선별장/컨베이어) 판정 -> 이물질 결과를 낼지 결정 (train_domain_clf.py)"""
    def __init__(self, clf_dir="runs/domain_clf", dev=None):
        import pickle

        from torchvision import models, transforms
        self.dev = dev or ("cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu"))
        with open(Path(clf_dir) / "clf.pkl", "rb") as f:
            self.clf = pickle.load(f)
        m = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1); m.fc = torch.nn.Identity()
        self.m = m.eval().to(self.dev)
        self.tf = transforms.Compose([transforms.ToPILImage(), transforms.Resize((224, 224)), transforms.ToTensor(),
                                      transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])

    @torch.no_grad()
    def __call__(self, bgr_frames):
        x = torch.stack([self.tf(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in bgr_frames]).to(self.dev)
        p = self.clf.predict_proba(self.m(x).cpu().numpy()).mean(0)
        return self.clf.classes_[int(p.argmax())], float(p.max())


_GATES: dict = {}


def domain_gate(clf_dir):
    """도메인 판정기는 디렉터리마다 한 번만 만든다(resnet18 가중치를 부를 때마다 읽으면 사진 한 장에 0.4초가 더 든다)."""
    key = str(clf_dir)
    if key not in _GATES:
        _GATES[key] = DomainGate(clf_dir)
    return _GATES[key]


def frames(src, fps):
    p = Path(src)
    if p.is_dir() or p.suffix.lower() in (".jpg", ".jpeg", ".png"):
        files = (sorted(p.glob("*.jp*g")) + sorted(p.glob("*.png"))) if p.is_dir() else [p]
        for i, f in enumerate(files):
            yield i, i, cv2.imread(str(f))
        return
    cap = cv2.VideoCapture(str(p))
    vfps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, round(vfps / fps)); i = k = 0
    while True:
        ok, fr = cap.read()
        if not ok or i / vfps > 30.5:  # 업로드 정책: 30초 제한
            break
        if i % step == 0:
            yield k, i / vfps, fr; k += 1
        i += 1


def attach_conditions(mat, cond, cond_names, iou_thr=0.5):
    """재질 박스마다 상태를 붙인다.
    - clean / contaminated 는 서로 배타: IoU>=thr 로 겹치는 상태 박스 중 (IoU x 보정신뢰도)가 가장 큰 쪽 하나만
    - multi_material(라벨·뚜껑 미분리)은 독립 속성: 겹치는 multi 박스가 있으면 추가"""
    out = [set() for _ in range(len(mat))]
    if cond is None or not len(cond) or not len(mat):
        return out
    M = sv.box_iou_batch(mat.xyxy, cond.xyxy)
    # 작은 이물질 박스(테이프·라벨·얼룩)는 물체 전체와 IoU 가 낮다 -> '상태 박스가 재질 박스 안에 얼마나 들어있는가'(containment)도 본다.
    # 선별장(71385) 라벨은 물체 전체 박스라 IoU 로, 현장 사진 의사 라벨(OWLv2)은 부분 박스라 containment 로 붙는다.
    mx1, my1, mx2, my2 = [mat.xyxy[:, k][:, None] for k in range(4)]
    cx1, cy1, cx2, cy2 = [cond.xyxy[:, k][None, :] for k in range(4)]
    inter = np.clip(np.minimum(mx2, cx2) - np.maximum(mx1, cx1), 0, None) * np.clip(np.minimum(my2, cy2) - np.maximum(my1, cy1), 0, None)
    c_area = ((cond.xyxy[:, 2] - cond.xyxy[:, 0]) * (cond.xyxy[:, 3] - cond.xyxy[:, 1]))[None, :] + 1e-9
    m_area = ((mat.xyxy[:, 2] - mat.xyxy[:, 0]) * (mat.xyxy[:, 3] - mat.xyxy[:, 1]))[:, None] + 1e-9
    contain = (inter / c_area >= 0.7) & (c_area / m_area <= 0.5)  # 상태 박스의 70% 이상이 재질 박스 안, 크기는 절반 이하
    for i in range(len(mat)):
        best, best_s = None, 0.0
        for j in range(len(cond)):
            n = cond_names[int(cond.class_id[j])]
            ov = M[i, j] >= iou_thr or (n != "clean" and contain[i, j])  # clean 은 물체 전체 박스만 인정
            if not ov:
                continue
            if n == "multi_material":
                out[i].add(n)
            elif n == "contaminated" and contain[i, j] and M[i, j] < iou_thr:
                out[i].add(n)  # 부분 이물질: clean 과 경쟁하지 않고 바로 표시
            elif M[i, j] * cond.confidence[j] > best_s:
                best, best_s = n, M[i, j] * cond.confidence[j]
        if best and not (best == "clean" and "contaminated" in out[i]):
            out[i].add(best)
    return out


class Aggregator:
    def __init__(self, names, photo_mode):
        self.names, self.photo = names, photo_mode
        self.tracks = defaultdict(lambda: {"cls": [], "conf": [], "area": 0.0, "hits": 0, "cond": Counter(), "judged": 0})
        self.photo_counts = defaultdict(int)
        self.n = 0; self.frame_area = 1
        self.rep = None  # 대표 프레임(재질 박스가 가장 많은 프레임)의 박스들 -> 프런트 detections[]
        self.rep_img = None

    def add(self, d, conds, frame_area, frame_wh=None, cond_det=None, frame_index=None, frame_img=None):
        self.n += 1; self.frame_area = frame_area
        if frame_wh is not None and (self.rep is None or len(d) > self.rep["n_items"]):
            W, H = frame_wh
            items = [{"label": self.names[int(c)], "confidence": float(s), "xyxy": [float(v) for v in b], "conds": sorted(cs)}
                     for b, c, s, cs in zip(d.xyxy, d.class_id, d.confidence, conds)]
            conts = []
            if cond_det is not None and len(cond_det):
                for b, c, s in zip(cond_det.xyxy, cond_det.class_id, cond_det.confidence):
                    conts.append({"state": cond_det.data.get("names", {}).get(int(c), str(int(c))) if isinstance(cond_det.data, dict) and "names" in cond_det.data else int(c),
                                  "confidence": float(s), "xyxy": [float(v) for v in b]})
            self.rep = {"frame_index": frame_index if frame_index is not None else self.n - 1, "width": W, "height": H,
                        "n_items": len(d), "items": items, "conditions": conts}
            self.rep_img = frame_img  # 이물 이름 붙이기(OWLv2)용
        ids = d.tracker_id if (d.tracker_id is not None and not self.photo) else [f"{self.n}_{i}" for i in range(len(d))]
        if self.photo:
            for c, k in Counter(int(c) for c in d.class_id).items():
                self.photo_counts[c] = max(self.photo_counts[c], k)
        for tid, c, s, ar, cs in zip(ids, d.class_id, d.confidence, d.area, conds):
            t = self.tracks[tid]
            t["cls"].append(int(c)); t["conf"].append(float(s)); t["area"] += float(ar); t["hits"] += 1
            if cs:
                t["judged"] += 1
                for x in cs:
                    t["cond"][x] += 1

    def result(self, backend, version, elapsed, cond_on, domain=None, field_note=None):
        per = defaultdict(lambda: {"n": 0, "area": 0.0, "conf": [], "cont": 0, "multi": 0, "judged": 0,
                                   "cont_area": 0.0, "multi_area": 0.0, "j_area": 0.0})
        kept = 0
        for t in self.tracks.values():
            if not self.photo and t["hits"] < 2 and self.n > 2:  # 1프레임만 잡힌 트랙은 노이즈
                continue
            kept += 1
            mat = self.names[Counter(t["cls"]).most_common(1)[0][0]]
            v = per[mat]; a = t["area"] / t["hits"]
            v["n"] += 1; v["area"] += a; v["conf"].append(float(np.mean(t["conf"])))
            if t["judged"]:
                v["judged"] += 1; v["j_area"] += a
                if t["cond"]["contaminated"] >= t["judged"] / 2:
                    v["cont"] += 1; v["cont_area"] += a
                if t["cond"]["multi_material"] >= t["judged"] / 2:
                    v["multi"] += 1; v["multi_area"] += a
        tot = sum(v["area"] for v in per.values()) or 1
        items = []
        for m, v in per.items():
            cnt = self.photo_counts[self.names.index(m)] if self.photo else v["n"]
            cover = v["area"] / (self.frame_area * (self.n if self.photo else 1))
            items.append({"material": m, "label_ko": KO.get(m, m), "count_estimate": int(cnt),
                          "area_ratio": round(v["area"] / tot, 3), "frame_coverage": round(min(1.0, cover), 3),
                          "confidence": round(float(np.mean(v["conf"])), 3),
                          "contaminated_ratio": round(v["cont"] / v["judged"], 3) if (cond_on and v["judged"]) else None,
                          "multi_material_ratio": round(v["multi"] / v["judged"], 3) if (cond_on and v["judged"]) else None})
        items.sort(key=lambda d: -d["area_ratio"])
        primary = items[0] if items else None
        contamination = None
        judged_any = any(v["judged"] for v in per.values())
        if cond_on and judged_any:
            ja = sum(v["j_area"] for v in per.values())
            notes = {"a71385_sorting": "선별장 test 기준 물체 단위 이물 판정 정확도 0.76, 사진별 비율 오차 ±0.20 (v4)",
                     "aihub137_field": (field_note or {}).get("note", "현장 사진: OWLv2 의사 라벨로 학습, 사람 라벨 검증 없음. 사용자 확인 권장"),
                     "sortwaste_conveyor": (field_note or {}).get("conveyor_note", "컨베이어: ZeroWaste 오염물 라벨로 검증" if field_note else "선별장과 다른 시설 영상이라 검증 안 된 참고값")}
            contamination = {"overall_ratio": round(sum(v["cont_area"] for v in per.values()) / ja, 3) if ja else 0.0,
                             "multi_material_ratio": round(sum(v["multi_area"] for v in per.values()) / ja, 3) if ja else 0.0,
                             "judged_objects": int(sum(v["judged"] for v in per.values())),
                             "reliable": domain == "a71385_sorting",
                             "note": notes.get(domain, "검증 안 된 참고값")}
        low = (not primary) or primary["confidence"] < 0.6
        qs = (["사진에서 품목 판단이 애매합니다. 주된 품목이 무엇인가요?"] if low else []) + \
             ["월 발생량(kg)은 어느 정도인가요?", "보관 기간과 보관 장소(실내/야외)는 어떻게 되나요?"]
        if contamination and contamination["overall_ratio"] > 0.3:
            qs.append("이물질(음식물·테이프 등)을 현장에서 제거할 수 있나요?")
        if cond_on and contamination is None:  # 상태 모델을 돌리지 않은 경우: 사용자에게 확인
            qs.append("폐기물에 음식물·기름·테이프·라벨 같은 이물질이 묻거나 붙어 있나요? 대략 몇 % 정도인가요?")
        elif contamination is not None and not contamination["reliable"]:
            qs.append(f"사진 기준 이물질 비율을 약 {int(contamination['overall_ratio']*100)}%로 추정했습니다. 실제와 비슷한가요?")
        detections = []
        if self.rep:
            W, H = self.rep["width"], self.rep["height"]; FA = float(W * H)
            def norm(b):
                x1, y1, x2, y2 = b
                return [round(max(0.0, x1 / W), 4), round(max(0.0, y1 / H), 4), round(min(1.0, (x2 - x1) / W), 4), round(min(1.0, (y2 - y1) / H), 4)]
            for i, it in enumerate(self.rep["items"]):
                b = it["xyxy"]; area = (b[2] - b[0]) * (b[3] - b[1])
                detections.append({"id": f"d{i}", "label": it["label"], "kind": "item", "confidence": round(min(1.0, it["confidence"]), 3),
                                   "box": norm(b), "ratio": round(min(1.0, area / FA), 4), "conds": it["conds"]})
            for j, c in enumerate(self.rep["conditions"]):
                if c["state"] not in ("contaminated", "multi_material"):
                    continue
                b = c["xyxy"]; area = (b[2] - b[0]) * (b[3] - b[1])
                # ratio: 이물 박스가 들어 있는 재질 박스 대비 면적 비율(없으면 프레임 대비). '면적 기준 추정'
                parent = None
                for it in self.rep["items"]:
                    pb = it["xyxy"]
                    ix = max(0, min(b[2], pb[2]) - max(b[0], pb[0])); iy = max(0, min(b[3], pb[3]) - max(b[1], pb[1]))
                    if ix * iy >= 0.7 * area:
                        pa = (pb[2] - pb[0]) * (pb[3] - pb[1])
                        if parent is None or pa < parent:
                            parent = pa
                detections.append({"id": f"c{j}", "label": "라벨·뚜껑 미분리" if c["state"] == "multi_material" else "이물질",
                                   "kind": "contaminant", "confidence": round(min(1.0, c["confidence"]), 3), "box": norm(b),
                                   "ratio": round(min(1.0, area / (parent or FA)), 4), "ratio_basis": "면적 기준 추정"})
        return {"stage": "classify", "backend": backend, "model_version": version, "detections": detections,
                "rep_frame": self.rep["frame_index"] if self.rep else None,
                "mode": "photos" if self.photo else "video", "primary_material": primary["material"] if primary else None,
                "items": items, "input_domain": domain, "contamination": contamination, "frames_analyzed": self.n,
                "tracks_total": 0 if self.photo else kept, "needs_followup": low,
                "followup_questions": qs, "latency_sec": round(elapsed, 2)}


def run(model, src, out=None, fps=4.0, conf=None, cond_model=None, save_vis=True, gate=None, batch_size=8, cond_every=2,
        field_condition=None, namer=None):
    """field_condition: 현장 사진(aihub137_field)에도 상태 모델을 돌릴지. None 이면 모델 디렉터리에 field_ok.json 이 있을 때만(v3 이상).
    namer: ContaminantNamer (선택). 있으면 대표 프레임의 이물 박스에 OWLv2 로 라벨·테이프·금속 인서트·탄화물 이름을 단다."""
    det = model if isinstance(model, Detector) else Detector(model)
    cdet = cond_model if (cond_model is None or isinstance(cond_model, Detector)) else Detector(cond_model)
    if field_condition is None:
        field_condition = bool(cdet is not None and (Path(cdet.dir) / "field_ok.json").exists())
    photo = Path(src).is_dir() or Path(src).suffix.lower() in (".jpg", ".jpeg", ".png")
    tracker = sv.ByteTrack(track_activation_threshold=0.4, lost_track_buffer=int(fps * 2), frame_rate=int(max(1, fps)))
    agg = Aggregator(det.names, photo)
    box_ann, lab_ann = sv.BoxAnnotator(thickness=3), sv.LabelAnnotator(text_scale=0.7)
    out = Path(out) if out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    writer = None; t0 = time.time(); domain = None
    if cdet is not None and gate is None:
        gate_dir = os.getenv("DOMAIN_CLF_DIR", "")
        if gate_dir and (Path(gate_dir) / "clf.pkl").exists():
            gate = domain_gate(gate_dir)
    def chunks(it, n):
        buf = []
        for item in it:
            buf.append(item)
            if len(buf) == n:
                yield buf; buf = []
        if buf:
            yield buf

    cdet_run = cdet
    for chunk in chunks(frames(src, fps), batch_size):
        ks = [c[0] for c in chunk]; frs = [c[2] for c in chunk]
        if ks[0] == 0 and gate is not None:  # 첫 프레임으로 도메인 판정
            domain = gate([frs[0]])[0]
            # v3 부터 현장 사진도 상태 모델을 돌린다(OWLv2 의사 라벨로 학습). 결과의 reliable/note 로 신뢰 수준을 표시.
            cdet_run = cdet if (cdet is not None and (field_condition or domain != "aihub137_field")) else None
        dets = det.batch(frs, conf)
        # 상태 모델은 cond_every 장마다 한 번 (이물질 비율은 집계값이라 모든 프레임이 필요 없다)
        cond_idx = [i for i, k in enumerate(ks) if k % cond_every == 0] if cdet_run is not None else []
        cds = dict(zip(cond_idx, cdet_run.batch([frs[i] for i in cond_idx]))) if cond_idx else {}
        for i, (k, fr, d) in enumerate(zip(ks, frs, dets)):
            if not photo:
                d = tracker.update_with_detections(d)
            cd = cds.get(i)
            cnames = cdet_run.names if (cdet_run is not None and cd is not None) else []
            conds = attach_conditions(d, cd, cnames)
            if cd is not None:
                cd.data["names"] = {i_: n_ for i_, n_ in enumerate(cnames)}
            agg.add(d, conds, fr.shape[0] * fr.shape[1], frame_wh=(fr.shape[1], fr.shape[0]), cond_det=cd, frame_index=k,
                    frame_img=fr if namer is not None else None)
            if out and save_vis:
                tids = d.tracker_id if d.tracker_id is not None else [None] * len(d)
                labels = []
                for c, s, t, cs in zip(d.class_id, d.confidence, tids, conds):
                    flag = "".join({"contaminated": " !dirty", "multi_material": " !label"}.get(x, "") for x in sorted(cs))
                    labels.append(f"{'#' + str(t) + ' ' if t is not None else ''}{det.names[c]} {s:.2f}{flag}")
                vis = lab_ann.annotate(box_ann.annotate(fr.copy(), d), d, labels)
                if not photo:
                    if writer is None:
                        writer = cv2.VideoWriter(str(out / "tracked.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, (fr.shape[1], fr.shape[0]))
                    writer.write(vis)
                if k < 6 or (not photo and k % 10 == 0 and k < 60):
                    cv2.imwrite(str(out / f"frame_{k:02d}.jpg"), vis)
    if writer is not None:
        writer.release()
    res = agg.result(f"trained:rtdetr_v2_r18{'+condition' if cdet else ''}", det.version, time.time() - t0, cdet_run is not None, domain,
                     cdet.field_note if cdet is not None else None)
    if namer is not None and agg.rep_img is not None and res.get("detections"):
        res["detections"] = namer.name(agg.rep_img, res["detections"])
        res["latency_sec"] = round(time.time() - t0, 2)
    from app.ml.internal import ClassifyResult as InternalClassifyResult  # ml 내부 결과 검증
    res = InternalClassifyResult(**res).model_dump()
    if out:
        (out / "classify_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.getenv("MATERIAL_MODEL_DIR", "models/material"))
    ap.add_argument("--cond-model", default=None)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", default="ml/reports/demo")
    ap.add_argument("--fps", type=float, default=4.0, help="영상 샘플링. 4fps면 30초 영상이 10초 안에 끝난다(M 시리즈 MPS). 컨베이어처럼 빠르면 6")
    ap.add_argument("--conf", type=float, default=None, help="raw 점수 임계값. 비우면 <model>/threshold.json")
    a = ap.parse_args()
    res = run(a.model, a.src, a.out, a.fps, a.conf, a.cond_model)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
