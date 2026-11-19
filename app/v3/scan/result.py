"""재구성 서버의 결과(RESULT)와 점군 바이트를 읽고 확인한다. 모양이 다르면 ResultError. 값을 채워 넣지 않는다.

계약: ~/rs-work/scan/CONTRACT.md 2장(저장소 밖 작업 문서). 좌표는 바닥 좌표계(m, z 위쪽).
점군: 머리 16바이트(b"RSPC", uint32 판 1, uint32 점 수 N, uint32 0) + N × 10바이트
(int16 x·y·z mm, uint8 r·g·b, uint8 더미 번호).
"""

from __future__ import annotations

import math
import struct
from typing import Any

MAGIC = b"RSPC"
HEADER = struct.Struct("<4sIII")
RECORD_BYTES = 10
MAX_POINTS = 200_000
MAX_PILES = 60
MAX_POLYGON = 400
MAX_THUMBNAIL = 120_000  # data URL 글자 수
WARNINGS = ("low-coverage", "scale-uncertain", "no-floor", "few-piles", "dark")


class ResultError(ValueError):
    """재구성 결과의 모양이 계약과 다르다."""


def _num(value: Any, what: str, *, low: float = -1e6, high: float = 1e6) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ResultError(f"{what}이(가) 수가 아닙니다.")
    if not low <= float(value) <= high:
        raise ResultError(f"{what}이(가) 범위({low}~{high}) 밖입니다.")
    return float(value)


def _point(value: Any, what: str, dims: int = 2) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != dims:
        raise ResultError(f"{what}은(는) 수 {dims}개의 목록이어야 합니다.")
    return [round(_num(v, what, low=-1000, high=1000), 4) for v in value]


def _polygon(
    value: Any, what: str, *, minimum: int = 3, maximum: int = MAX_POLYGON
) -> list[list[float]]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ResultError(f"{what}의 꼭짓점 수가 맞지 않습니다.")
    return [_point(v, what) for v in value]


def _thumbnail(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.startswith("data:image/jpeg;base64,")
        or len(value) > MAX_THUMBNAIL
    ):
        raise ResultError("thumbnail은 JPEG data URL이어야 합니다.")
    return value


