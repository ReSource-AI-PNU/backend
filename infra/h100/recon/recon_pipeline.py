"""보관장 3D 스캔의 재구성 파이프라인(모듈 이름은 MoGe가 깔고 오는 pipeline 패키지와 겹치지 않게 recon_pipeline). H100 재구성 서버(server.py)가 부른다.

사진 묶음(휴대폰 영상에서 브라우저가 고른 장면, 또는 사진 여러 장)을 받아 다음을 낸다(계약: 맥 ~/rs-work/scan/CONTRACT.md 2절).

1. VGGT-1B(Wang et al., CVPR 2025)로 프레임마다 카메라 위치·깊이·신뢰도를 한 번에 추정한다. 이 좌표는 크기를 모른다.
2. MoGe-2(단안 실제 크기 기하)로 몇 장의 깊이를 m 단위로 다시 추정하고, 같은 픽셀의 VGGT 깊이와의 비율(프레임마다
   중앙값)의 중앙값을 m 단위 크기로 쓴다. 프레임 사이 퍼짐(사분위 범위의 절반 ÷ 중앙값)을 spreadPct로 낸다.
3. 휴대폰을 세워 든다는 가정으로 카메라 y축 평균을 위쪽으로 잡고, 그 방향에 가까운 평면을 RANSAC으로 찾아 바닥으로 삼는다.
4. 바닥 좌표계(원점은 보인 바닥의 중심, z는 위쪽, m)로 옮기고, 크고 수직인 면(벽)을 지운 뒤 바닥 격자 2cm 높이 지도를 만든다.
5. 높이 지도의 연결 성분을 더미로 보고, 더미마다 최소 넓이 사각형, 부피(밑이 꽉 찬 더미로 본 윗면 높이의 합), 범위,
   coverage, 대표 프레임에서 자른 썸네일을 낸다.

숫자를 지어내지 않는다: 바닥을 찾지 못하면 floor=None, piles=[]이고 warnings에 "no-floor"를 넣는다.
"""

from __future__ import annotations

import base64
import math
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

GRID = 0.02  # 높이 지도 칸(m)
FLOOR_TOL = 0.02  # 바닥 평면 RANSAC 허용 거리(m)
PILE_MIN_H = 0.06  # 이보다 낮으면 바닥 위 얼룩으로 본다(m)
PILE_MAX_Z = 2.4  # 이보다 높은 점은 더미가 아니다(벽 윗부분, 선반, 천장)
MAX_POINTS = 150_000
MAX_DEPTH = 4.5  # 카메라에서 이보다 먼 점은 쓰지 않는다(m)
MIN_CONSISTENT = 0.70  # 다중 시점 일관 점 비율이 이보다 낮으면 재구성을 믿지 않는다
MAX_CAM_HEIGHT_STD = (
    0.08  # 손에 든 카메라 높이의 흔들림(표준편차, m)이 이보다 크면 재구성을 믿지 않는다
)
# 휴대폰을 가슴 높이에 들고 찍는다는 안내의 가정 높이(m). 실제 크기의 두 번째 단서
HAND_HEIGHT = 1.4
# 실제 크기 퍼짐(%). 합성 평가(2026-11-20)에서 두 단서를 합친 실제 크기 오차의 중앙값이 약 18%였다
HAND_SPREAD = 20.0
VERSION = "recon-2026.11.20"


class ReconError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Models:
    """서버가 시작할 때 한 번 올리는 모델 둘."""

    geometry: Any
    scale: Any
    device: str = "cuda"
    names: dict[str, str] = field(
        default_factory=lambda: {"geometry": "VGGT-1B", "scale": "MoGe-2"}
    )


def load_models(device: str = "cuda") -> Models:
    import torch
    from moge.model.v2 import MoGeModel
    from vggt.models.vggt import VGGT

    torch.backends.cuda.matmul.allow_tf32 = True
    geometry = VGGT.from_pretrained("facebook/VGGT-1B").to(device).eval()
    scale = MoGeModel.from_pretrained("Ruicheng/moge-2-vitl-normal").to(device).eval()
    return Models(geometry=geometry, scale=scale, device=device)


# ── 입력 ────────────────────────────────────────────────────────────
def decode_images(blobs: list[bytes]) -> list[np.ndarray]:
    """JPEG·PNG 바이트를 RGB uint8로. 읽지 못하는 것이 있으면 BAD_IMAGE."""
    images = []
    for blob in blobs:
        array = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
        if array is None or array.ndim != 3 or min(array.shape[:2]) < 64:
            raise ReconError("BAD_IMAGE", "읽을 수 없는 사진이 있습니다.")
        images.append(cv2.cvtColor(array, cv2.COLOR_BGR2RGB))
    return images


def _target_size(h: int, w: int, long_side: int = 518) -> tuple[int, int]:
    """긴 변 518, 두 변 모두 14의 배수(VGGT 패치 크기)."""
    if w >= h:
        return max(14, round(h * long_side / w / 14) * 14), long_side
    return long_side, max(14, round(w * long_side / h / 14) * 14)


def same_orientation(images: list[np.ndarray]) -> tuple[list[np.ndarray], int]:
    """가로·세로가 섞이면 많은 쪽만 남긴다. (남긴 사진, 버린 수)."""
    landscape = [im for im in images if im.shape[1] >= im.shape[0]]
    portrait = [im for im in images if im.shape[1] < im.shape[0]]
    keep = landscape if len(landscape) >= len(portrait) else portrait
    return keep, len(images) - len(keep)


