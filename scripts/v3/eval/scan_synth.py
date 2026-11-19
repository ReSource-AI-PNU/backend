"""보관장 3D 스캔 평가용 합성 장면을 정답과 함께 렌더한다. Blender(bpy, 4.5)에서 돈다(H100: /opt/rsai-recon/blender).

    blender -b --python scripts/v3/eval/scan_synth.py -- --out /mnt/recon/synth/s01 --seed 1
    blender -b --python scripts/v3/eval/scan_synth.py -- --out /mnt/recon/synth/hanbit --demo

장면: 콘크리트 바닥, 벽 넷(앞벽에 출입구), 천장 조명(밝음·어두움). 더미 종류는 톤백, 마대 묶음, 200L 드럼, 팔레트 위 상자,
흩어진 더미(언덕형), 필름 묶음이다. 카메라는 1.4~1.7m 높이에서 손에 든 것처럼 흔들리며 한 바퀴 돈다(방 가운데에서 벽 쪽을
보거나, 가운데 더미 섬을 둘러보며 안쪽을 본다). 30fps 영상의 셔터(1/60초)만큼 모션 블러를 넣고, 브라우저가 보내는 꼴(2fps로
고른 장면, 긴 변 896px JPEG)로 저장한다.

정답(truth.json): 더미마다 위에서 1cm 격자로 광선을 쏘아 잰 윗면 높이 적분 부피(envelopeVolumeM3, 파이프라인이 재는 것과
같은 정의: 밑이 꽉 찬 더미), 볼록 껍질, 최소 넓이 사각형, 높이, 종류. 카메라 위치(프레임마다).
"""

from __future__ import annotations

import json
import math
import os
import random
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Vector

KINDS = ("tonbag", "sacks", "drums", "pallet", "heap", "film")


# ── 인자 ────────────────────────────────────────────────────────────
def _args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = {
        "out": None,
        "seed": 1,
        "demo": False,
        "frames": None,
        "samples": 32,
        "tex": "/opt/rsai-recon/tex",
        "orient": None,
    }
    i = 0
    while i < len(argv):
        key = argv[i].lstrip("-")
        if key == "demo":
            out["demo"] = True
            i += 1
            continue
        out[key] = argv[i + 1]
        i += 2
    out["seed"] = int(out["seed"])
    out["samples"] = int(out["samples"])
    if out["frames"] is not None:
        out["frames"] = int(out["frames"])
    return out


# ── 재질 ────────────────────────────────────────────────────────────
def _mat(
    name,
    color,
    rough=0.7,
    metal=0.0,
    noise=None,
    bump=0.0,
    stripes=None,
    voronoi=None,
    transmission=0.0,
    brick=None,
):
    """Principled BSDF 하나. noise=(scale, 섞을 색), stripes=(scale, 색), voronoi=(scale, [색들])로 무늬를 더한다."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = transmission
    coord = nt.nodes.new("ShaderNodeTexCoord")
    base = nt.nodes.new("ShaderNodeRGB")
    base.outputs[0].default_value = (*color, 1.0)
    col_out = base.outputs[0]
    if noise is not None:
        tex = nt.nodes.new("ShaderNodeTexNoise")
        tex.inputs["Scale"].default_value = noise[0]
        tex.inputs["Detail"].default_value = 8.0
        nt.links.new(coord.outputs["Object"], tex.inputs["Vector"])
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        nt.links.new(tex.outputs["Fac"], mix.inputs["Factor"])
        nt.links.new(col_out, mix.inputs[6])
        mix.inputs[7].default_value = (*noise[1], 1.0)
        col_out = mix.outputs[2]
    if voronoi is not None:
        vor = nt.nodes.new("ShaderNodeTexVoronoi")
        vor.inputs["Scale"].default_value = voronoi[0]
        nt.links.new(coord.outputs["Object"], vor.inputs["Vector"])
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        els = ramp.color_ramp.elements
        cols = voronoi[1]
        els[0].color = (*cols[0], 1.0)
        els[1].color = (*cols[-1], 1.0)
        for j, c in enumerate(cols[1:-1], start=1):
            e = els.new(j / (len(cols) - 1))
            e.color = (*c, 1.0)
        ramp.color_ramp.interpolation = "CONSTANT"
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(vor.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs[0], ramp.inputs["Fac"])
        col_out = ramp.outputs["Color"]
    if stripes is not None:
        wave = nt.nodes.new("ShaderNodeTexWave")
        wave.inputs["Scale"].default_value = stripes[0]
        nt.links.new(coord.outputs["Object"], wave.inputs["Vector"])
        mix2 = nt.nodes.new("ShaderNodeMix")
        mix2.data_type = "RGBA"
        gt = nt.nodes.new("ShaderNodeMath")
        gt.operation = "GREATER_THAN"
        gt.inputs[1].default_value = 0.8
        nt.links.new(wave.outputs["Fac"], gt.inputs[0])
        nt.links.new(gt.outputs[0], mix2.inputs["Factor"])
        nt.links.new(col_out, mix2.inputs[6])
        mix2.inputs[7].default_value = (*stripes[1], 1.0)
        col_out = mix2.outputs[2]
    if brick is not None:
        # brick = (크기, 둘째 색, 줄눈 색, 줄눈 두께, 벽돌 폭, 줄 높이)
        br = nt.nodes.new("ShaderNodeTexBrick")
        nt.links.new(coord.outputs["Object"], br.inputs["Vector"])
        nt.links.new(col_out, br.inputs["Color1"])
        br.inputs["Color2"].default_value = (*brick[1], 1.0)
        br.inputs["Mortar"].default_value = (*brick[2], 1.0)
        br.inputs["Scale"].default_value = brick[0]
        br.inputs["Mortar Size"].default_value = brick[3]
        br.inputs["Brick Width"].default_value = brick[4]
        br.inputs["Row Height"].default_value = brick[5]
        col_out = br.outputs["Color"]
    nt.links.new(col_out, bsdf.inputs["Base Color"])
    if bump > 0:
        bt = nt.nodes.new("ShaderNodeTexNoise")
        bt.inputs["Scale"].default_value = 60.0
        nt.links.new(coord.outputs["Object"], bt.inputs["Vector"])
        bn = nt.nodes.new("ShaderNodeBump")
        bn.inputs["Strength"].default_value = bump
        nt.links.new(bt.outputs["Fac"], bn.inputs["Height"])
        nt.links.new(bn.outputs["Normal"], bsdf.inputs["Normal"])
    return m


FLOOR_TEX = (
    "concrete_floor_worn_001",
    "garage_floor",
    "hangar_concrete_floor",
    "dirty_concrete",
    "concrete_floor_02",
)
WALL_TEX = (
    "concrete_block_wall",
    "concrete_wall_008",
    "box_profile_metal_sheet",
    "beige_wall_001",
    "painted_concrete",
)


def _imgmat(name, path, tile_m, rough=0.8, box=False):
    """사진 질감(Poly Haven CC0 diffuse 2k) 재질. tile_m 미터마다 한 번 되풀이한다."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = rough
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (1.0 / tile_m, 1.0 / tile_m, 1.0 / tile_m)
    nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    img = nt.nodes.new("ShaderNodeTexImage")
    img.image = bpy.data.images.load(path, check_existing=True)
    if box:
        img.projection = "BOX"
        img.projection_blend = 0.15
    nt.links.new(mapping.outputs["Vector"], img.inputs["Vector"])
    nt.links.new(img.outputs["Color"], bsdf.inputs["Base Color"])
    return m


