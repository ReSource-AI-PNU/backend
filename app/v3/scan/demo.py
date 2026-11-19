"""체험하기의 시연 스캔: 합성 보관장 장면을 H100 재구성 서버로 돌린 결과(app/v3/demo/scan/)를 시연 배출 기업에 넣는다.

- 세 파일(hanbit-result.json, hanbit-points.bin, hanbit-truth.json)이 모두 있을 때만 넣는다. 없으면 아무것도 하지 않는다.
- 결과는 실제 재구성 결과를 그대로 쓰고(scan/result.py로 확인), 더미별 폐기물 지정만 정답 파일(합성 장면의 정답과
  짝지은 결과)에서 가져온다. 모든 숫자는 실제 사용자 경로와 같은 함수(views.complete, views.derive)로 근거와 함께 만든다.
- 스캔 ID는 고정이다(되돌리기를 여러 번 해도 근거 ID가 같다).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.v3.scan import views
from app.v3.scan.result import read_points, read_result

ASSETS = Path(__file__).resolve().parents[1] / "demo" / "scan"
SCAN_ID = "scan_demo_hanbit"


def seed_scan(store: Any, registry: Any, today: str, org_id: str) -> str | None:
    """시연 스캔을 넣고 그 ID를 준다. 자산이 없으면 None."""
    paths = [
        ASSETS / name for name in ("hanbit-result.json", "hanbit-points.bin", "hanbit-truth.json")
    ]
    if not all(path.exists() for path in paths):
        return None
    result = read_result(json.loads(paths[0].read_text(encoding="utf-8")))
    points = paths[1].read_bytes()
    read_points(points, len(result["piles"]))
    truth = json.loads(paths[2].read_text(encoding="utf-8"))
    streams = {s["id"] for s in store.list_streams(org_id)}
    piles = {p["id"] for p in result["piles"]}
    assign = {
        pid: sid
        for pid, sid in (truth.get("assign") or {}).items()
        if pid in piles and sid in streams
    }
    weighed = {
        pid: float(kg)
        for pid, kg in (truth.get("weighed") or {}).items()
        if pid in assign and isinstance(kg, (int, float)) and not isinstance(kg, bool) and kg > 0
    }
    app = SimpleNamespace(store=store, registry=registry, today=today, read_only=False)
    scan = {
        "id": SCAN_ID,
        "orgId": org_id,
        "createdAt": store.now(),
        "source": "video",
        "frameCount": result["frames"]["received"] or result["frames"]["used"],
        "durationSec": None,
        "status": "processing",
        "jobId": None,
        "progress": None,
        "failure": None,
        "result": None,
        "assign": assign,
        "weighed": {},
        "pickup": None,
        "derived": None,
        "synthetic": True,
    }
    done = views.complete(app, scan, result, points)  # type: ignore[arg-type]
    if weighed and done.get("status") == "done":
        for pid, kg in sorted(weighed.items()):
            done = _weigh(app, done, pid, kg)
    return SCAN_ID if done.get("status") == "done" else None


def _weigh(app: Any, scan: dict[str, Any], pile_id: str, kg: float) -> dict[str, Any]:
    """시연 계량값(정답 파일의 weighed)을 사용자 경로와 같은 근거로 넣는다."""
    from app.v3.evidence.model import Evidence

    index = [p["id"] for p in scan["result"]["piles"]].index(pile_id) + 1
    app.registry.put(
        Evidence(
            id=views._weighed_id(scan, pile_id),
            kind="human",
            label=f"더미 {index} 계량 무게",
            value=kg,
            unit="kg",
            excerpt=f"담당자가 적은 더미 {index}의 계량 무게 {kg:g} kg (시연 값)",
            source=f"보관장 3D 스캔 {scan['id']} · 담당자 입력",
            as_of=str(app.today),
            access=(scan["orgId"],),
        )
    )
    scan["weighed"] = {**scan.get("weighed", {}), pile_id: kg}
    scan["derived"] = views.derive(app, scan)
    return app.store.put_scan(scan)