# ── 본 계산 ─────────────────────────────────────────────────────────
def run(
    models: Models, blobs: list[bytes], progress=lambda stage, ratio: None, max_frames: int = 96
) -> dict[str, Any]:
    """사진 바이트 목록 → {"result": RESULT, "points": RSPC 바이트, "debug": {...}}."""
    import torch
    from vggt.utils.geometry import unproject_depth_map_to_point_map
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri

    t0 = time.time()
    timings: dict[str, float] = {}
    warnings: list[str] = []
    originals = decode_images(blobs)
    received = len(originals)
    originals, dropped = same_orientation(originals)
    if len(originals) > max_frames:
        pick = np.linspace(0, len(originals) - 1, max_frames).round().astype(int)
        originals = [originals[i] for i in pick]
    if len(originals) < 2:
        raise ReconError("TOO_FEW_FRAMES", "장면 사진이 너무 적습니다.")
    h0, w0 = originals[0].shape[:2]
    H, W = _target_size(h0, w0)
    small = np.stack(
        [cv2.resize(im, (W, H), interpolation=cv2.INTER_AREA) for im in originals]
    )  # S,H,W,3
    S = small.shape[0]
    if float(small.mean()) / 255.0 < 0.18:
        warnings.append("dark")

    # 1. 기하 (VGGT)
    progress("geometry", 0.1)
    t = time.time()
    images = torch.from_numpy(small).to(models.device).permute(0, 3, 1, 2).float() / 255.0
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
        pred = models.geometry(images)
    extri, intri = pose_encoding_to_extri_intri(pred["pose_enc"], images.shape[-2:])
    depth = pred["depth"][0].float().cpu().numpy()  # S,H,W,1
    point_head = pred["world_points"][0].float().cpu().numpy()  # S,H,W,3 (점 지도 머리, 비교용)
    conf = pred["depth_conf"][0].float().cpu().numpy()  # S,H,W
    extri = extri[0].float().cpu().numpy()  # S,3,4 (world → camera, OpenCV)
    intri = intri[0].float().cpu().numpy()  # S,3,3
    del pred, images
    torch.cuda.empty_cache()
    world = unproject_depth_map_to_point_map(depth, extri, intri)  # S,H,W,3 (VGGT 단위)
    if os.environ.get("RECON_POINTS") == "head":
        world = point_head
    timings["geometry"] = round(time.time() - t, 2)

    # 2. 실제 크기 (MoGe-2)
    progress("scale", 0.45)
    t = time.time()
    fx = float(np.median(intri[:, 0, 0]))
    fov_x = math.degrees(2.0 * math.atan(W / (2.0 * fx)))
    ratios = []
    for i in np.unique(np.linspace(0, S - 1, min(S, 10)).round().astype(int)):
        tensor = torch.from_numpy(originals[i]).to(models.device).permute(2, 0, 1).float() / 255.0
        with torch.no_grad():
            out = models.scale.infer(tensor, fov_x=fov_x, use_fp16=True)
        d_m = cv2.resize(
            out["depth"].float().cpu().numpy(), (W, H), interpolation=cv2.INTER_NEAREST
        )
        m_m = (
            cv2.resize(
                out["mask"].cpu().numpy().astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST
            )
            > 0
        )
        d_v, c = depth[i, ..., 0], conf[i]
        ok = (
            m_m
            & np.isfinite(d_m)
            & (d_m > 0.05)
            & (d_m < 30)
            & (d_v > 1e-6)
            & (c >= np.percentile(c, 40))
        )
        if ok.sum() > 500:
            ratios.append(float(np.median(d_m[ok] / d_v[ok])))
    torch.cuda.empty_cache()
    method = "metric-depth"
    if len(ratios) >= 3:
        scale = float(np.median(ratios))
        q1, q3 = np.percentile(ratios, [25, 75])
        spread = float((q3 - q1) / 2.0 / scale * 100.0)
    else:
        scale, spread, method = 1.0, 100.0, "none"
    timings["scale"] = round(time.time() - t, 2)

    # 3. 점 모으기 (m)
    progress("floor", 0.6)
    t = time.time()
    consistent = _consistent(world, depth[..., 0], extri, intri, models.device)
    keep = (
        (conf >= max(float(np.percentile(conf, 30)), 1.0 + 1e-3))
        & _smooth(depth[..., 0])
        & consistent
    )
    consistent_frac = float(consistent.mean())
    pts = world[keep] * scale
    cols = small[keep]
    pdepth = (
        depth[..., 0][keep] * scale
    )  # 점마다 찍은 카메라에서의 깊이(m, 아래에서 크기를 다시 맞춘다)
    R, tr = extri[:, :, :3], extri[:, :, 3]
    centers = -np.einsum("sji,sj->si", R, tr) * scale  # 카메라 중심(VGGT 세계, m)
    up0 = -R[:, 1, :].mean(axis=0)
    up0 /= np.linalg.norm(up0) + 1e-12

    floor = _find_floor(pts, up0)
    if floor is None:
        warnings.append("no-floor")
        timings["floor"] = round(time.time() - t, 2)
        timings["total"] = round(time.time() - t0, 2)
        result = _empty_result(received, S, W, H, scale, spread, method, timings, models, warnings)
        return {
            "result": result,
            "points": encode_points(
                np.zeros((0, 3)), np.zeros((0, 3), np.uint8), np.zeros(0, np.uint8)
            ),
            "debug": {},
        }
    n, d, inliers = floor
    # 실제 크기의 두 단서를 합친다: (1) MoGe-2 깊이 비율, (2) 카메라 높이 = 가슴 높이(1.4m) 가정.
    # 두 값이 25% 안에서 맞으면 기하 평균을 쓰고, 어긋나면 카메라 높이 가정을 쓰고 scale-uncertain을 붙인다.
    cam_h0 = float(np.median(centers @ n + d))
    scale_depth = scale if method == "metric-depth" else None
    if cam_h0 > 1e-6:
        c = HAND_HEIGHT / cam_h0
        if method == "metric-depth" and abs(math.log(c)) <= math.log(1.25):
            f = math.sqrt(c)
            method = "metric-depth+camera-height"
            spread = max(spread, abs(math.log(c)) / 2.0 * 100.0, HAND_SPREAD * 0.75)
        else:
            f = c
            if method == "metric-depth":
                warnings.append("scale-uncertain")
            method = "camera-height"
            spread = HAND_SPREAD
        scale *= f
        pts *= f
        centers *= f
        pdepth *= f
        d *= f
    # 바닥 좌표계: z = n, x·y는 바닥 위. 바닥 칸의 최소 넓이 사각형에 축을 맞춘다
    e1 = np.cross(n, [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    fl = pts[inliers]
    origin = fl.mean(axis=0) - (fl.mean(axis=0) @ n + d) * n
    rel = fl - origin
    uv = np.stack([rel @ e1, rel @ e2], axis=1)
    angle = _dominant_angle(uv)
    ca, sa = math.cos(angle), math.sin(angle)
    ex = ca * e1 + sa * e2
    ey = np.cross(n, ex)
    basis = np.stack([ex, ey, n])  # 행이 축

    def to_floor(p: np.ndarray) -> np.ndarray:
        q = (p - origin) @ basis.T
        return q

    Q = to_floor(pts)
    cams = to_floor(centers)
    plane_rms = float(np.sqrt(np.mean(((fl @ n) + d) ** 2)))
    cam_std = float(np.std(cams[:, 2]))
    unreliable = consistent_frac < MIN_CONSISTENT or cam_std > MAX_CAM_HEIGHT_STD
    if unreliable and os.environ.get("RECON_GATE") != "off":
        # 합성 장면 평가(docs/v3/eval/2026-11-20-storage-scan.md)에서 궤적이 무너진 장면을 이 두 값으로 골라냈다
        raise ReconError(
            "UNRELIABLE",
            "장면을 3D로 맞추지 못했습니다. 휴대폰을 가로로 들고, 더미 줄을 2~3m 떨어져 마주 보며 옆으로 천천히 걸으며 다시 찍어 주세요.",
        )
    cam_h = float(np.median(cams[:, 2]))
    if spread > 20.0:
        warnings.append("scale-uncertain")
    timings["floor"] = round(time.time() - t, 2)

    # 4. 벽 지우기, 높이 지도, 더미
    progress("piles", 0.75)
    t = time.time()
    lo_xy = np.percentile(Q[np.abs(Q[:, 2]) < 0.05][:, :2], 0.5, axis=0) - 1.0
    hi_xy = np.percentile(Q[np.abs(Q[:, 2]) < 0.05][:, :2], 99.5, axis=0) + 1.0
    hi_xy = np.minimum(hi_xy, lo_xy + 40.0)
    inside = (
        np.all((Q[:, :2] >= lo_xy) & (Q[:, :2] <= hi_xy), axis=1)
        & (Q[:, 2] > -0.3)
        & (Q[:, 2] < 3.0)
        & (pdepth <= MAX_DEPTH)  # 멀리 찍힌 점(먼 벽)은 깊이 오차가 커서 버린다
    )
    Q, cols = Q[inside], cols[inside]
    grid = _Grid(lo_xy, hi_xy, GRID)
    above = (Q[:, 2] > 0.03) & (Q[:, 2] < PILE_MAX_Z)
    top, count = grid.top(Q[above])
    walls = _wall_cells(top, count, grid)
    top = np.where(walls, 0.0, top)
    count = np.where(walls, 0, count)
    piles_mask = _pile_masks(top, count)
    floor_cells = grid.occupancy(Q[np.abs(Q[:, 2]) < 0.03])
    piles = []
    labels_grid = np.zeros(grid.shape, np.int32)
    from scipy import ndimage

    any_pile = np.zeros(grid.shape, bool)
    for mask in piles_mask:
        any_pile |= mask
    seen_floor = ndimage.binary_dilation(floor_cells, structure=np.ones((3, 3)))
    unknown = ~(seen_floor | walls | any_pile | (count > 0))
    wall_near = (
        ndimage.binary_dilation(walls, structure=np.ones((21, 21))) if walls.any() else walls
    )
    for k, mask in enumerate(piles_mask):
        pile = _measure(mask, top, count, grid, spread, unknown, walls, wall_near)
        if pile is None:
            continue
        pile["id"] = f"p{len(piles) + 1}"
        labels_grid[mask] = len(piles) + 1
        piles.append(pile)
    for pile in piles:
        pile["thumbnail"] = _thumbnail(pile, originals, extri, intri, (H, W), origin, basis, scale)
    if not piles:
        warnings.append("few-piles")
    if any(p["coverage"] < 0.6 for p in piles):
        warnings.append("low-coverage")
    floor_poly, floor_area = _floor_polygon(floor_cells | (labels_grid > 0), grid)
    timings["piles"] = round(time.time() - t, 2)

    # 5. 점군(바닥 좌표계 mm)
    show = (Q[:, 2] > -0.05) & (Q[:, 2] < 2.6)
    Qs, Cs = Q[show], cols[show]
    Qs, Cs = _voxel_down(Qs, Cs, 0.02, MAX_POINTS)
    lab = np.zeros(len(Qs), np.uint8)
    ij = grid.index(Qs[:, :2])
    okij = (
        (ij[:, 0] >= 0) & (ij[:, 1] >= 0) & (ij[:, 0] < grid.shape[0]) & (ij[:, 1] < grid.shape[1])
    )
    lab_ok = np.zeros(len(Qs), np.int32)
    lab_ok[okij] = labels_grid[ij[okij, 0], ij[okij, 1]]
    lab = np.where(Qs[:, 2] > 0.03, lab_ok, 0).astype(np.uint8)
    points = encode_points(Qs, Cs, lab)

    path_idx = np.unique(np.linspace(0, S - 1, min(S, 64)).round().astype(int))
    timings["total"] = round(time.time() - t0, 2)
    bounds = (
        {"min": _r(Qs.min(axis=0).tolist(), 3), "max": _r(Qs.max(axis=0).tolist(), 3)}
        if len(Qs)
        else None
    )
    result = {
        "frames": {"received": received, "used": S, "width": W, "height": H},
        "scale": {
            "metersPerUnit": round(scale, 5),
            "spreadPct": round(spread, 2),
            "method": method,
        },
        "floor": {
            "areaM2": round(floor_area, 2),
            "polygon": floor_poly,
            "planeRmsM": round(plane_rms, 4),
        },
        "camera": {
            "path": [_r(c.tolist(), 3) for c in cams[path_idx]],
            "start": _r(cams[0, :2].tolist(), 3),
        },
        "piles": piles,
        "bounds": bounds,
        "pointsCount": len(Qs),
        "timingsSec": timings,
        "models": dict(models.names),
        "warnings": sorted(set(warnings)),
    }
    if dropped:
        result["frames"]["dropped"] = dropped
    debug = {
        "consistentFrac": consistent_frac,
        "unreliable": unreliable,
        "camHeightStdM": float(np.std(cams[:, 2])),
        "floorInlierFrac": float(inliers.mean()),
        "cameras": cams,
        "scaleRatios": ratios,
        "cameraHeightM": cam_h,
        "up": n,
        "scaleDepth": scale_depth,
        "scaleFinal": scale,
        "scaleMethod": method,
    }
    return {"result": result, "points": points, "debug": debug}


def _smooth(depth: np.ndarray, rel: float = 0.04) -> np.ndarray:
    """깊이 경계의 떠 있는 점(앞 물체와 뒤 벽 사이에 번진 점)을 지운다: 이웃 칸과 깊이가 4%보다 크게 다르면 버린다."""
    d = depth
    diff = np.zeros_like(d)
    diff[:, 1:, :] = np.maximum(diff[:, 1:, :], np.abs(d[:, 1:, :] - d[:, :-1, :]))
    diff[:, :-1, :] = np.maximum(diff[:, :-1, :], np.abs(d[:, 1:, :] - d[:, :-1, :]))
    diff[:, :, 1:] = np.maximum(diff[:, :, 1:], np.abs(d[:, :, 1:] - d[:, :, :-1]))
    diff[:, :, :-1] = np.maximum(diff[:, :, :-1], np.abs(d[:, :, 1:] - d[:, :, :-1]))
    return diff < rel * np.maximum(d, 1e-6)


def _consistent(
    world, depth, extri, intri, device, k: int = 4, tol: float = 0.03, min_views: int = 2
) -> np.ndarray:
    """다중 시점 일관성 걸러내기: 프레임 i의 점을 앞뒤 k장에 투영해 그 프레임의 깊이와 3% 안에서 맞는 시점이 2개 이상인 점만 남긴다.

    프레임마다 따로 낸 깊이가 서로 어긋나 벽이 여러 겹으로 생기는 것을 막는다(깊이 지도 융합의 표준 검사).
    """
    import torch

    S, H, W = depth.shape
    Wt = torch.from_numpy(world).to(device).float()  # S,H,W,3
    D = torch.from_numpy(depth).to(device).float()
    E = torch.from_numpy(extri).to(device).float()
    K = torch.from_numpy(intri).to(device).float()
    good = torch.zeros((S, H, W), dtype=torch.int32, device=device)
    for i in range(S):
        Xw = Wt[i].reshape(-1, 3)
        for j in range(max(0, i - k), min(S, i + k + 1)):
            if j == i:
                continue
            Xc = Xw @ E[j, :, :3].T + E[j, :, 3]
            z = Xc[:, 2]
            uv = Xc @ K[j].T
            u = torch.round(uv[:, 0] / uv[:, 2].clamp(min=1e-6)).long()
            v = torch.round(uv[:, 1] / uv[:, 2].clamp(min=1e-6)).long()
            ok = (z > 1e-6) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
            dj = torch.zeros_like(z)
            dj[ok] = D[j, v[ok], u[ok]]
            cons = ok & (torch.abs(z - dj) < tol * dj)
            good[i] += cons.reshape(H, W).int()
    return (good >= min_views).cpu().numpy()


def _empty_result(received, S, W, H, scale, spread, method, timings, models, warnings):
    return {
        "frames": {"received": received, "used": S, "width": W, "height": H},
        "scale": {
            "metersPerUnit": round(scale, 5),
            "spreadPct": round(spread, 2),
            "method": method,
        },
        "floor": None,
        "camera": None,
        "piles": [],
        "bounds": None,
        "pointsCount": 0,
        "timingsSec": timings,
        "models": dict(models.names),
        "warnings": sorted(set(warnings)),
    }


def _r(values, nd):
    return [round(float(v), nd) for v in values]


# ── 바닥 ────────────────────────────────────────────────────────────
def _find_floor(pts: np.ndarray, up0: np.ndarray, iters: int = 400, seed: int = 0):
    """위쪽(up0)에서 25° 안의 평면 중 점이 가장 많은 것. (법선, d, 바닥 점 표시) 또는 None."""
    rng = np.random.default_rng(seed)
    sample = pts if len(pts) <= 200_000 else pts[rng.choice(len(pts), 200_000, replace=False)]
    h = sample @ up0
    low = sample[h <= np.percentile(h, 55)]
    if len(low) < 100:
        return None
    best, best_count = None, 0
    cos_lim = math.cos(math.radians(25))
    for _ in range(iters):
        a, b, c = low[rng.choice(len(low), 3, replace=False)]
        nrm = np.cross(b - a, c - a)
        norm = np.linalg.norm(nrm)
        if norm < 1e-9:
            continue
        nrm /= norm
        if nrm @ up0 < 0:
            nrm = -nrm
        if nrm @ up0 < cos_lim:
            continue
        d = -nrm @ a
        cnt = int((np.abs(sample @ nrm + d) < FLOOR_TOL).sum())
        if cnt > best_count:
            best, best_count = (nrm, d), cnt
    if best is None or best_count < 0.08 * len(sample):
        return None
    nrm, d = best
    for tol in (FLOOR_TOL * 1.5, FLOOR_TOL):
        sel = sample[np.abs(sample @ nrm + d) < tol]
        center = sel.mean(axis=0)
        _, _, vt = np.linalg.svd(sel - center, full_matrices=False)
        nrm = vt[2] if vt[2] @ up0 > 0 else -vt[2]
        d = -nrm @ center
    inliers = np.abs(pts @ nrm + d) < FLOOR_TOL
    return nrm, float(d), inliers


def _dominant_angle(uv: np.ndarray) -> float:
    """바닥 점이 놓인 칸들의 최소 넓이 사각형 방향(라디안). 방이 축에 맞게 놓이도록."""
    g = 0.1
    lo = uv.min(axis=0)
    ij = np.unique(((uv - lo) / g).astype(np.int32), axis=0).astype(np.float32)
    if len(ij) < 10:
        return 0.0
    (_, _), (_, _), ang = cv2.minAreaRect(ij)
    return math.radians(ang)


def _wall_cells(
    top: np.ndarray, count: np.ndarray, grid: _Grid, rounds: int = 10, seed: int = 1
) -> np.ndarray:
    """높이 지도에서 벽 칸을 찾는다: 1.3m보다 높은 칸들이 1.5m 넘게 곧게 늘어선 줄(2차원 RANSAC 직선)과 그 양옆 12cm.

    더미(톤백 약 1m, 팔레트 적재 약 0.8~1.5m)는 길게 곧은 높은 줄을 만들지 않는다. 2m 넘게 쌓은 더미가 벽에 붙어 있으면
    벽과 함께 지워질 수 있다(한계).
    """
    rng = np.random.default_rng(seed)
    walls = np.zeros(top.shape, bool)
    rows, cols = np.nonzero((top > 1.3) & (count >= 2))
    if len(rows) < 40:
        return walls
    pts = grid.xy(rows, cols)
    alive = np.ones(len(pts), bool)
    all_r, all_c = np.indices(top.shape)
    for _ in range(rounds):
        idx = np.nonzero(alive)[0]
        if len(idx) < 40:
            break
        best, best_in = None, None
        for _ in range(300):
            a, b = pts[rng.choice(idx, 2, replace=False)]
            v = b - a
            L = np.linalg.norm(v)
            if L < 0.3:
                continue
            u = v / L
            nrm = np.array([-u[1], u[0]])
            dist = np.abs((pts[idx] - a) @ nrm)
            inl = idx[dist < 0.08]
            if best_in is None or len(inl) > len(best_in):
                best, best_in = (a, u, nrm), inl
        if best is None:
            break
        a, u, nrm = best
        along = (pts[best_in] - a) @ u
        span = float(np.percentile(along, 99) - np.percentile(along, 1))
        density = len(best_in) * grid.g / max(span, 1e-6)
        alive[best_in] = False
        if span < 1.5 or density < 0.3:
            continue
        lo, hi = np.percentile(along, 1) - 0.1, np.percentile(along, 99) + 0.1
        cxy = grid.xy(all_r.ravel(), all_c.ravel())
        d = np.abs((cxy - a) @ nrm)
        t = (cxy - a) @ u
        walls |= ((d < 0.12) & (t > lo) & (t < hi)).reshape(top.shape)
    return walls


def _wall_mask(Q: np.ndarray, rounds: int = 6, seed: int = 1) -> np.ndarray:
    """크고 수직인 면(가로 1.6m 넘고 세로 1.4m 넘고 1.6m보다 높이 올라가는 면)의 점을 표시한다."""
    rng = np.random.default_rng(seed)
    wall = np.zeros(len(Q), bool)
    cand_all = np.where((Q[:, 2] > 0.1) & (Q[:, 2] < 3.0))[0]
    for _ in range(rounds):
        cand = cand_all[~wall[cand_all]]
        if len(cand) < 500:
            break
        sub = cand if len(cand) <= 60_000 else rng.choice(cand, 60_000, replace=False)
        P = Q[sub]
        best, best_cnt = None, 0
        for _ in range(250):
            a, b = P[rng.choice(len(P), 2, replace=False)]
            v = b - a
            v[2] = 0.0
            if np.linalg.norm(v) < 0.3:
                continue
            nrm = np.array([-v[1], v[0], 0.0]) / np.linalg.norm(v)
            d = -nrm @ a
            cnt = int((np.abs(P @ nrm + d) < 0.03).sum())
            if cnt > best_cnt:
                best, best_cnt = (nrm, d), cnt
        if best is None:
            break
        nrm, d = best
        sel = cand[np.abs(Q[cand] @ nrm + d) < 0.04]
        if len(sel) < 300:
            break
        along = Q[sel][:, :2] @ np.array([-nrm[1], nrm[0]])
        span = np.percentile(along, 98) - np.percentile(along, 2)
        zs = Q[sel][:, 2]
        tall = np.percentile(zs, 98)
        vspan = tall - np.percentile(zs, 2)
        if span > 1.6 and vspan > 1.4 and tall > 1.6:
            wall[sel] = True
        else:
            break
    return wall


# ── 격자 ────────────────────────────────────────────────────────────
class _Grid:
    def __init__(self, lo: np.ndarray, hi: np.ndarray, g: float) -> None:
        self.lo, self.g = lo.astype(np.float64), g
        self.shape = (
            math.ceil((hi[1] - lo[1]) / g) + 1,
            math.ceil((hi[0] - lo[0]) / g) + 1,
        )  # 행=y, 열=x

    def index(self, xy: np.ndarray) -> np.ndarray:
        col = np.floor((xy[:, 0] - self.lo[0]) / self.g).astype(np.int64)
        row = np.floor((xy[:, 1] - self.lo[1]) / self.g).astype(np.int64)
        return np.stack([row, col], axis=1)

    def xy(self, row: np.ndarray, col: np.ndarray) -> np.ndarray:
        return np.stack(
            [self.lo[0] + (col + 0.5) * self.g, self.lo[1] + (row + 0.5) * self.g], axis=-1
        )

    def _flat(self, P: np.ndarray):
        ij = self.index(P[:, :2])
        ok = (
            (ij[:, 0] >= 0)
            & (ij[:, 1] >= 0)
            & (ij[:, 0] < self.shape[0])
            & (ij[:, 1] < self.shape[1])
        )
        return ij[ok, 0] * self.shape[1] + ij[ok, 1], P[ok]

    def top(self, P: np.ndarray, q: float = 0.95):
        """칸마다 높이의 q 분위수와 점 개수."""
        top = np.zeros(self.shape[0] * self.shape[1], np.float32)
        count = np.zeros_like(top, dtype=np.int32)
        if len(P):
            flat, P = self._flat(P)
            order = np.lexsort((P[:, 2], flat))
            flat, z = flat[order], P[order, 2]
            cells, start, cnt = np.unique(flat, return_index=True, return_counts=True)
            top[cells] = z[start + np.floor(q * (cnt - 1)).astype(np.int64)]
            count[cells] = cnt
        return top.reshape(self.shape), count.reshape(self.shape)

    def occupancy(self, P: np.ndarray) -> np.ndarray:
        occ = np.zeros(self.shape[0] * self.shape[1], bool)
        if len(P):
            flat, _ = self._flat(P)
            occ[flat] = True
        return occ.reshape(self.shape)


def _pile_masks(top: np.ndarray, count: np.ndarray) -> list[np.ndarray]:
    from scipy import ndimage

    occ = (top >= PILE_MIN_H) & (count >= 2)
    occ = ndimage.binary_opening(
        occ, structure=np.ones((5, 5))
    )  # 벽 자국처럼 10cm보다 얇은 줄을 지운다
    occ = ndimage.binary_closing(occ, structure=np.ones((5, 5)))
    occ = ndimage.binary_fill_holes(occ)
    labels, n = ndimage.label(occ, structure=np.ones((3, 3)))
    masks = []
    for k in range(1, n + 1):
        m = labels == k
        if m.sum() >= 150:  # 0.06 m² 미만은 버린다
            masks.append(m)
    return masks


def _behind(
    mask: np.ndarray,
    unknown: np.ndarray,
    walls: np.ndarray,
    wall_near: np.ndarray,
    max_cells: int = 60,
) -> np.ndarray:
    """벽에 붙여 쌓은 더미의 가려진 뒤쪽: 더미에서 보이지 않은 칸(바닥도 더미도 보이지 않은 칸)을 따라 1.2m까지 넓혀
    벽 근처(20cm)에 닿으면, 벽에서 10cm를 남기고 그 칸들을 더미의 가려진 뒤쪽으로 본다. 닿지 않으면 넓히지 않는다."""
    from scipy import ndimage

    if not walls.any():
        return np.zeros_like(mask)
    grown = mask.copy()
    st = np.ones((3, 3), bool)
    for _ in range(max_cells):
        nxt = grown | (ndimage.binary_dilation(grown, structure=st) & unknown)
        if (nxt == grown).all():
            break
        grown = nxt
    extra = grown & ~mask
    if not (extra & wall_near).any():
        return np.zeros_like(mask)
    keep_off = ndimage.binary_dilation(walls, structure=np.ones((11, 11)))
    extra &= ~keep_off
    # 가려진 칸 가운데 더미와 이어진 덩어리만
    lab, _ = ndimage.label(extra | mask, structure=st)
    ids = np.unique(lab[mask])
    return np.isin(lab, ids[ids > 0]) & extra


def _measure(
    mask, top, count, grid: _Grid, spread: float, unknown=None, walls=None, wall_near=None
) -> dict[str, Any] | None:
    from scipy import ndimage

    observed = mask & (count >= 1) & (top > 0.03)
    if observed.sum() < 50:
        return None
    # 덜 찍힌 칸은 가장 가까운 보인 칸의 높이로 채운다
    _, (ri, ci) = ndimage.distance_transform_edt(~observed, return_indices=True)
    heights = np.where(observed, top, top[ri, ci])
    h_in = heights[mask]
    volume_seen = float(h_in.sum() * grid.g * grid.g)
    hidden = (
        _behind(mask, unknown, walls, wall_near) if unknown is not None else np.zeros_like(mask)
    )
    volume_hidden = float(heights[hidden].sum() * grid.g * grid.g)
    # 값은 보인 부피다. 가려진 뒤쪽까지 찼을 때의 부피는 범위의 위 끝에만 넣는다(벽에 붙은 더미는 값이 작게 나올 수 있다)
    volume = volume_seen
    h95 = float(np.percentile(h_in, 95))
    rect0 = cv2.minAreaRect(np.stack(np.nonzero(mask)[::-1], axis=1).astype(np.float32))
    if volume < 0.02 or h95 < 0.1 or min(rect0[1]) * grid.g < 0.2:  # 20cm보다 얇은 줄은 벽 자국이다
        return None
    rows, cols = np.nonzero(mask)
    full = ndimage.binary_fill_holes(
        ndimage.binary_closing(mask | hidden, structure=np.ones((9, 9)))
    )
    coverage = float(observed.sum() / max(full.sum(), 1))
    rect = cv2.minAreaRect(np.stack([cols, rows], axis=1).astype(np.float32))
    box = cv2.boxPoints(rect)  # (col,row)
    corners = grid.xy(box[:, 1] - 0.0, box[:, 0] - 0.0)
    corners = corners if _signed_area(corners) > 0 else corners[::-1]
    center = grid.xy(np.array([rect[0][1]]), np.array([rect[0][0]]))[0]
    e0, e1 = corners[1] - corners[0], corners[2] - corners[1]
    long_edge = e0 if np.linalg.norm(e0) >= np.linalg.norm(e1) else e1
    yaw = math.degrees(math.atan2(long_edge[1], long_edge[0]))
    yaw = (yaw + 90.0) % 180.0 - 90.0
    size = sorted(
        [float(np.linalg.norm(e0)) + grid.g, float(np.linalg.norm(e1)) + grid.g], reverse=True
    )
    err = math.sqrt((3.0 * spread / 100.0) ** 2 + 0.05**2)
    lo_v = volume_seen * max(0.0, 1.0 - err)
    hi_v = (volume_seen + volume_hidden) * (1.0 + err)
    return {
        "footprint": [_r(c, 3) for c in corners.tolist()],
        "center": _r(center.tolist(), 3),
        "sizeM": _r(size, 3),
        "yawDeg": round(yaw, 1),
        "heightM": round(h95, 3),
        "maxHeightM": round(float(h_in.max()), 3),
        "volumeM3": round(volume, 3),
        "volumeRangeM3": [round(lo_v, 3), round(hi_v, 3)],
        "coverage": round(min(coverage, 1.0), 3),
        "points": int(count[mask].sum()),
    }


def _signed_area(poly: np.ndarray) -> float:
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _floor_polygon(cells: np.ndarray, grid: _Grid) -> tuple[list[list[float]], float]:
    from scipy import ndimage

    f = 5  # 2cm → 10cm 칸
    h, w = cells.shape[0] // f * f, cells.shape[1] // f * f
    coarse = cells[:h, :w].reshape(h // f, f, w // f, f).any(axis=(1, 3))
    coarse = ndimage.binary_closing(coarse, structure=np.ones((5, 5)))
    labels, n = ndimage.label(coarse)
    if n == 0:
        return [], 0.0
    sizes = ndimage.sum(coarse, labels, range(1, n + 1))
    main = ndimage.binary_fill_holes(labels == (int(np.argmax(sizes)) + 1))
    contours, _ = cv2.findContours(
        main.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    c = max(contours, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, 1.5, True)[:, 0, :].astype(np.float64)  # (col,row) 10cm 칸
    gc = grid.g * f
    poly = np.stack([grid.lo[0] + (c[:, 0] + 0.5) * gc, grid.lo[1] + (c[:, 1] + 0.5) * gc], axis=1)
    poly = poly if _signed_area(poly) > 0 else poly[::-1]
    return [_r(p, 3) for p in poly.tolist()], float(main.sum() * gc * gc)


# ── 썸네일 ──────────────────────────────────────────────────────────
def _thumbnail(pile, originals, extri, intri, hw, origin, basis, scale) -> str | None:
    H, W = hw
    fp = np.array(pile["footprint"])
    corners = np.concatenate(
        [np.c_[fp, np.zeros(4)], np.c_[fp, np.full(4, pile["heightM"])]]
    )  # 바닥 좌표
    world = (origin + corners @ basis) / scale  # VGGT 세계 좌표
    best, best_score = None, -1.0
    for i in range(len(extri)):
        cam = world @ extri[i, :, :3].T + extri[i, :, 3]
        if np.any(cam[:, 2] <= 1e-6):
            continue
        uv = cam @ intri[i].T
        uv = uv[:, :2] / uv[:, 2:3]
        x0, y0 = uv.min(axis=0)
        x1, y1 = uv.max(axis=0)
        area = (x1 - x0) * (y1 - y0)
        if area <= 0:
            continue
        ix0, iy0, ix1, iy1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
        inside = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0) / area
        frac = area / (W * H)
        if inside < 0.7 or frac > 0.8:
            continue
        score = inside * min(frac, 0.5)
        if score > best_score:
            best, best_score = (i, ix0, iy0, ix1, iy1), score
    if best is None:
        return None
    i, x0, y0, x1, y1 = best
    img = originals[i]
    sx, sy = img.shape[1] / W, img.shape[0] / H
    mx, my = (x1 - x0) * 0.1, (y1 - y0) * 0.1
    a0, b0 = int(max(0, (x0 - mx) * sx)), int(max(0, (y0 - my) * sy))
    a1, b1 = int(min(img.shape[1], (x1 + mx) * sx)), int(min(img.shape[0], (y1 + my) * sy))
    if a1 - a0 < 8 or b1 - b0 < 8:
        return None
    crop = img[b0:b1, a0:a1]
    k = 160.0 / max(crop.shape[:2])
    crop = cv2.resize(
        crop,
        (max(1, round(crop.shape[1] * k)), max(1, round(crop.shape[0] * k))),
        interpolation=cv2.INTER_AREA,
    )
    ok, jpg = cv2.imencode(
        ".jpg", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 75]
    )
    return "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode() if ok else None


# ── 점군 ────────────────────────────────────────────────────────────
def _voxel_down(P: np.ndarray, C: np.ndarray, v: float, cap: int, seed: int = 2):
    if not len(P):
        return P, C
    key = np.floor(P / v).astype(np.int64)
    _, first = np.unique(key, axis=0, return_index=True)
    P, C = P[first], C[first]
    if len(P) > cap:
        pick = np.random.default_rng(seed).choice(len(P), cap, replace=False)
        P, C = P[pick], C[pick]
    return P, C


def encode_points(P: np.ndarray, C: np.ndarray, labels: np.ndarray) -> bytes:
    """RSPC 1판: 머리 16바이트 + 점마다 int16 x,y,z(mm), uint8 r,g,b, uint8 더미 번호."""
    n = len(P)
    rec = np.zeros(
        n,
        dtype=[
            ("x", "<i2"),
            ("y", "<i2"),
            ("z", "<i2"),
            ("r", "u1"),
            ("g", "u1"),
            ("b", "u1"),
            ("k", "u1"),
        ],
    )
    mm = (
        np.clip(np.round(P * 1000.0), -32767, 32767).astype(np.int16)
        if n
        else np.zeros((0, 3), np.int16)
    )
    if n:
        rec["x"], rec["y"], rec["z"] = mm[:, 0], mm[:, 1], mm[:, 2]
        rec["r"], rec["g"], rec["b"] = C[:, 0], C[:, 1], C[:, 2]
        rec["k"] = labels
    return b"RSPC" + struct.pack("<III", 1, n, 0) + rec.tobytes()


def decode_points(blob: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    assert blob[:4] == b"RSPC"
    _, n, _ = struct.unpack("<III", blob[4:16])
    rec = np.frombuffer(
        blob[16:],
        dtype=[
            ("x", "<i2"),
            ("y", "<i2"),
            ("z", "<i2"),
            ("r", "u1"),
            ("g", "u1"),
            ("b", "u1"),
            ("k", "u1"),
        ],
        count=n,
    )
    P = np.stack([rec["x"], rec["y"], rec["z"]], axis=1).astype(np.float32) / 1000.0
    C = np.stack([rec["r"], rec["g"], rec["b"]], axis=1)
    return P, C, rec["k"].copy()