def _assign(obj, mat):
    obj.data.materials.clear()
    obj.data.materials.append(mat)


# ── 도형 ────────────────────────────────────────────────────────────
def _cube(name, size, loc, rot_z=0.0, bevel=0.0, mat=None, cast=0.0):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.scale = size
    o.rotation_euler = (0, 0, rot_z)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel > 0:
        md = o.modifiers.new("bevel", "BEVEL")
        md.width = bevel
        md.segments = 4
    if cast > 0:
        sub = o.modifiers.new("sub", "SUBSURF")
        sub.levels = 2
        sub.render_levels = 2
        md = o.modifiers.new("cast", "CAST")
        md.factor = cast
    if mat:
        _assign(o, mat)
    return o


def _cyl(name, r, h, loc, mat):
    bpy.ops.mesh.primitive_cylinder_add(
        radius=r, depth=h, vertices=48, location=(loc[0], loc[1], h / 2)
    )
    o = bpy.context.active_object
    o.name = name
    _assign(o, mat)
    return o


def _blob(name, size, loc, seed, mat, flat=0.35):
    bpy.ops.mesh.primitive_ico_sphere_add(
        subdivisions=5, radius=0.5, location=(loc[0], loc[1], size[2] * (0.5 - flat * 0.5))
    )
    o = bpy.context.active_object
    o.name = name
    o.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    tex = bpy.data.textures.new(f"{name}_tex", "CLOUDS")
    tex.noise_scale = 0.35
    md = o.modifiers.new("disp", "DISPLACE")
    md.texture = tex
    md.strength = 0.12
    md.texture_coords = "GLOBAL"
    o.rotation_euler = (0, 0, seed % 7)
    _assign(o, mat)
    return o


def _heap(name, radius, height, loc, seed, mat):
    """언덕형 흩어진 더미: 높이 h(r) = H(1-(r/R)²)^0.8에 잔물결."""
    rng = random.Random(seed)
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    n_r, n_t = 28, 64
    phase = [rng.uniform(0, 6.28) for _ in range(3)]
    rows = []
    for i in range(n_r + 1):
        rr = radius * i / n_r
        row = []
        for j in range(n_t if i else 1):
            t = 2 * math.pi * j / n_t
            wob = 1.0 + 0.12 * math.sin(3 * t + phase[0]) + 0.06 * math.sin(5 * t + phase[1])
            rad = rr * wob
            z = (
                height
                * max(0.0, 1 - (i / n_r) ** 2) ** 0.8
                * (1 + 0.04 * math.sin(7 * t + phase[2]))
            )
            row.append(
                bm.verts.new(
                    (loc[0] + rad * math.cos(t), loc[1] + rad * math.sin(t), max(z, 0.0) - 0.005)
                )
            )
        rows.append(row)
    for j in range(n_t):
        bm.faces.new((rows[0][0], rows[1][j], rows[1][(j + 1) % n_t]))
    for i in range(1, n_r):
        for j in range(n_t):
            a, b = rows[i][j], rows[i][(j + 1) % n_t]
            c, d = rows[i + 1][(j + 1) % n_t], rows[i + 1][j]
            bm.faces.new((a, d, c, b))
    bm.faces.new(list(reversed(rows[n_r])))
    bm.to_mesh(mesh)
    bm.free()
    o = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(o)
    for p in mesh.polygons:
        p.use_smooth = True
    _assign(o, mat)
    return o