def read_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ResultError("결과가 객체가 아닙니다.")
    frames = payload.get("frames")
    if not isinstance(frames, dict):
        raise ResultError("frames가 없습니다.")
    scale = payload.get("scale")
    if not isinstance(scale, dict):
        raise ResultError("scale이 없습니다.")
    floor = payload.get("floor")
    if floor is not None and not isinstance(floor, dict):
        raise ResultError("floor 형식이 잘못됐습니다.")
    piles_raw = payload.get("piles")
    if not isinstance(piles_raw, list) or len(piles_raw) > MAX_PILES:
        raise ResultError("piles 형식이 잘못됐습니다.")
    piles, seen = [], set()
    for raw in piles_raw:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise ResultError("더미에 id가 없습니다.")
        pid = raw["id"][:20]
        if pid in seen:
            raise ResultError("더미 id가 겹칩니다.")
        seen.add(pid)
        volume = _num(raw.get("volumeM3"), "volumeM3", low=0, high=500)
        rng = raw.get("volumeRangeM3")
        if not isinstance(rng, list) or len(rng) != 2:
            raise ResultError("volumeRangeM3는 [하한, 상한]이어야 합니다.")
        lo, hi = (_num(v, "volumeRangeM3", low=0, high=1000) for v in rng)
        if not lo <= volume <= hi:
            raise ResultError("volumeM3가 volumeRangeM3 밖에 있습니다.")
        size = raw.get("sizeM")
        if not isinstance(size, list) or len(size) != 2:
            raise ResultError("sizeM은 [긴 변, 짧은 변]이어야 합니다.")
        length, width = (_num(v, "sizeM", low=0.01, high=100) for v in size)
        piles.append(
            {
                "id": pid,
                "footprint": _polygon(raw.get("footprint"), "footprint", minimum=4, maximum=4),
                "center": _point(raw.get("center"), "center"),
                "sizeM": [round(max(length, width), 3), round(min(length, width), 3)],
                "yawDeg": round(_num(raw.get("yawDeg", 0.0), "yawDeg", low=-360, high=360), 2),
                "heightM": round(_num(raw.get("heightM"), "heightM", low=0, high=20), 3),
                "maxHeightM": round(
                    _num(raw.get("maxHeightM", raw.get("heightM")), "maxHeightM", low=0, high=20), 3
                ),
                "volumeM3": round(volume, 4),
                "volumeRangeM3": [round(lo, 4), round(hi, 4)],
                "coverage": round(_num(raw.get("coverage"), "coverage", low=0, high=1), 3),
                "points": int(_num(raw.get("points", 0), "points", low=0, high=1e8)),
                "thumbnail": _thumbnail(raw.get("thumbnail")),
            }
        )
    camera = payload.get("camera")
    path, start = [], None
    if isinstance(camera, dict):
        raw_path = camera.get("path") or []
        if not isinstance(raw_path, list):
            raise ResultError("camera.path 형식이 잘못됐습니다.")
        path = [_point(p, "camera.path", 3) for p in raw_path[:200]]
        if camera.get("start") is not None:
            start = _point(camera.get("start"), "camera.start")
    bounds = payload.get("bounds")
    if bounds is not None:
        if not isinstance(bounds, dict):
            raise ResultError("bounds 형식이 잘못됐습니다.")
        bounds = {
            "min": _point(bounds.get("min"), "bounds", 3),
            "max": _point(bounds.get("max"), "bounds", 3),
        }
    warnings = [w for w in (payload.get("warnings") or []) if w in WARNINGS]
    models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
    timings = payload.get("timingsSec") if isinstance(payload.get("timingsSec"), dict) else {}
    return {
        "frames": {
            "received": int(_num(frames.get("received", 0), "frames.received", low=0, high=1000)),
            "used": int(_num(frames.get("used", 0), "frames.used", low=0, high=1000)),
        },
        "scale": {
            "metersPerUnit": _num(scale.get("metersPerUnit", 1.0), "scale", low=0, high=1e6),
            "spreadPct": round(_num(scale.get("spreadPct", 0.0), "spreadPct", low=0, high=1000), 2),
            "method": str(scale.get("method") or "")[:40],
        },
        "floor": None
        if floor is None
        else {
            "areaM2": round(_num(floor.get("areaM2"), "floor.areaM2", low=0, high=1e5), 2),
            "polygon": _polygon(floor.get("polygon"), "floor.polygon"),
            "planeRmsM": round(_num(floor.get("planeRmsM", 0.0), "planeRmsM", low=0, high=10), 4),
        },
        "camera": {"path": path, "start": start},
        "piles": piles,
        "bounds": bounds,
        "pointsCount": int(
            _num(payload.get("pointsCount", 0), "pointsCount", low=0, high=MAX_POINTS)
        ),
        "warnings": warnings,
        # 운영 화면과 근거 meta에만 쓴다(사용자 화면에 모델 이름을 쓰지 않는다)
        "models": {str(k)[:20]: str(v)[:60] for k, v in models.items()},
        "timingsSec": {
            str(k)[:20]: round(float(v), 2)
            for k, v in timings.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        },
    }


def read_points(data: bytes, piles: int | None = None) -> int:
    """점군 바이트를 확인하고 점 수를 준다. 더미 번호가 piles보다 크면 ResultError."""
    if len(data) < HEADER.size:
        raise ResultError("점군이 너무 짧습니다.")
    magic, version, count, _ = HEADER.unpack_from(data)
    if magic != MAGIC or version != 1:
        raise ResultError("점군 머리가 맞지 않습니다.")
    if count > MAX_POINTS or len(data) != HEADER.size + count * RECORD_BYTES:
        raise ResultError("점군 길이가 맞지 않습니다.")
    if piles is not None and count:
        labels = data[HEADER.size + 9 :: RECORD_BYTES]
        if max(labels) > piles:
            raise ResultError("점군의 더미 번호가 더미 수보다 큽니다.")
    return count
