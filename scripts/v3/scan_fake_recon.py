"""보관장 3D 스캔의 가짜 재구성 서버(로컬 점검·시험용). H100 재구성 서버(infra/h100/recon)와 같은 계약으로 답한다.

    python -m scripts.v3.scan_fake_recon --port 8093                     # 손으로 만든 장면(더미 5개)
    python -m scripts.v3.scan_fake_recon --port 8093 \\
        --result app/v3/demo/scan/hanbit-result.json --points app/v3/demo/scan/hanbit-points.bin

    백엔드 devserver는 V3_RECON_URL=http://127.0.0.1:8093 으로 띄운다.

작업은 GET을 받을 때마다 한 단계씩(geometry → scale → floor → piles → done) 나아간다. 사진은 쓰지 않는다.
sample_result()와 sample_points()는 시험(tests/v3/test_scan.py)도 쓴다.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STAGES = ("geometry", "scale", "floor", "piles", "done")

# (가운데 x, y, 긴 변, 짧은 변, 방향 도, 높이 m, 색)
SAMPLE_PILES = (
    (-2.4, 1.2, 1.0, 1.0, 0.0, 1.05, (226, 222, 210)),  # 톤백
    (-1.1, 1.3, 1.0, 1.0, 8.0, 0.95, (226, 222, 210)),  # 톤백
    (0.6, 1.0, 1.4, 0.9, 0.0, 0.8, (150, 120, 80)),  # 팔레트 위 상자
    (1.2, -0.6, 1.2, 0.6, 90.0, 0.88, (40, 80, 160)),  # 드럼 두 개
    (-0.4, -0.2, 1.5, 1.1, 25.0, 0.6, (235, 235, 240)),  # 필름 묶음
)
FLOOR = [[-4.0, -3.0], [4.0, -3.0], [4.0, 3.0], [-4.0, 3.0]]


def _rect(cx: float, cy: float, length: float, width: float, yaw: float) -> list[list[float]]:
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    out = []
    for x, y in ((-length / 2, -width / 2), (length / 2, -width / 2), (length / 2, width / 2), (-length / 2, width / 2)):  # fmt: skip
        out.append([round(cx + x * c - y * s, 4), round(cy + x * s + y * c, 4)])
    return out


def sample_result(piles: tuple = SAMPLE_PILES) -> dict[str, Any]:
    out = []
    for index, (cx, cy, length, width, yaw, height, _) in enumerate(piles, start=1):
        volume = length * width * height * 0.92  # 위가 둥근 더미
        out.append(
            {
                "id": f"p{index}",
                "footprint": _rect(cx, cy, length, width, yaw),
                "center": [cx, cy],
                "sizeM": [length, width],
                "yawDeg": yaw,
                "heightM": height,
                "maxHeightM": round(height * 1.08, 3),
                "volumeM3": round(volume, 4),
                "volumeRangeM3": [round(volume * 0.9, 4), round(volume * 1.11, 4)],
                "coverage": 0.88 if index != 3 else 0.52,
                "points": 12000,
                "thumbnail": None,
            }
        )
    return {
        "frames": {"received": 48, "used": 48, "width": 518, "height": 294},
        "scale": {"metersPerUnit": 1.0, "spreadPct": 3.4, "method": "metric-depth"},
        "floor": {"areaM2": 48.0, "polygon": FLOOR, "planeRmsM": 0.011},
        "camera": {
            "path": [
                [round(3.5 * math.cos(t / 8), 3), round(-2.6 + 0.2 * math.sin(t / 8), 3), 1.5]
                for t in range(16)
            ],
            "start": [0.2, -2.8],
        },
        "piles": out,
        "bounds": {"min": [-4.0, -3.0, 0.0], "max": [4.0, 3.0, 1.2]},
        "pointsCount": 0,  # sample_points가 채운다
        "timingsSec": {"geometry": 1.0, "scale": 1.0, "floor": 0.1, "piles": 0.1, "total": 2.2},
        "models": {"geometry": "fake", "scale": "fake"},
        "warnings": ["low-coverage"],
    }


def sample_points(piles: tuple = SAMPLE_PILES, step: float = 0.08) -> bytes:
    """바닥 격자와 더미 윗면·옆면 점(RSPC 형식). 같은 입력이면 같은 바이트다."""
    records = []

    def add(x: float, y: float, z: float, rgb: tuple[int, int, int], label: int) -> None:
        records.append(
            struct.pack("<hhhBBBB", round(x * 1000), round(y * 1000), round(z * 1000), *rgb, label)
        )

    n = int(8 / step)
    for i in range(n):
        for j in range(int(6 / step)):
            x, y = -4 + i * step, -3 + j * step
            shade = 120 + (i * 7 + j * 13) % 20
            add(x, y, 0.0, (shade, shade, shade - 6), 0)
    for index, (cx, cy, length, width, yaw, height, rgb) in enumerate(piles, start=1):
        c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        k = max(2, int(length / 0.04))
        m = max(2, int(width / 0.04))
        for a in range(k + 1):
            for b in range(m + 1):
                u, v = -length / 2 + length * a / k, -width / 2 + width * b / m
                edge = a in (0, k) or b in (0, m)
                bump = 1 - 0.15 * ((2 * u / length) ** 2 + (2 * v / width) ** 2)
                x, y = cx + u * c - v * s, cy + u * s + v * c
                add(x, y, height * bump, rgb, index)
                if edge:
                    for h in range(1, 10):
                        add(x, y, height * bump * h / 10, tuple(max(0, q - 30) for q in rgb), index)
    return struct.pack("<4sIII", b"RSPC", 1, len(records), 0) + b"".join(records)


class _State:
    def __init__(self, result: dict[str, Any], points: bytes, fail: bool) -> None:
        self.result, self.points, self.fail = result, points, fail
        self.jobs: dict[str, int] = {}


def make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # 조용히
            return

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if parts == ["health"]:
                return self._json(200, {"ok": True, "version": "fake", "models": {}, "busy": False})
            if len(parts) >= 2 and parts[0] == "jobs" and parts[1] in state.jobs:
                job = parts[1]
                if len(parts) == 3 and parts[2] == "points":
                    body = state.points
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                step = state.jobs[job]
                state.jobs[job] = min(step + 1, len(STAGES) - 1)
                stage = STAGES[step]
                if stage == "done" and state.fail:
                    return self._json(200, {"jobId": job, "status": "failed", "stage": None, "progress": 1,
                                            "error": {"code": "NO_FLOOR", "message": "no floor"}, "result": None})  # fmt: skip
                return self._json(
                    200,
                    {
                        "jobId": job,
                        "status": "done" if stage == "done" else "running",
                        "stage": stage,
                        "progress": round(step / (len(STAGES) - 1), 2),
                        "error": None,
                        "result": state.result if stage == "done" else None,
                    },
                )
            return self._json(404, {"error": {"code": "NOT_FOUND", "message": "없음"}})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if self.path.rstrip("/") != "/jobs":
                return self._json(404, {"error": {"code": "NOT_FOUND", "message": "없음"}})
            frames = body.count(b'name="frames"')
            if frames < 12:
                return self._json(422, {"error": {"code": "TOO_FEW_FRAMES", "message": "too few"}})
            job = f"job{len(state.jobs) + 1}"
            state.jobs[job] = 0
            return self._json(202, {"jobId": job, "status": "queued"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="보관장 3D 스캔 가짜 재구성 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument("--result", default=None, help="RESULT JSON 파일(없으면 손으로 만든 장면)")
    parser.add_argument("--points", default=None, help="점군 파일(RSPC)")
    parser.add_argument("--fail", action="store_true", help="끝에서 NO_FLOOR로 실패한다")
    args = parser.parse_args()
    if args.result:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
        points = Path(args.points).read_bytes() if args.points else sample_points()
    else:
        result, points = sample_result(), sample_points()
    result["pointsCount"] = struct.unpack_from("<4sIII", points)[2]
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(_State(result, points, args.fail))
    )
    print(
        f"가짜 재구성 서버 http://{args.host}:{args.port} (더미 {len(result['piles'])}개)",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