# ── 더미 만들기 ─────────────────────────────────────────────────────
class Builder:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.mats = {
            "bag": [
                _mat(f"bag{i}", c, 0.85, noise=(40.0, (0.6, 0.6, 0.55)), bump=0.3, stripes=(2.5, s))
                for i, (c, s) in enumerate(
                    [
                        ((0.86, 0.85, 0.8), (0.1, 0.25, 0.6)),
                        ((0.8, 0.82, 0.86), (0.7, 0.15, 0.1)),
                        ((0.9, 0.88, 0.78), (0.15, 0.45, 0.2)),
                    ]
                )
            ],
            "sack": [
                _mat(
                    f"sack{i}",
                    c,
                    0.8,
                    noise=(30.0, (0.5, 0.55, 0.6)),
                    bump=0.4,
                    stripes=(4.0, (0.9, 0.9, 0.9)),
                )
                for i, c in enumerate([(0.55, 0.7, 0.85), (0.85, 0.85, 0.82), (0.5, 0.7, 0.45)])
            ],
            "drum": [
                _mat(
                    f"drum{i}",
                    c,
                    0.45,
                    metal=0.15,
                    noise=(15.0, tuple(x * 0.6 for x in c)),
                    stripes=(1.2, (0.9, 0.9, 0.9)),
                )
                for i, c in enumerate([(0.08, 0.2, 0.55), (0.12, 0.12, 0.12), (0.6, 0.1, 0.08)])
            ],
            "wood": _mat(
                "wood", (0.55, 0.4, 0.25), 0.8, noise=(12.0, (0.35, 0.25, 0.15)), bump=0.2
            ),
            "box": _mat(
                "box",
                (0.62, 0.47, 0.3),
                0.75,
                noise=(25.0, (0.5, 0.36, 0.22)),
                stripes=(3.0, (0.85, 0.8, 0.7)),
            ),
            "regrind": [
                _mat(f"regrind{i}", (0.8, 0.8, 0.8), 0.5, voronoi=(55.0, cols))
                for i, cols in enumerate(
                    [
                        [(0.9, 0.9, 0.9), (0.15, 0.35, 0.7), (0.85, 0.75, 0.2), (0.2, 0.2, 0.2)],
                        [(0.7, 0.1, 0.1), (0.9, 0.9, 0.85), (0.1, 0.5, 0.3)],
                    ]
                )
            ],
            "film": [
                _mat(
                    f"film{i}",
                    c,
                    0.25,
                    noise=(8.0, (0.95, 0.95, 0.97)),
                    stripes=(1.5, (0.2, 0.3, 0.6)),
                    transmission=0.1,
                )
                for i, c in enumerate([(0.82, 0.86, 0.9), (0.7, 0.8, 0.88)])
            ],
        }

    def spec(self, kind: str) -> dict:
        r = self.rng
        if kind == "tonbag":
            n = r.choice([1, 1, 2])
            a, b, h = r.uniform(0.9, 1.1), r.uniform(0.9, 1.1), r.uniform(0.8, 1.1)
            return {
                "kind": kind,
                "n": n,
                "a": a,
                "b": b,
                "h": h,
                "w": a * n + 0.02 * (n - 1),
                "d": b,
            }
        if kind == "sacks":
            cols, rows, layers = r.choice([2, 3]), r.choice([1, 2]), r.choice([1, 2])
            return {
                "kind": kind,
                "cols": cols,
                "rows": rows,
                "layers": layers,
                "w": cols * 0.52,
                "d": rows * 0.36 + 0.04,
            }
        if kind == "drums":
            nx, ny = r.choice([(1, 1), (2, 1), (3, 1), (2, 2)])
            return {"kind": kind, "nx": nx, "ny": ny, "w": nx * 0.585, "d": ny * 0.585}
        if kind == "pallet":
            layers = r.choice([1, 2, 3])
            return {"kind": kind, "layers": layers, "w": 1.1, "d": 1.1}
        if kind == "heap":
            rad, h = r.uniform(0.5, 0.9), r.uniform(0.35, 0.75)
            return {"kind": kind, "r": rad, "h": h, "w": rad * 2.3, "d": rad * 2.3}
        if kind == "film":
            a, b, h = r.uniform(0.8, 1.2), r.uniform(0.6, 0.9), r.uniform(0.5, 0.8)
            return {"kind": kind, "a": a, "b": b, "h": h, "w": a, "d": b}
        raise ValueError(kind)

    def build(self, pid: str, spec: dict, cx: float, cy: float, yaw: float) -> list:
        """spec을 (cx, cy)에 yaw로 놓는다. 만든 물체 목록."""
        r = self.rng
        kind = spec["kind"]
        objs = []
        rot = Matrix.Rotation(yaw, 4, "Z")

        def place(lx, ly):
            v = rot @ Vector((lx, ly, 0))
            return cx + v.x, cy + v.y

        if kind == "tonbag":
            mat = r.choice(self.mats["bag"])
            for i in range(spec["n"]):
                lx = -spec["w"] / 2 + spec["a"] / 2 + i * (spec["a"] + 0.02)
                x, y = place(lx, 0)
                h = spec["h"] * r.uniform(0.95, 1.05)
                objs.append(
                    _cube(
                        f"{pid}_bag{i}",
                        (spec["a"], spec["b"], h),
                        (x, y, h / 2),
                        yaw + r.uniform(-0.05, 0.05),
                        bevel=0.09,
                        mat=mat,
                        cast=0.12,
                    )
                )
        elif kind == "sacks":
            mat = r.choice(self.mats["sack"])
            k = 0
            for layer in range(spec["layers"]):
                for i in range(spec["cols"]):
                    for j in range(spec["rows"]):
                        if layer and r.random() < 0.25:
                            continue
                        lx = -spec["w"] / 2 + 0.26 + i * 0.52 + r.uniform(-0.03, 0.03)
                        ly = -spec["d"] / 2 + 0.2 + j * 0.36 + r.uniform(-0.03, 0.03)
                        x, y = place(lx, ly)
                        o = _blob(
                            f"{pid}_sack{k}",
                            (0.52, 0.36, 0.26),
                            (x, y),
                            k + layer * 5,
                            mat,
                            flat=0.15,
                        )
                        o.location.z = 0.11 + layer * 0.2
                        o.rotation_euler = (0, 0, yaw + r.uniform(-0.1, 0.1))
                        o.modifiers["disp"].strength = 0.04
                        objs.append(o)
                        k += 1
        elif kind == "drums":
            mat = r.choice(self.mats["drum"])
            for i in range(spec["nx"]):
                for j in range(spec["ny"]):
                    x, y = place(
                        -spec["w"] / 2 + 0.2925 + i * 0.585, -spec["d"] / 2 + 0.2925 + j * 0.585
                    )
                    objs.append(_cyl(f"{pid}_drum{i}{j}", 0.29, 0.88, (x, y), mat))
        elif kind == "pallet":
            wood = self.mats["wood"]
            for i in range(5):  # 윗판
                x, y = place(-0.55 + 0.11 + i * 0.22, 0)
                objs.append(
                    _cube(f"{pid}_top{i}", (0.14, 1.1, 0.022), (x, y, 0.133), yaw, mat=wood)
                )
            for i in range(3):  # 받침
                x, y = place(-0.55 + 0.05 + i * 0.5, 0)
                objs.append(_cube(f"{pid}_blk{i}", (0.1, 1.1, 0.1), (x, y, 0.072), yaw, mat=wood))
                objs.append(_cube(f"{pid}_bot{i}", (0.1, 1.1, 0.022), (x, y, 0.011), yaw, mat=wood))
            bw, bd, bh = r.choice([(0.36, 0.36, 0.3), (0.55, 0.36, 0.3), (0.36, 0.27, 0.25)])
            nx, ny = max(1, int(1.08 // bw)), max(1, int(1.08 // bd))
            box = self.mats["box"]
            for layer in range(spec["layers"]):
                for i in range(nx):
                    for j in range(ny):
                        if layer == spec["layers"] - 1 and layer and r.random() < 0.3:
                            continue
                        x, y = place(-nx * bw / 2 + bw / 2 + i * bw, -ny * bd / 2 + bd / 2 + j * bd)
                        objs.append(
                            _cube(
                                f"{pid}_box{layer}{i}{j}",
                                (bw - 0.01, bd - 0.01, bh - 0.005),
                                (x, y, 0.144 + layer * bh + bh / 2),
                                yaw + r.uniform(-0.03, 0.03),
                                bevel=0.005,
                                mat=box,
                            )
                        )
        elif kind == "heap":
            objs.append(
                _heap(
                    f"{pid}_heap",
                    spec["r"],
                    spec["h"],
                    (cx, cy),
                    r.randrange(1000),
                    r.choice(self.mats["regrind"]),
                )
            )
        elif kind == "film":
            objs.append(
                _blob(
                    f"{pid}_film",
                    (spec["a"], spec["b"], spec["h"]),
                    (cx, cy),
                    r.randrange(100),
                    r.choice(self.mats["film"]),
                    flat=0.3,
                )
            )
            objs[-1].rotation_euler = (0, 0, yaw)
        return objs


# ── 정답 ────────────────────────────────────────────────────────────
def _envelope(objs, cx, cy, half, step=0.01):
    """pile 물체만 맞히는 위→아래 광선으로 윗면 높이를 재서 (부피, 높이 지도 점들)."""
    dg = bpy.context.evaluated_depsgraph_get()
    names = {o.name for o in objs}
    xs = np.arange(cx - half, cx + half, step) + step / 2
    ys = np.arange(cy - half, cy + half, step) + step / 2
    occupied, heights = [], []
    scene = bpy.context.scene
    for x in xs:
        for y in ys:
            origin = Vector((x, y, 4.0))
            hit, loc, _, _, obj, _ = scene.ray_cast(dg, origin, Vector((0, 0, -1)))
            # 바닥이나 벽을 먼저 맞히면 빈 칸이다(더미끼리는 겹치지 않게 놓았다)
            if hit and obj is not None and obj.name in names and loc.z > 0.005:
                occupied.append((x, y))
                heights.append(loc.z)
    vol = float(np.sum(heights) * step * step)
    return vol, np.array(occupied), np.array(heights)


def _hull_rect(pts: np.ndarray) -> dict:
    """볼록 껍질과 최소 넓이 사각형(회전 캘리퍼스)."""
    P = sorted(set(map(tuple, np.round(pts, 4))))
    if len(P) < 3:
        return {"hull": [list(p) for p in P], "rect": None}

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(P):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = np.array(lower[:-1] + upper[:-1])
    best = None
    for i in range(len(hull)):
        e = hull[(i + 1) % len(hull)] - hull[i]
        th = math.atan2(e[1], e[0])
        c, s = math.cos(th), math.sin(th)
        R = np.array([[c, s], [-s, c]])
        q = hull @ R.T
        lo, hi = q.min(axis=0), q.max(axis=0)
        area = float(np.prod(hi - lo))
        if best is None or area < best[0]:
            best = (area, th, lo, hi, R)
    _, th, lo, hi, R = best
    ctr = ((lo + hi) / 2) @ R
    size = sorted((hi - lo + 0.01).tolist(), reverse=True)
    corners = np.array([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]]) @ R
    return {
        "hull": hull.round(4).tolist(),
        "rect": {
            "center": ctr.round(4).tolist(),
            "size": [round(v, 4) for v in size],
            "corners": corners.round(4).tolist(),
        },
    }


# ── 장면 ────────────────────────────────────────────────────────────
def _room(W, D, rng, dark, tex=None):
    # 콘크리트 바닥: 얼룩(잡음 두 겹)과 3m마다 자른 줄눈
    floor_mat = _mat(
        "concrete",
        (0.46, 0.45, 0.42),
        0.85,
        noise=(0.9, (0.24, 0.24, 0.23)),
        bump=0.3,
        voronoi=None,
        brick=(1.0, (0.40, 0.40, 0.38), (0.12, 0.12, 0.12), 0.012, 3.0, 3.0),
    )
    if tex and os.path.isdir(tex):
        floor_mat = _imgmat(
            "floor_img",
            os.path.join(tex, rng.choice(FLOOR_TEX) + ".jpg"),
            rng.uniform(2.0, 3.0),
            0.8,
        )
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
    fl = bpy.context.active_object
    fl.name = "floor"
    fl.scale = (W + 0.4, D + 0.4, 1)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    _assign(fl, floor_mat)
    # 바닥 표시선(창고 구역선)
    line = _mat("line", (0.85, 0.7, 0.1), 0.6)
    for k in range(rng.randint(2, 4)):
        horiz = rng.random() < 0.5
        L = rng.uniform(1.5, 4.0)
        cx, cy = rng.uniform(-W / 3, W / 3), rng.uniform(-D / 3, D / 3)
        _cube(
            f"line{k}", (L, 0.08, 0.002) if horiz else (0.08, L, 0.002), (cx, cy, 0.001), mat=line
        )
    # 콘크리트 블록 벽(0.4m×0.2m), 아래 1.2m는 회색 띠
    wall_mat = _mat(
        "wall",
        (0.70, 0.70, 0.67),
        0.9,
        noise=(1.5, (0.5, 0.52, 0.5)),
        bump=0.15,
        brick=(1.0, (0.6, 0.6, 0.58), (0.35, 0.35, 0.34), 0.012, 0.4, 0.2),
    )
    if tex and os.path.isdir(tex):
        wall_mat = _imgmat(
            "wall_img",
            os.path.join(tex, rng.choice(WALL_TEX) + ".jpg"),
            rng.uniform(2.0, 3.0),
            0.85,
            box=True,
        )
    t, h = 0.15, 3.0
    _cube("wall_n", (W + 2 * t, t, h), (0, D / 2 + t / 2, h / 2), mat=wall_mat)
    _cube("wall_e", (t, D, h), (W / 2 + t / 2, 0, h / 2), mat=wall_mat)
    _cube("wall_w", (t, D, h), (-W / 2 - t / 2, 0, h / 2), mat=wall_mat)
    door = min(3.0, W * 0.35)
    seg = (W - door) / 2
    _cube(
        "wall_s1", (seg + t, t, h), (-W / 2 + seg / 2 - t / 2, -D / 2 - t / 2, h / 2), mat=wall_mat
    )
    _cube(
        "wall_s2", (seg + t, t, h), (W / 2 - seg / 2 + t / 2, -D / 2 - t / 2, h / 2), mat=wall_mat
    )
    # 벽에 붙은 물건(소화기 함, 안내판)으로 특징점을 둔다
    sign = _mat("sign", (0.1, 0.35, 0.65), 0.5, stripes=(5.0, (0.95, 0.95, 0.95)))
    red = _mat("ext", (0.75, 0.08, 0.06), 0.4)
    for k in range(4):
        side = rng.choice(["n", "e", "w"])
        if side == "n":
            _cube(
                f"sign{k}",
                (0.6, 0.02, 0.4),
                (rng.uniform(-W / 2.5, W / 2.5), D / 2 - 0.01, rng.uniform(1.4, 2.0)),
                mat=sign,
            )
        else:
            x = W / 2 - 0.01 if side == "e" else -W / 2 + 0.01
            _cube(
                f"sign{k}",
                (0.02, 0.6, 0.4),
                (x, rng.uniform(-D / 2.5, D / 2.5), rng.uniform(1.4, 2.0)),
                mat=red if k % 2 else sign,
            )
    stain = _mat("stain", (0.18, 0.17, 0.15), 0.6)
    for k in range(rng.randint(6, 12)):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=rng.uniform(0.1, 0.5),
            depth=0.002,
            vertices=24,
            location=(rng.uniform(-W / 2.2, W / 2.2), rng.uniform(-D / 2.2, D / 2.2), 0.001),
        )
        o = bpy.context.active_object
        o.scale = (1.0, rng.uniform(0.4, 1.0), 1.0)
        o.rotation_euler = (0, 0, rng.uniform(0, 3.14))
        _assign(o, stain)
    pipe = _mat("pipe", (0.75, 0.6, 0.1), 0.4, metal=0.3)
    box = _mat("ebox", (0.55, 0.57, 0.6), 0.5, metal=0.2, stripes=(6.0, (0.2, 0.2, 0.2)))
    for k, side in enumerate(rng.sample(["n", "e", "w"], 2)):
        zz = rng.uniform(2.1, 2.5)
        if side == "n":
            _cube(f"pipe{k}", (W, 0.1, 0.1), (0, D / 2 - 0.08, zz), mat=pipe)
            _cube(
                f"ebox{k}",
                (0.5, 0.18, 0.7),
                (rng.uniform(-W / 3, W / 3), D / 2 - 0.09, 1.5),
                mat=box,
            )
        else:
            x = W / 2 - 0.08 if side == "e" else -W / 2 + 0.08
            _cube(f"pipe{k}", (0.1, D, 0.1), (x, 0, zz), mat=pipe)
            _cube(
                f"ebox{k}",
                (0.18, 0.5, 0.7),
                (x - (0.01 if side == "e" else -0.01), rng.uniform(-D / 3, D / 3), 1.5),
                mat=box,
            )
    world = bpy.data.worlds.new("w")
    bpy.context.scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.75, 0.78, 0.82, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.05 if dark else 0.2
    lights = 1 if dark else rng.randint(2, 4)
    for k in range(lights):
        bpy.ops.object.light_add(
            type="AREA", location=(rng.uniform(-W / 4, W / 4), rng.uniform(-D / 4, D / 4), 3.6)
        )
        L = bpy.context.active_object
        L.data.size = 1.5
        L.data.energy = 60 if dark else rng.uniform(150, 260)


