"""보관장 3D 스캔 합성 평가. H100 재구성 환경에서 돈다(재구성 파이프라인과 모델이 거기 있다).

    PYTHONPATH=/opt/rsai-recon/app /opt/rsai-recon/env/bin/python scripts/v3/eval/scan_eval.py \
        --scenes /mnt/recon/synth --out /mnt/recon/eval/2026-11-20.json [--demo-out /mnt/recon/demo]

장면(scan_synth.py가 만든 frames/와 truth.json)마다 다음을 잰다.

- 사진에 센서 잡음(표준편차 2~5/255)을 더하고 JPEG(품질 82)로 다시 저장해 휴대폰 입력에 가깝게 만든 뒤 파이프라인을 돌린다.
- 재구성 카메라 위치(바닥 좌표계)를 정답 카메라 위치에 2차원 닮음 변환(Umeyama)으로 맞춘다. 맞춘 배율로 실제 크기 오차를,
  맞춘 뒤 남는 거리로 궤적 오차를 낸다.
- 더미 짝짓기: 재구성 바닥 자국 사각형(닮음 변환으로 옮김)과 정답 최소 넓이 사각형의 IoU(1cm 격자로 칠해 셈)를 헝가리안으로
  짝짓고 IoU ≥ 0.3만 맞은 것으로 센다. 탐지 정밀도·재현율.
- 맞은 더미마다 부피 절대 백분율 오차(정답은 위에서 쏜 광선으로 잰 윗면 높이 적분 부피), 범위가 정답을 담는지, 긴 변·짧은 변·높이 오차.
- MAPE와 중앙값에 95% 부트스트랩 구간(더미 단위로 2,000번 다시 뽑기, 시드 고정)을 붙인다.

결정적이다: 잡음 시드는 장면 이름에서 정하고, 파이프라인의 RANSAC 시드도 고정이다(VGGT·MoGe 추론의 GPU 수치 차이만 남는다).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import cv2
import numpy as np


def _noisy(blob: bytes, rng: np.random.Generator) -> bytes:
    img = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR).astype(np.float32)
    sigma = rng.uniform(2.0, 5.0)
    img = np.clip(img + rng.normal(0.0, sigma, img.shape), 0, 255).astype(np.uint8)
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    assert ok
    return out.tobytes()


def _sim2(src: np.ndarray, dst: np.ndarray):
    """dst ≈ s·R·src + t (2차원). (s, R, t)."""
    ms, md = src.mean(0), dst.mean(0)
    a, b = src - ms, dst - md
    U, S, Vt = np.linalg.svd(b.T @ a / len(src))
    D = np.eye(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[1, 1] = -1
    R = U @ D @ Vt
    s = float(np.trace(np.diag(S) @ D) / (a**2).sum(1).mean())
    return s, R, md - s * R @ ms


def _poly_iou(p: np.ndarray, q: np.ndarray, cell: float = 0.01) -> float:
    lo = np.minimum(p.min(0), q.min(0)) - 0.05
    hi = np.maximum(p.max(0), q.max(0)) + 0.05
    shape = (int((hi[1] - lo[1]) / cell) + 1, int((hi[0] - lo[0]) / cell) + 1)
    if shape[0] * shape[1] > 4_000_000:
        return 0.0

    def raster(poly):
        m = np.zeros(shape, np.uint8)
        pts = np.round((poly - lo) / cell).astype(np.int32)
        cv2.fillPoly(m, [pts], 1)
        return m.astype(bool)

    a, b = raster(p), raster(q)
    union = (a | b).sum()
    return float((a & b).sum() / union) if union else 0.0


def _boot(values: list[float], fn, n: int = 2000, seed: int = 7) -> list[float] | None:
    if len(values) < 3:
        return None
    rng = np.random.default_rng(seed)
    arr = np.asarray(values)
    stats = [fn(arr[rng.integers(0, len(arr), len(arr))]) for _ in range(n)]
    return [round(float(np.percentile(stats, 2.5)), 4), round(float(np.percentile(stats, 97.5)), 4)]


def evaluate_scene(models, folder: Path, pipeline) -> dict:
    from scipy.optimize import linear_sum_assignment

    truth = json.loads((folder / "truth.json").read_text())
    seed = int(hashlib.sha256(folder.name.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    blobs = [_noisy(p.read_bytes(), rng) for p in sorted((folder / "frames").glob("*.jpg"))]
    t0 = time.time()
    failure = None
    try:
        out = pipeline.run(models, blobs)
    except pipeline.ReconError as exc:
        failure = exc.code
        out = None
    elapsed = time.time() - t0
    row = {
        "scene": folder.name,
        "path": truth.get("path", truth["mode"]),
        "dark": truth["dark"],
        "portrait": truth["portrait"],
        "frames": truth["frames"],
        "truthPiles": len(truth["piles"]),
        "seconds": round(elapsed, 2),
        "failure": failure,
    }
    if out is None:
        row.update(predPiles=0, tp=0, fp=0, fn=len(truth["piles"]), matches=[])
        return row
    res = out["result"]
    row["warnings"] = res["warnings"]
    row["predPiles"] = len(res["piles"])
    row["scaleSpreadPct"] = res["scale"]["spreadPct"]
    row["timings"] = res["timingsSec"]
    if res["floor"] is None or "cameras" not in out["debug"]:
        row.update(tp=0, fp=len(res["piles"]), fn=len(truth["piles"]), matches=[], noFloor=True)
        return row
    cams = np.asarray(out["debug"]["cameras"])
    gt = np.asarray(truth["cameras"])
    s, R, t = _sim2(cams[:, :2], gt[:, :2])
    resid = np.linalg.norm((s * (R @ cams[:, :2].T)).T + t - gt[:, :2], axis=1)
    row["trajRmsM"] = round(float(np.sqrt((resid**2).mean())), 4)
    row["scaleErrPct"] = round((1.0 / s - 1.0) * 100.0, 2)  # 재구성 길이 ÷ 실제 길이 - 1
    row["scaleMethod"] = res["scale"]["method"]
    row["gateReject"] = bool(out["debug"].get("unreliable"))
    for key in ("consistentFrac", "camHeightStdM", "floorInlierFrac"):
        if key in out["debug"]:
            row[key] = round(float(out["debug"][key]), 4)
    if out["debug"].get("scaleDepth"):
        # 깊이 단서(MoGe-2)만 썼다면의 실제 크기 오차
        row["scaleErrDepthOnlyPct"] = round(
            (out["debug"]["scaleDepth"] / out["debug"]["scaleFinal"] / s - 1.0) * 100.0, 2
        )
    row["camHeightErrPct"] = round(
        (float(np.median(cams[:, 2])) / float(np.median(gt[:, 2])) - 1.0) * 100.0, 2
    )
    preds = [np.asarray(p["footprint"]) for p in res["piles"]]
    preds_w = [(s * (R @ p.T)).T + t for p in preds]
    gts = [np.asarray(g["rect"]["corners"]) for g in truth["piles"]]
    iou = np.zeros((len(preds), len(gts)))
    for i, p in enumerate(preds_w):
        for j, g in enumerate(gts):
            iou[i, j] = _poly_iou(p, g)
    matches = []
    if len(preds) and len(gts):
        ri, ci = linear_sum_assignment(-iou)
        for i, j in zip(ri, ci):
            if iou[i, j] >= 0.3:
                p, g = res["piles"][i], truth["piles"][j]
                vt = g["envelopeVolumeM3"]
                lo, hi = p["volumeRangeM3"]
                matches.append(
                    {
                        "pred": p["id"],
                        "truth": g["id"],
                        "kind": g["kind"],
                        "streamId": g.get("streamId"),
                        "iou": round(float(iou[i, j]), 3),
                        "volumePred": p["volumeM3"],
                        "volumeTrue": vt,
                        "ape": round(abs(p["volumeM3"] - vt) / vt, 4),
                        "signedPct": round((p["volumeM3"] / vt - 1.0) * 100.0, 2),
                        # 실제 크기 오차를 정답 배율로 걷어 낸 부피 오차(모양·부피 계산만의 오차)
                        "apeScaleFixed": round(abs(p["volumeM3"] * s**3 - vt) / vt, 4),
                        "inRange": bool(lo <= vt <= hi),
                        "longErrM": round(p["sizeM"][0] - g["rect"]["size"][0], 3),
                        "shortErrM": round(p["sizeM"][1] - g["rect"]["size"][1], 3),
                        "heightErrM": round(p["heightM"] - g["heightM"], 3),
                        "coverage": p["coverage"],
                    }
                )
    row.update(
        tp=len(matches), fp=len(preds) - len(matches), fn=len(gts) - len(matches), matches=matches
    )
    row["totalVolumePred"] = round(sum(p["volumeM3"] for p in res["piles"]), 3)
    row["totalVolumeTrue"] = round(sum(g["envelopeVolumeM3"] for g in truth["piles"]), 3)
    row["_out"] = out
    return row


def summarize(rows: list[dict]) -> dict:
    def agg(sel: list[dict]) -> dict:
        tp = sum(r["tp"] for r in sel)
        fp = sum(r["fp"] for r in sel)
        fn = sum(r["fn"] for r in sel)
        apes = [m["ape"] for r in sel for m in r["matches"]]
        fixed = [m["apeScaleFixed"] for r in sel for m in r["matches"]]
        depth_only = [abs(r["scaleErrDepthOnlyPct"]) for r in sel if "scaleErrDepthOnlyPct" in r]
        signed = [m["signedPct"] for r in sel for m in r["matches"]]
        scale = [abs(r["scaleErrPct"]) for r in sel if "scaleErrPct" in r]
        tot = [
            abs(r["totalVolumePred"] / r["totalVolumeTrue"] - 1.0)
            for r in sel
            if r.get("totalVolumeTrue")
        ]
        inr = [m["inRange"] for r in sel for m in r["matches"]]
        sides = [abs(m["longErrM"]) for r in sel for m in r["matches"]] + [
            abs(m["shortErrM"]) for r in sel for m in r["matches"]
        ]
        return {
            "scenes": len(sel),
            "failed": sum(1 for r in sel if r.get("failure") or r.get("noFloor")),
            "truthPiles": tp + fn,
            "predPiles": tp + fp,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "matched": len(apes),
            "mape": round(float(np.mean(apes)), 4) if apes else None,
            "mapeCI95": _boot(apes, np.mean),
            "medianApe": round(float(np.median(apes)), 4) if apes else None,
            "medianApeCI95": _boot(apes, np.median),
            "mapeScaleFixed": round(float(np.mean(fixed)), 4) if fixed else None,
            "mapeScaleFixedCI95": _boot(fixed, np.mean),
            "medianApeScaleFixed": round(float(np.median(fixed)), 4) if fixed else None,
            "meanSignedPct": round(float(np.mean(signed)), 2) if signed else None,
            "rangeCoverage": round(float(np.mean(inr)), 4) if inr else None,
            "sideAbsErrMedianM": round(float(np.median(sides)), 3) if sides else None,
            "scaleAbsErrMedianPct": round(float(np.median(scale)), 2) if scale else None,
            "scaleAbsErrMeanPct": round(float(np.mean(scale)), 2) if scale else None,
            "scaleDepthOnlyAbsErrMedianPct": round(float(np.median(depth_only)), 2)
            if depth_only
            else None,
            "totalVolumeAbsErrMedian": round(float(np.median(tot)), 4) if tot else None,
            "secondsMedian": round(float(np.median([r["seconds"] for r in sel])), 2)
            if sel
            else None,
        }

    accepted = [r for r in rows if not r.get("gateReject") and not r.get("failure")]
    groups = {"all": rows, "accepted": accepted}
    for key in sorted({r["path"] for r in rows}):
        groups[f"path={key}"] = [r for r in rows if r["path"] == key]
    groups["guided"] = [r for r in rows if r["path"] in ("strafe", "inward") and not r["portrait"]]
    groups["guided+accepted"] = [r for r in groups["guided"] if r in accepted]
    groups["landscape"] = [r for r in rows if not r["portrait"] and r["path"] != "spin"]
    groups["portrait"] = [r for r in rows if r["portrait"] and r["path"] != "spin"]
    groups["dark"] = [r for r in rows if r["dark"]]
    kinds = {}
    for r in rows:
        if r["path"] == "spin" or r["portrait"] or r.get("gateReject"):
            continue
        for m in r["matches"]:
            kinds.setdefault(m["kind"], []).append(m["ape"])
    broken = [r for r in rows if r.get("trajRmsM") is None or r["trajRmsM"] > 0.5]
    fine = [r for r in rows if r.get("trajRmsM") is not None and r["trajRmsM"] <= 0.5]
    gate = {
        "brokenScenes(traj>0.5m)": len(broken),
        "brokenRejected": sum(1 for r in broken if r.get("gateReject")),
        "fineScenes": len(fine),
        "fineRejected": sum(1 for r in fine if r.get("gateReject")),
    }
    return {
        "gate": gate,
        "groups": {k: agg(v) for k, v in groups.items() if v},
        "byKindGuided": {
            k: {
                "n": len(v),
                "mape": round(float(np.mean(v)), 4),
                "medianApe": round(float(np.median(v)), 4),
            }
            for k, v in sorted(kinds.items())
        },
    }


def demo_assets(row: dict, folder: Path, out_dir: Path, pipeline, cap: int = 80_000) -> None:
    """시연 장면 결과를 체험하기 자산으로 남긴다(점 8만 개 이하)."""
    out = row["_out"]
    result = json.loads(json.dumps(out["result"]))
    P, C, K = pipeline.decode_points(out["points"])
    if len(P) > cap:
        pick = np.sort(np.random.default_rng(11).choice(len(P), cap, replace=False))
        P, C, K = P[pick], C[pick], K[pick]
    blob = pipeline.encode_points(P, C, K)
    result["pointsCount"] = len(P)
    result["bounds"] = {
        "min": [round(float(v), 3) for v in P.min(0)],
        "max": [round(float(v), 3) for v in P.max(0)],
    }
    assign = {m["pred"]: m["streamId"] for m in row["matches"] if m.get("streamId")}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hanbit-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    (out_dir / "hanbit-points.bin").write_bytes(blob)
    (out_dir / "hanbit-truth.json").write_text(
        json.dumps(
            {
                "assign": assign,
                "weighed": {},
                "note": "합성 장면 정답과 바닥 자국 IoU로 짝지은 결과",
                "matches": [
                    {
                        k: m[k]
                        for k in (
                            "pred",
                            "truth",
                            "kind",
                            "streamId",
                            "iou",
                            "volumePred",
                            "volumeTrue",
                        )
                    }
                    for m in row["matches"]
                ],
            },
            ensure_ascii=False,
            indent=1,
        )
    )


def main() -> None:
    import recon_pipeline as pipeline

    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--demo-out")
    args = ap.parse_args()
    models = pipeline.load_models()
    rows = []
    for folder in sorted(Path(args.scenes).iterdir()):
        if not (folder / "truth.json").exists():
            continue
        row = evaluate_scene(models, folder, pipeline)
        print(
            f"{row['scene']:>8} {row['path']:>6} frames={row['frames']:>2} truth={row['truthPiles']} pred={row['predPiles']} "
            f"tp={row['tp']} scale={row.get('scaleErrPct')}%({row.get('scaleMethod')}, depth-only {row.get('scaleErrDepthOnlyPct')}%) traj={row.get('trajRmsM')} "
            f"ape={[m['ape'] for m in row['matches']]} fixed={[m['apeScaleFixed'] for m in row['matches']]} fail={row.get('failure')} warn={row.get('warnings')}",
            flush=True,
        )
        if folder.name == "hanbit" and args.demo_out and "_out" in row:
            demo_assets(row, folder, Path(args.demo_out), pipeline)
        row.pop("_out", None)
        rows.append(row)
    scored = [r for r in rows if r["scene"] != "hanbit"]
    report = {
        "date": time.strftime("%Y-%m-%d"),
        "pipeline": pipeline.VERSION,
        "models": dict(models.names),
        "summary": summarize(scored),
        "demo": next((r for r in rows if r["scene"] == "hanbit"), None),
        "scenes": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