def _place_piles(builder, rng, W, D, mode, kinds, ring):
    """겹치지 않게(사이 0.35m 이상) 더미를 놓는다. ring = 카메라가 돌 타원의 반지름(ax, ay)."""
    placed = []
    ax, ay = ring
    for kind in kinds:
        spec = builder.spec(kind)
        for _ in range(400):
            yaw = rng.choice([0.0, math.pi / 2, rng.uniform(-0.6, 0.6)])
            half = 0.5 * math.hypot(spec["w"], spec["d"])
            if mode == "strafe":
                x = rng.uniform(-W / 2 + half + 0.1, W / 2 - half - 0.1)
                y = rng.uniform(-0.3, D / 2 - half - 0.1)
                back = y > D / 2 - half - 0.9
                side = abs(x) > W / 2 - half - 0.9 and y > 0.2
                if not (back or side):
                    continue
            elif mode == "outward":
                x = rng.uniform(-W / 2 + half + 0.1, W / 2 - half - 0.1)
                y = rng.uniform(-D / 2 + half + 0.1, D / 2 - half - 0.1)
                # 카메라 길(둥근 사각형) 바깥쪽 0.7m 이상
                if abs(x) < ax + 0.7 + half and abs(y) < ay + 0.7 + half:
                    continue
                if y < -D / 2 + 1.6 and abs(x) < 1.8:  # 출입구 앞은 비운다
                    continue
            else:
                x = rng.uniform(-ax + 0.9, ax - 0.9)
                y = rng.uniform(-ay + 0.9, ay - 0.9)
                if (x / (ax - 0.8 - half * 0.5)) ** 2 + (y / (ay - 0.8 - half * 0.5)) ** 2 > 1.0:
                    continue
            if all(math.hypot(x - p["x"], y - p["y"]) > half + p["half"] + 0.35 for p in placed):
                placed.append({"spec": spec, "x": x, "y": y, "yaw": yaw, "half": half})
                break
    return placed


def _rounded_rect(ax, ay, n=4000, p=8.0):
    """초타원 |x/ax|^p + |y/ay|^p = 1 (모서리가 둥근 사각형) 위의 점, 호 길이, 바깥 법선."""
    th = np.linspace(0, 2 * np.pi, n, endpoint=False)
    c, s = np.cos(th), np.sin(th)
    x = ax * np.sign(c) * np.abs(c) ** (2 / p)
    y = ay * np.sign(s) * np.abs(s) ** (2 / p)
    pts = np.stack([x, y], axis=1)
    seg = np.linalg.norm(np.roll(pts, -1, axis=0) - pts, axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)[:-1]])
    tang = np.roll(pts, -1, axis=0) - np.roll(pts, 1, axis=0)
    tang /= np.linalg.norm(tang, axis=1, keepdims=True)
    normal = np.stack([tang[:, 1], -tang[:, 0]], axis=1)  # 반시계로 돌 때 오른쪽 = 바깥
    return pts, arc, float(seg.sum()), tang, normal


def _camera_path(W, D, path, ring, n_video, rng, start_south=False):
    """30fps 영상 프레임마다 (위치, 바라보는 점). 손에 든 흔들림을 넣는다.

    strafe: 벽을 따라 놓인 더미 줄을 2.2~2.8m 떨어져 마주 보며 옆으로 걸어 갔다 온다(끝에서는 옆벽 쪽으로 고개를 돌린다).
        안내 문구가 시키는 찍는 법이다.
    walk: 벽을 따라 놓인 더미를 보며 둥근 사각형 길을 걷는다(바깥을 보되 걷는 쪽으로 15° 앞을 본다). 안내 문구가 시키는 찍는 법.
    inward: 가운데 더미 섬을 둘러보며 안쪽을 본다.
    spin: 방 가운데 작은 타원을 돌며 바깥을 본다(거의 제자리 회전). 안내 문구가 하지 말라는 찍는 법이고, 실패를 알아채는지 본다.
    """
    ax, ay = ring
    loops = rng.uniform(0.95, 1.1) if path != "walk" else rng.uniform(0.9, 1.0)
    th0 = -math.pi / 2 if start_south else rng.uniform(0, 2 * math.pi)
    h0 = rng.uniform(1.4, 1.7)
    shake = np.cumsum(
        np.random.default_rng(rng.randrange(10**6)).normal(0, 1, (n_video, 5)), axis=0
    )
    shake -= np.linspace(0, 1, n_video)[:, None] * shake[-1]
    shake = shake / (np.abs(shake).max(axis=0) + 1e-9)
    if path == "strafe":
        xr, y_cam = ring
    if path == "walk":
        pts, arc, total, tang, normal = _rounded_rect(ax, ay)
        s0 = arc[int(((th0 % (2 * math.pi)) / (2 * math.pi)) * len(arc)) % len(arc)]
    poses = []
    for f in range(n_video):
        t = f / max(n_video - 1, 1)
        if path == "strafe":
            # 왼쪽 끝 → 오른쪽 끝 → (0.3m 다가서서) 왼쪽 끝. 시연은 가운데에서 시작해 오른쪽 → 왼쪽 → 가운데
            if start_south:
                u = (0.25 + t) % 1.0
            else:
                u = t
            x = xr * (4 * u - 1 if u < 0.5 else 3 - 4 * u)
            y = y_cam + (0.3 if u >= 0.5 else 0.0)
            edge = max(0.0, min(1.0, (abs(x) - (xr - 1.2)) / 1.2))
            yaw = math.radians(18) * math.sin(2 * math.pi * 1.5 * t) + math.radians(35) * edge * (
                1 if x > 0 else -1
            )
            dirv = Vector((math.sin(yaw), math.cos(yaw), 0))
            th = 0.0
        elif path == "walk":
            sv = (s0 + loops * total * t) % total
            k = int(np.searchsorted(arc, sv, side="right") - 1) % len(arc)
            x, y = float(pts[k, 0]), float(pts[k, 1])
            nv = Vector((float(normal[k, 0]), float(normal[k, 1]), 0))
            tv = Vector((float(tang[k, 0]), float(tang[k, 1]), 0))
            dirv = (nv * math.cos(math.radians(15)) + tv * math.sin(math.radians(15))).normalized()
        else:
            th = th0 + 2 * math.pi * loops * t
            x, y = ax * math.cos(th), ay * math.sin(th)
            dirv = Vector((math.cos(th), math.sin(th), 0)).normalized()
        z = h0 + 0.025 * math.sin(2 * math.pi * 1.8 * f / 30.0) + 0.03 * shake[f, 0]
        x += 0.04 * shake[f, 1]
        y += 0.04 * shake[f, 2]
        if path == "strafe":
            look = Vector((x, y, 0)) + dirv * 2.6
            look.z = 0.4
        elif path in ("walk", "spin"):
            look = Vector((x, y, 0)) + dirv * 2.2
            look.z = 0.35
        else:
            look = Vector((0.15 * math.cos(th + 1.0), 0.15 * math.sin(th + 1.0), 0.3))
        yaw_j, roll_j = 0.06 * shake[f, 3], 0.035 * shake[f, 4]
        poses.append(((x, y, z), look, yaw_j, roll_j))
    return poses


def _set_camera(cam, pos, look, yaw_j, roll_j):
    cam.location = pos
    d = (look - Vector(pos)).normalized()
    rot = d.to_track_quat("-Z", "Y").to_euler()
    q = rot.to_quaternion()
    q = q @ Euler((0, 0, roll_j)).to_quaternion()
    q = Euler((0, 0, yaw_j)).to_quaternion() @ q
    cam.rotation_euler = q.to_euler()


def main() -> None:
    a = _args()
    out = a["out"]
    os.makedirs(os.path.join(out, "frames"), exist_ok=True)
    rng = random.Random(a["seed"] * 7919 + (1 if a["demo"] else 0))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    demo = a["demo"]
    if demo:
        W, D, mode, dark, portrait = 9.0, 7.0, "strafe", False, a["orient"] != "landscape"
    else:
        W, D = rng.uniform(7.0, 11.0), rng.uniform(6.0, 9.0)
        mode = "inward" if a["seed"] % 3 == 0 else ("outward" if a["seed"] % 5 == 4 else "strafe")
        dark = a["seed"] % 4 == 2
        portrait = a["seed"] % 2 == 0
    path = {"inward": "inward", "outward": "spin", "strafe": "strafe"}[mode]
    n_frames = a["frames"] or (56 if demo else rng.choice([24, 32, 48, 64, 80]))
    if path == "strafe":
        ring = (W / 2 - 1.0, max(-D / 2 + 0.6, D / 2 - 1.6 - rng.uniform(2.2, 2.8)))
    elif path == "spin":
        ring = (max(1.0, W / 2 - 2.6), max(0.8, D / 2 - 2.6))
    elif mode == "outward":
        ring = (max(0.6, W / 2 - 2.4), max(0.6, D / 2 - 2.4))
    else:
        ring = (max(2.2, W / 2 - 1.2), max(2.0, D / 2 - 1.2))
    _room(W, D, rng, dark, a["tex"])
    builder = Builder(rng)
    truth_piles = []
    if demo:
        # 한빛정밀 보관장: 뒷벽을 따라 섞여 놓였다(스프루 톤백·필름·불량 사출품 팔레트가 0.7m씩 떨어져 한 줄, 옆벽에 퍼지 드럼과 필름)
        layout = [
            (
                "sprue",
                "st_sprue",
                {"kind": "tonbag", "n": 2, "a": 1.0, "b": 1.0, "h": 1.0, "w": 2.02, "d": 1.0},
                -2.6,
                2.75,
                0.0,
            ),
            (
                "film",
                "st_film",
                {"kind": "film", "a": 1.1, "b": 0.8, "h": 0.7, "w": 1.1, "d": 0.8},
                -0.34,
                2.85,
                0.0,
            ),
            (
                "defect",
                "st_defect",
                {"kind": "pallet", "layers": 2, "w": 1.1, "d": 1.1},
                1.46,
                2.7,
                0.0,
            ),
            (
                "purge",
                "st_purge",
                {"kind": "drums", "nx": 2, "ny": 1, "w": 1.17, "d": 0.585},
                3.3,
                1.5,
                math.pi / 2,
            ),
            (
                "film2",
                "st_film",
                {"kind": "film", "a": 0.9, "b": 0.7, "h": 0.6, "w": 0.9, "d": 0.7},
                -3.6,
                0.6,
                0.4,
            ),
        ]
        placed = [
            {"spec": s, "x": x, "y": y, "yaw": yaw, "name": nm, "stream": st}
            for nm, st, s, x, y, yaw in layout
        ]
    else:
        count = rng.randint(3, 7)
        kinds = [rng.choice(KINDS) for _ in range(count)]
        placed = _place_piles(builder, rng, W, D, mode, kinds, ring)
    for i, p in enumerate(placed):
        pid = f"g{i + 1}"
        objs = builder.build(pid, p["spec"], p["x"], p["y"], p["yaw"])
        p["objs"] = objs
    bpy.context.view_layer.update()
    for i, p in enumerate(placed):
        half = 0.5 * math.hypot(p["spec"]["w"], p["spec"]["d"]) + 0.25
        vol, occ, hts = _envelope(p["objs"], p["x"], p["y"], half)
        if len(occ) < 10:
            continue
        hr = _hull_rect(occ)
        truth_piles.append(
            {
                "id": f"g{i + 1}",
                "kind": p["spec"]["kind"],
                "name": p.get("name"),
                "streamId": p.get("stream"),
                "envelopeVolumeM3": round(vol, 4),
                "heightM": round(float(np.percentile(hts, 95)), 4),
                "maxHeightM": round(float(hts.max()), 4),
                "footprintAreaM2": round(len(occ) * 1e-4, 4),
                "hull": hr["hull"],
                "rect": hr["rect"],
            }
        )
    # 카메라
    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = 26.0
    cam_data.sensor_width = 36.0
    cam_data.sensor_fit = "AUTO"
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    step = 15  # 30fps 영상에서 2fps로 고른다
    n_video = (n_frames - 1) * step + 1
    poses = _camera_path(W, D, path, ring, n_video, rng, start_south=demo)
    for f, (pos, look, yj, rj) in enumerate(poses):
        _set_camera(cam, pos, Vector(look), yj, rj)
        cam.keyframe_insert("location", frame=f)
        cam.keyframe_insert("rotation_euler", frame=f)
    # 렌더 설정
    scene.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    try:
        prefs.compute_device_type = "OPTIX"
    except TypeError:
        prefs.compute_device_type = "CUDA"
    prefs.get_devices()
    for dev in prefs.devices:
        dev.use = dev.type != "CPU"
    scene.cycles.device = "GPU"
    scene.cycles.samples = a["samples"]
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y = (504, 896) if portrait else (896, 504)
    scene.render.resolution_percentage = 100
    scene.render.fps = 30
    scene.render.use_motion_blur = True
    scene.render.motion_blur_shutter = 0.5
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 82
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = -0.4
    cams = []
    for k in range(n_frames):
        f = k * step
        scene.frame_set(f)
        scene.render.filepath = os.path.join(out, "frames", f"f{k:03d}.jpg")
        bpy.ops.render.render(write_still=True)
        cams.append([round(v, 4) for v in cam.matrix_world.translation])
    truth = {
        "scene": os.path.basename(out.rstrip("/")),
        "seed": a["seed"],
        "demo": demo,
        "room": {"W": round(W, 3), "D": round(D, 3)},
        "mode": mode,
        "path": path,
        "dark": dark,
        "portrait": portrait,
        "frames": n_frames,
        "lensMm": 26.0,
        "cameras": cams,
        "piles": truth_piles,
    }
    with open(os.path.join(out, "truth.json"), "w") as fh:
        json.dump(truth, fh, ensure_ascii=False, indent=1)
    print("SYNTH_DONE", out, n_frames, len(truth_piles))


if __name__ == "__main__":
    main()
