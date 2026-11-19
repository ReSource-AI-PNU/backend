"""보관장 3D 스캔 서비스: 올리기, 진행 갱신, 더미별 폐기물 지정과 계량값, 상차 지점, 화면용 ScanView.

- 사진(브라우저가 영상에서 고른 장면)을 재구성 서버(scan/client.py)에 보내고, 끝나면 결과와 점군을 저장한다.
- 숫자는 모두 근거에 연결한다. 부피는 model 근거, 회사가 적은 겉보기 밀도와 계량 무게는 human 근거,
  필름의 문헌 밀도는 ref 근거(EPA 2016), 무게·합계는 calc 근거, 정리안의 간격은 assumption 근거다.
- 무게 기준이 없으면 무게를 산정 보류한다(0으로 두지 않는다).
- 계산한 값(derived)은 쓰는 경로(끝남, 지정, 계량, 상차 지점)에서 근거와 함께 저장한다. 조회는 쓰지 않고
  저장된 값을 보인다. 체험하기(읽기 전용)도 같은 값을 그대로 본다. 실제 조직은 폐기물 정보(밀도 등)가
  바뀌었으면 조회 때 다시 계산한다.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import PUBLIC, Evidence
from app.v3.scan import layout
from app.v3.scan.client import ReconError
from app.v3.scan.result import ResultError, read_points, read_result
from app.v3.service import App, ServiceError, _owner
from app.v3.store import NotFound

logger = logging.getLogger(__name__)

MODEL_NAME = "resource-ai-scan3d"
MODEL_VERSION = "v1"
MIN_FRAMES, MAX_FRAMES = 12, 96
MAX_FRAME_BYTES = 3 * 1024 * 1024
STALE_MINUTES = 20  # 이만큼 끝나지 않으면 멈춘 것으로 본다
LIST_LIMIT = 5
STAGES = ("geometry", "scale", "floor", "piles")
LOW_COVERAGE = 0.6

# EPA, Volume-to-Weight Conversion Factors (2016-04) · Plastic · Film · LDPE.
# 35 lb/yd³(loose), 150 lb/yd³(compacted). 1 lb/yd³ = 0.45359237 kg / 0.764554858 m³
LB_PER_YD3 = 0.45359237 / 0.764554858
EPA_SOURCE = (
    "US EPA, Volume-to-Weight Conversion Factors (2016-04), Plastic · Film · LDPE "
    "https://www.epa.gov/sites/production/files/2016-04/documents/"
    "volume_to_weight_conversion_factors_memorandum_04192016_508fnl.pdf"
)
EPA_FILM = (
    ("흩어짐", 35.0, "LDPE 필름, 흩어진 상태(loose) 35 lb/cubic yard"),
    ("압축", 150.0, "LDPE 필름, 압축한 상태(compacted) 150 lb/cubic yard"),
)
RULE_TEXT = {
    "gapM": ("보관장정리-폐기물사이간격", "다른 폐기물 더미 사이를 {v:g} m 띄운다", "섞임을 줄이려는 서비스의 배치 기준(법령 기준이 아님)"),
    "wallM": ("보관장정리-벽여유", "벽에서 {v:g} m 안쪽에 둔다", "더미를 옮기고 덮을 여유를 두려는 서비스의 배치 기준"),
    "pickupClearM": ("보관장정리-상차지점여유", "상차 지점 반경 {v:g} m는 비운다", "차량을 대고 싣는 자리를 두려는 서비스의 배치 기준"),
}  # fmt: skip
FAILURES = {
    "NO_FLOOR": "바닥이 충분히 보이지 않아 더미를 나누지 못했습니다. 바닥이 함께 보이게 조금 떨어져서 다시 찍어 주세요.",
    "NO_PILES": "쌓인 더미를 찾지 못했습니다. 더미가 화면 가운데 오게 둘레를 천천히 돌며 다시 찍어 주세요.",
    "TOO_FEW_FRAMES": "장면이 너무 적습니다. 20초 이상 천천히 걸으며 다시 찍어 주세요.",
    "BAD_IMAGE": "사진을 읽지 못했습니다. 다른 영상으로 다시 올려 주세요.",
    "STALE": "분석을 끝내지 못했습니다. 다시 올려 주세요.",
}
DEFAULT_FAILURE = (
    "이 영상으로는 3D 재구성을 하지 못했습니다. 더미 둘레를 천천히 한 바퀴 돌며 다시 찍어 주세요."
)
NOTES = {
    "low-coverage": "일부 더미가 한쪽에서만 찍혀 부피 범위가 넓습니다. 더미 둘레를 한 바퀴 돌며 찍으면 좁아집니다.",
    "dark": "어두운 장면이 많아 정확도가 낮을 수 있습니다. 조명을 켜고 찍으면 나아집니다.",
}
UNAVAILABLE = ServiceError(
    "SCAN_UNAVAILABLE", "지금은 3D 분석을 할 수 없습니다. 잠시 뒤 다시 올려 주세요.", 503
)


# ── 공통 ─────────────────────────────────────────────────────────────
def _scan(app: App, org_id: str, scan_id: str) -> dict[str, Any]:
    try:
        scan = app.store.get_scan(str(scan_id or ""))
    except NotFound:
        raise ServiceError("SCAN_NOT_FOUND", "그런 스캔이 없습니다.", 404) from None
    if scan.get("orgId") != org_id:
        raise ServiceError("SCAN_NOT_FOUND", "그런 스캔이 없습니다.", 404)
    return scan


def _image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"


def _minutes_since(app: App, stamp: str | None) -> float:
    try:
        then = datetime.fromisoformat(str(stamp))
        now = datetime.fromisoformat(app.store.now())
    except (TypeError, ValueError):
        return 0.0
    return (now - then).total_seconds() / 60.0


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:,.0f}"


def _put(app: App, evidence: Evidence) -> str:
    return app.registry.put(evidence).id


# ── 올리기 ───────────────────────────────────────────────────────────
def create_scan(
    app: App,
    org_id: str,
    frames: list[tuple[str, bytes]],
    source: str,
    duration_sec: float | None,
    viewer: str,
) -> dict[str, Any]:
    """장면 사진들을 재구성 서버에 맡기고 진행 중인 스캔을 만든다."""
    _owner(app, org_id, viewer, "emitter")
    if not frames or any(not _image(data) for _, data in frames):
        raise ServiceError("UNSUPPORTED_MEDIA", "사진(JPEG·PNG)만 올릴 수 있습니다.", 415)
    if len(frames) < MIN_FRAMES:
        raise ServiceError("TOO_FEW_FRAMES", FAILURES["TOO_FEW_FRAMES"], 422)
    if len(frames) > MAX_FRAMES or any(len(data) > MAX_FRAME_BYTES for _, data in frames):
        raise ServiceError(
            "TOO_LARGE", "장면이 너무 많거나 큽니다. 영상을 1분 안쪽으로 줄여 주세요.", 413
        )
    client = getattr(app, "recon", None)
    if client is None or not client.configured:
        raise UNAVAILABLE
    # GPU 하나를 여러 회사가 함께 쓴다: 한 회사는 한 번에 하나만 맡긴다(끝났거나 멈춘 것은 세지 않는다)
    for other in app.store.list_scans(org_id):
        if (
            other.get("status") == "processing"
            and refresh(app, other).get("status") == "processing"
        ):
            raise ServiceError(
                "SCAN_IN_PROGRESS", "이미 분석 중인 스캔이 있습니다. 끝난 뒤 다시 올려 주세요.", 409
            )
    named = [
        (f"f{i:03d}." + ("png" if data[:4] == b"\x89PNG" else "jpg"), data)
        for i, (_, data) in enumerate(frames)
    ]
    try:
        job_id = client.submit(named)
    except ReconError as error:
        if error.status == 422:
            code = error.code if error.code in FAILURES else "BAD_IMAGE"
            raise ServiceError(code, FAILURES[code], 422) from error
        logger.warning("재구성 서버에 맡기지 못했다: %s %s", error.code, error)
        raise UNAVAILABLE from error
    scan = {
        "id": app.store.next_id("scan"),
        "orgId": org_id,
        "createdAt": app.store.now(),
        "source": "photos" if source == "photos" else "video",
        "frameCount": len(frames),
        "durationSec": round(float(duration_sec), 1)
        if isinstance(duration_sec, (int, float))
        and not isinstance(duration_sec, bool)
        and duration_sec > 0
        else None,
        "status": "processing",
        "jobId": job_id,
        "progress": {"stage": "geometry", "ratio": 0.0},
        "failure": None,
        "result": None,
        "assign": {},
        "weighed": {},
        "pickup": None,
        "derived": None,
    }
    app.store.put_scan(scan)
    return view(app, scan)


# ── 진행 갱신 ────────────────────────────────────────────────────────
def _fail(app: App, scan: dict[str, Any], code: str) -> dict[str, Any]:
    scan.update(
        status="failed",
        progress=None,
        failure={"code": code, "message": FAILURES.get(code, DEFAULT_FAILURE)},
    )
    return app.store.put_scan(scan)


def refresh(app: App, scan: dict[str, Any]) -> dict[str, Any]:
    """진행 중이면 재구성 서버에 묻고, 끝났으면 결과·점군·근거를 저장한다. 읽기 전용 앱은 그대로 둔다."""
    if scan.get("status") != "processing" or app.read_only:
        return scan
    client = getattr(app, "recon", None)
    stale = _minutes_since(app, scan.get("createdAt")) > STALE_MINUTES
    if client is None or not client.configured:
        return _fail(app, scan, "STALE") if stale else scan
    try:
        job = client.job(str(scan.get("jobId")))
    except ReconError as error:
        if error.status == 404:  # 서버가 다시 켜져 작업을 잃었다
            return _fail(app, scan, "STALE")
        return _fail(app, scan, "STALE") if stale else scan
    status = job.get("status")
    if status == "failed":
        error = job.get("error") if isinstance(job.get("error"), dict) else {}
        logger.info("재구성 실패 %s: %s", scan["id"], error)
        return _fail(app, scan, str(error.get("code") or "RECON_FAILED"))
    if status != "done":
        stage = job.get("stage") if job.get("stage") in STAGES else "geometry"
        ratio = job.get("progress")
        ratio = (
            float(ratio) if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else 0.0
        )
        scan["progress"] = {"stage": stage, "ratio": round(max(0.0, min(1.0, ratio)), 2)}
        if stale:
            return _fail(app, scan, "STALE")
        return app.store.put_scan(scan)
    try:
        result = read_result(job.get("result"))
        points = client.points(str(scan.get("jobId")))
        read_points(points, len(result["piles"]))
    except (ResultError, ReconError) as error:
        logger.warning("재구성 결과를 받지 못했다 %s: %s", scan["id"], error)
        return _fail(app, scan, "RECON_FAILED")
    return complete(app, scan, result, points)


def complete(
    app: App, scan: dict[str, Any], result: dict[str, Any], points: bytes
) -> dict[str, Any]:
    """확인한 결과와 점군을 저장하고 더미별 부피 근거를 등록한다(시연 시드도 이 경로를 쓴다)."""
    if result["floor"] is None:
        scan["result"] = result
        return _fail(app, scan, "NO_FLOOR")
    if not result["piles"]:
        scan["result"] = result
        return _fail(app, scan, "NO_PILES")
    app.store.put_scan_points(
        {"id": scan["id"], "orgId": scan["orgId"], "data": base64.b64encode(points).decode("ascii")}
    )
    scan.update(
        status="done", progress=None, failure=None, result=result, completedAt=app.store.now()
    )
    _register_volumes(app, scan)
    scan["derived"] = derive(app, scan)
    return app.store.put_scan(scan)


def _volume_id(scan: dict[str, Any], pile_id: str) -> str:
    return ids.model_id(MODEL_NAME, MODEL_VERSION, f"{scan['id']}-{pile_id}-부피")


def _register_volumes(app: App, scan: dict[str, Any]) -> None:
    result = scan["result"]
    day = str(scan.get("completedAt") or scan["createdAt"])[:10]
    for index, pile in enumerate(result["piles"], start=1):
        lo, hi = pile["volumeRangeM3"]
        _put(
            app,
            Evidence(
                id=_volume_id(scan, pile["id"]),
                kind="model",
                label=f"더미 {index} 부피(3D 스캔)",
                value=pile["volumeM3"],
                unit="m³",
                excerpt=(
                    f"보관장 영상의 장면 {result['frames']['used']}장으로 3D 재구성해 잰 더미 {index}의 부피 "
                    f"{_fmt(pile['volumeM3'])} m³ (범위 {_fmt(lo)}~{_fmt(hi)} m³, 바닥 자국의 "
                    f"{round(pile['coverage'] * 100)}%가 보임, 실제 크기 추정 퍼짐 ±{_fmt(result['scale']['spreadPct'], 1)}%)"
                ),
                source=f"보관장 3D 스캔 {scan['id']} · {day}",
                as_of=day,
                access=(scan["orgId"],),
                meta={
                    "scanId": scan["id"],
                    "pileId": pile["id"],
                    "volumeRangeM3": [lo, hi],
                    "coverage": pile["coverage"],
                    "heightM": pile["heightM"],
                    "sizeM": pile["sizeM"],
                    "scale": result["scale"],
                    "models": result["models"],
                },
            ),
        )


# ── 무게 기준과 정리안 (쓰는 경로에서 계산해 저장한다) ─────────────────
def _inputs(app: App, scan: dict[str, Any]) -> dict[str, Any]:
    """derived를 다시 계산해야 하는지 가르는 입력. 지정된 폐기물의 이름·형태·밀도가 바뀌면 달라진다."""
    streams = {s["id"]: s for s in app.store.list_streams(scan["orgId"])}
    used = sorted({sid for sid in scan.get("assign", {}).values() if sid})
    return {
        "assign": scan.get("assign", {}),
        "weighed": scan.get("weighed", {}),
        "pickup": scan.get("pickup"),
        "streams": {
            sid: [
                streams[sid].get("name"),
                streams[sid].get("form"),
                streams[sid].get("bulkDensityKgPerM3"),
            ]
            if sid in streams
            else None
            for sid in used
        },
    }


def _density(
    app: App,
    scan: dict[str, Any],
    stream: dict[str, Any],
    piles: dict[str, dict[str, Any]],
    day: str,
) -> dict[str, Any] | None:
    """폐기물 하나의 무게 기준: 계량 > 회사가 적은 겉보기 밀도 > 필름 문헌값 > 없음."""
    org = scan["orgId"]
    sid = stream["id"]
    weighed = [
        (pid, float(kg))
        for pid, kg in sorted(scan.get("weighed", {}).items())
        if scan.get("assign", {}).get(pid) == sid and pid in piles
    ]
    if weighed:
        kg_sum = sum(kg for _, kg in weighed)
        vol_sum = sum(piles[pid]["volumeM3"] for pid, _ in weighed)
        if vol_sum > 0:
            density = kg_sum / vol_sum
            inputs = {"weighed": weighed, "volumes": [piles[p]["volumeM3"] for p, _ in weighed]}
            sources = [_weighed_id(scan, pid) for pid, _ in weighed] + [
                _volume_id(scan, pid) for pid, _ in weighed
            ]
            cid = _put(
                app,
                Evidence(
                    id=ids.calc_id("scan_density", {"scan": scan["id"], **inputs}, "kgPerM3"),
                    kind="calc",
                    label=f"{stream.get('name') or sid} 겉보기 밀도(계량 보정)",
                    value=round(density, 1),
                    unit="kg/m³",
                    excerpt=(
                        f"계량한 무게 {_fmt(kg_sum, 0)} kg ÷ 같은 더미의 3D 스캔 부피 {_fmt(vol_sum)} m³ "
                        f"= {_fmt(density, 1)} kg/m³"
                    ),
                    source=f"보관장 3D 스캔 {scan['id']} 계량 보정",
                    as_of=day,
                    access=(org,),
                    meta={"inputs": sources},
                ),
            )
            return {
                "basis": "weighed",
                "lowKgM3": round(density, 1),
                "highKgM3": round(density, 1),
                "label": "계량한 무게로 맞춘 밀도",
                "evidence": [cid],
            }
    declared = stream.get("bulkDensityKgPerM3")
    if isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared > 0:
        hid = _put(
            app,
            Evidence(
                id=ids.human_id(org, f"{sid}-겉보기밀도"),
                kind="human",
                label=f"{stream.get('name') or sid} 겉보기 밀도(회사 입력)",
                value=float(declared),
                unit="kg/m³",
                excerpt=f"회사가 폐기물 정보에 적은 겉보기 밀도 {_fmt(float(declared), 1)} kg/m³",
                source="폐기물 정보(회사 입력)",
                as_of=day,
                access=(org,),
            ),
        )
        return {
            "basis": "stream",
            "lowKgM3": float(declared),
            "highKgM3": float(declared),
            "label": "회사가 적은 겉보기 밀도",
            "evidence": [hid],
        }
    if "필름" in str(stream.get("form") or "") or "필름" in str(stream.get("name") or ""):
        refs, values = [], []
        for key, lb, text in EPA_FILM:
            value = round(lb * LB_PER_YD3, 1)
            values.append(value)
            refs.append(
                _put(
                    app,
                    Evidence(
                        id=ids.ref_id("EPA부피무게환산", f"LDPE필름-{key}", "2016-04"),
                        kind="ref",
                        label=f"필름 겉보기 밀도({key})",
                        value=value,
                        unit="kg/m³",
                        excerpt=f"{text} = {_fmt(value, 1)} kg/m³",
                        source=EPA_SOURCE,
                        as_of="2016-04-19",
                        access=(PUBLIC,),
                    ),
                )
            )
        return {
            "basis": "ref",
            "lowKgM3": min(values),
            "highKgM3": max(values),
            "label": "필름 문헌값(흩어짐~압축)",
            "evidence": refs,
        }
    return None


def _weighed_id(scan: dict[str, Any], pile_id: str) -> str:
    return ids.human_id(scan["orgId"], f"{scan['id']}-{pile_id}-계량")


def _claim(cid: str, value: float, unit: str, evidence: list[str]) -> dict[str, Any]:
    return {"id": cid, "kind": "number", "value": value, "unit": unit, "evidence": evidence}


def derive(app: App, scan: dict[str, Any]) -> dict[str, Any]:
    """더미별 무게 기준·무게, 합계, 정리안과 그 근거를 계산해 등록한다(쓰는 경로에서만 부른다)."""
    result = scan["result"]
    org = scan["orgId"]
    day = str(app.today)
    streams = {s["id"]: s for s in app.store.list_streams(org)}
    piles = {p["id"]: p for p in result["piles"]}
    assign = {
        pid: sid for pid, sid in scan.get("assign", {}).items() if sid in streams and pid in piles
    }
    densities = {
        sid: _density(app, scan, streams[sid], piles, day) for sid in sorted(set(assign.values()))
    }
    out_piles: dict[str, Any] = {}
    weights: list[tuple[str, float, float, list[str]]] = []
    for index, pile in enumerate(result["piles"], start=1):
        pid = pile["id"]
        sid = assign.get(pid)
        entry: dict[str, Any] = {"density": None, "weight": None, "weighedKg": None}
        kg = scan.get("weighed", {}).get(pid)
        if kg is not None and sid is not None:
            wid = _weighed_id(scan, pid)
            entry["weighedKg"] = _claim(f"{scan['id']}_{pid}_weighed", float(kg), "kg", [wid])
        density = densities.get(sid) if sid else None
        if density is not None:
            entry["density"] = density
            if kg is not None:
                low = high = float(kg)
                evidence = [_weighed_id(scan, pid)]
            else:
                lo_v, hi_v = pile["volumeRangeM3"]
                low, high = lo_v * density["lowKgM3"], hi_v * density["highKgM3"]
                inputs = {
                    "scan": scan["id"],
                    "pile": pid,
                    "volume": [lo_v, hi_v],
                    "density": density,
                }
                evidence = []
                for key, value, vol, dens in (
                    ("low", low, lo_v, density["lowKgM3"]),
                    ("high", high, hi_v, density["highKgM3"]),
                ):
                    evidence.append(
                        _put(
                            app,
                            Evidence(
                                id=ids.calc_id("scan_weight", inputs, key),
                                kind="calc",
                                label=f"더미 {index} 무게 {'하한' if key == 'low' else '상한'}",
                                value=round(value, 1),
                                unit="kg",
                                excerpt=(
                                    f"더미 {index} 부피 {'하한' if key == 'low' else '상한'} {_fmt(vol)} m³ × "
                                    f"{density['label']} {_fmt(dens, 1)} kg/m³ = {_fmt(value, 0)} kg"
                                ),
                                source=f"보관장 3D 스캔 {scan['id']} 무게 계산",
                                as_of=day,
                                access=(org,),
                                meta={"inputs": [_volume_id(scan, pid), *density["evidence"]]},
                            ),
                        )
                    )  # fmt: skip
            low_r, high_r = round(low, 0), round(high, 0)
            entry["weight"] = {
                "low": _claim(f"{scan['id']}_{pid}_weightLow", low_r, "kg", [evidence[0]]),
                "high": _claim(f"{scan['id']}_{pid}_weightHigh", high_r, "kg", [evidence[-1]]),
            }
            weights.append(
                (
                    pid,
                    low_r,
                    high_r,
                    entry["weight"]["low"]["evidence"] + entry["weight"]["high"]["evidence"],
                )
            )
        out_piles[pid] = entry
    # 합계
    total_volume = round(sum(p["volumeM3"] for p in result["piles"]), 3)
    volume_id = _put(
        app,
        Evidence(
            id=ids.calc_id("scan_volume_sum", {"scan": scan["id"], "v": [p["volumeM3"] for p in result["piles"]]}, "sum"),
            kind="calc",
            label="더미 부피 합계",
            value=total_volume,
            unit="m³",
            excerpt=" + ".join(f"{_fmt(p['volumeM3'])}" for p in result["piles"]) + f" = {_fmt(total_volume)} m³",
            source=f"보관장 3D 스캔 {scan['id']} 합계",
            as_of=day,
            access=(org,),
            meta={"inputs": [_volume_id(scan, p["id"]) for p in result["piles"]]},
        ),
    )  # fmt: skip
    summary: dict[str, Any] = {
        "pileCount": len(result["piles"]),
        "volume": _claim(f"{scan['id']}_volume", total_volume, "m³", [volume_id]),
        "weight": None,
        "weightPending": len(result["piles"]) - len(weights),
        "floorAreaM2": result["floor"]["areaM2"] if result["floor"] else None,
    }
    if weights:
        lows, highs = sum(w[1] for w in weights), sum(w[2] for w in weights)
        inputs = sorted({e for w in weights for e in w[3]})
        sums = []
        for key, value in (("low", lows), ("high", highs)):
            sums.append(
                _put(
                    app,
                    Evidence(
                        id=ids.calc_id("scan_weight_sum", {"scan": scan["id"], "w": weights}, key),
                        kind="calc",
                        label=f"무게 합계 {'하한' if key == 'low' else '상한'}",
                        value=value,
                        unit="kg",
                        excerpt=f"무게를 잰 더미 {len(weights)}개의 {'하한' if key == 'low' else '상한'} 합 = {_fmt(value, 0)} kg"
                        + (f" (무게 기준이 없는 더미 {summary['weightPending']}개는 빠짐)" if summary["weightPending"] else ""),
                        source=f"보관장 3D 스캔 {scan['id']} 합계",
                        as_of=day,
                        access=(org,),
                        meta={"inputs": inputs},
                    ),
                )
            )  # fmt: skip
        summary["weight"] = {
            "low": _claim(f"{scan['id']}_weightLow", lows, "kg", [sums[0]]),
            "high": _claim(f"{scan['id']}_weightHigh", highs, "kg", [sums[1]]),
        }
    return {
        "inputs": _inputs(app, scan),
        "piles": out_piles,
        "summary": summary,
        "layout": _layout(app, scan, streams, assign, day),
    }


def _pickup(scan: dict[str, Any]) -> dict[str, Any]:
    chosen = scan.get("pickup")
    if isinstance(chosen, dict):
        return {"x": chosen["x"], "y": chosen["y"], "source": "manual"}
    result = scan["result"]
    start = result["camera"].get("start")
    if start is not None:
        return {"x": start[0], "y": start[1], "source": "start"}
    poly = result["floor"]["polygon"]
    return {
        "x": round(sum(p[0] for p in poly) / len(poly), 3),
        "y": round(sum(p[1] for p in poly) / len(poly), 3),
        "source": "start",
    }


def _layout(
    app: App, scan: dict[str, Any], streams: dict[str, Any], assign: dict[str, str], day: str
) -> dict[str, Any]:
    result = scan["result"]
    pickup = _pickup(scan)
    rules = dict(layout.DEFAULT_RULES)
    rule_ids = []
    for key, (slug, text, why) in RULE_TEXT.items():
        rule_ids.append(
            _put(
                app,
                Evidence(
                    id=ids.assumption_id(slug, "v1"),
                    kind="assumption",
                    label=text.format(v=rules[key]),
                    value=rules[key],
                    unit="m",
                    excerpt=f"{text.format(v=rules[key])}: {why}",
                    source="Re:Source AI 보관장 정리 규칙 v1",
                    as_of="2026-11-20",
                    access=(PUBLIC,),
                ),
            )
        )
    zone_of = {p["id"]: assign.get(p["id"]) for p in result["piles"]}
    names = {sid: str(streams[sid].get("name") or sid) for sid in set(assign.values())}
    planned = layout.propose(
        [tuple(p) for p in result["floor"]["polygon"]],
        result["piles"],
        zone_of,
        names,
        (pickup["x"], pickup["y"]),
        rules,
    )
    return {
        "pickup": pickup,
        "rules": {**rules, "evidence": rule_ids},
        "current": planned["current"],
        "proposal": planned["proposal"],
        "unavailable": planned["unavailable"],
    }


# ── 화면 ─────────────────────────────────────────────────────────────
def view(app: App, scan: dict[str, Any]) -> dict[str, Any]:
    result = scan.get("result") if scan.get("status") == "done" else None
    derived = scan.get("derived") or {}
    base = {
        "id": scan["id"],
        "orgId": scan["orgId"],
        "createdAt": scan["createdAt"],
        "source": scan.get("source", "video"),
        "frameCount": scan.get("frameCount"),
        "status": scan["status"],
        "progress": scan.get("progress") if scan["status"] == "processing" else None,
        "failure": {"message": scan["failure"]["message"]} if scan.get("failure") else None,
        "summary": None,
        "piles": [],
        "layout": None,
        "floor": None,
        "camera": None,
        "bounds": None,
        "pointsCount": 0,
        "quality": None,
    }
    if result is None:
        return base
    streams = {s["id"]: s for s in app.store.list_streams(scan["orgId"])}
    assign = scan.get("assign", {})
    piles = []
    for index, pile in enumerate(result["piles"], start=1):
        pid = pile["id"]
        sid = assign.get(pid) if assign.get(pid) in streams else None
        extra = derived.get("piles", {}).get(pid, {})
        piles.append(
            {
                "id": pid,
                "name": f"더미 {index}",
                "center": pile["center"],
                "footprint": pile["footprint"],
                "sizeM": pile["sizeM"],
                "yawDeg": pile["yawDeg"],
                "heightM": pile["heightM"],
                "volume": _claim(f"{scan['id']}_{pid}_volume", pile["volumeM3"], "m³", [_volume_id(scan, pid)]),
                "volumeRangeM3": pile["volumeRangeM3"],
                "coverage": pile["coverage"],
                "thumbnail": pile["thumbnail"],
                "streamId": sid,
                "streamName": str(streams[sid].get("name")) if sid else None,
                "density": extra.get("density"),
                "weight": extra.get("weight"),
                "weighedKg": extra.get("weighedKg"),
                "note": "바닥 자국의 일부만 찍혀 부피 범위가 넓습니다." if pile["coverage"] < LOW_COVERAGE else None,
            }
        )  # fmt: skip
    notes = [NOTES[w] for w in result["warnings"] if w in NOTES]
    spread = result["scale"]["spreadPct"]
    if spread >= 3:
        notes.append(
            f"실제 크기 추정이 ±{_fmt(spread, 1)}% 안에서 흔들려 부피 범위에 반영했습니다."
        )
    return {
        **base,
        "summary": derived.get("summary"),
        "piles": piles,
        "layout": derived.get("layout"),
        "floor": {"polygon": result["floor"]["polygon"], "areaM2": result["floor"]["areaM2"]},
        "camera": {"path": result["camera"]["path"]},
        "bounds": result["bounds"],
        "pointsCount": result["pointsCount"],
        "quality": {"frames": result["frames"]["used"], "scaleSpreadPct": spread, "notes": notes},
    }


def _current(app: App, scan: dict[str, Any]) -> dict[str, Any]:
    """조회 경로: 진행 중이면 갱신하고, 폐기물 정보가 바뀌었으면(실제 조직만) 다시 계산한다."""
    scan = refresh(app, scan)
    stale = scan.get("status") == "done" and not app.read_only
    if stale and (scan.get("derived") or {}).get("inputs") != _inputs(app, scan):
        scan["derived"] = derive(app, scan)
        scan = app.store.put_scan(scan)
    return scan


def list_scans(app: App, org_id: str, viewer: str) -> dict[str, Any]:
    _owner(app, org_id, viewer, "emitter")
    scans = sorted(
        app.store.list_scans(org_id), key=lambda s: (s["createdAt"], s["id"]), reverse=True
    )
    client = getattr(app, "recon", None)
    can = (
        not app.read_only
        and client is not None
        and client.configured
        and client.health() is not None
    )
    return {"canScan": can, "scans": [view(app, _current(app, s)) for s in scans[:LIST_LIMIT]]}


def get_scan(app: App, org_id: str, scan_id: str, viewer: str) -> dict[str, Any]:
    _owner(app, org_id, viewer, "emitter")
    return view(app, _current(app, _scan(app, org_id, scan_id)))


def scan_points(app: App, org_id: str, scan_id: str, viewer: str) -> bytes:
    _owner(app, org_id, viewer, "emitter")
    scan = _scan(app, org_id, scan_id)
    if scan.get("status") != "done":
        raise ServiceError("POINTS_NOT_READY", "아직 3D 장면이 준비되지 않았습니다.", 404)
    try:
        stored = app.store.get_scan_points(scan_id)
    except NotFound:
        raise ServiceError("POINTS_NOT_READY", "아직 3D 장면이 준비되지 않았습니다.", 404) from None
    return base64.b64decode(stored["data"])


def _done(app: App, org_id: str, scan_id: str, viewer: str) -> dict[str, Any]:
    _owner(app, org_id, viewer, "emitter")
    scan = _current(app, _scan(app, org_id, scan_id))
    if scan.get("status") != "done":
        raise ServiceError("SCAN_NOT_READY", "분석이 끝난 뒤에 고칠 수 있습니다.", 409)
    return scan


def update_pile(
    app: App, org_id: str, scan_id: str, pile_id: str, patch: dict[str, Any], viewer: str
) -> dict[str, Any]:
    """더미의 폐기물(streamId)과 계량한 무게(weighedKg)를 고친다. null이면 지운다."""
    if not isinstance(patch, dict) or not ({"streamId", "weighedKg"} & set(patch)):
        raise ServiceError("INVALID_BODY", "streamId나 weighedKg를 보내 주세요.", 422)
    scan = _done(app, org_id, scan_id, viewer)
    piles = {p["id"]: p for p in scan["result"]["piles"]}
    if pile_id not in piles:
        raise ServiceError("PILE_NOT_FOUND", "그런 더미가 없습니다.", 404)
    index = list(piles).index(pile_id) + 1
    assign, weighed = dict(scan.get("assign") or {}), dict(scan.get("weighed") or {})
    if "streamId" in patch:
        sid = patch["streamId"]
        if sid is None:
            assign.pop(pile_id, None)
            weighed.pop(pile_id, None)  # 폐기물이 없으면 계량값으로 밀도를 정할 수 없다
        else:
            try:
                stream = app.store.get_stream(str(sid))
            except NotFound:
                raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404) from None
            if stream.get("orgId") != org_id:
                raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404)
            assign[pile_id] = stream["id"]
    if "weighedKg" in patch:
        kg = patch["weighedKg"]
        if kg is None:
            weighed.pop(pile_id, None)
        elif isinstance(kg, bool) or not isinstance(kg, (int, float)) or not 0 < kg <= 100000:
            raise ServiceError(
                "INVALID_VALUE", "계량한 무게는 0보다 크고 100,000 kg 이하인 숫자여야 합니다.", 422
            )
        elif pile_id not in assign:
            raise ServiceError("STREAM_REQUIRED", "먼저 이 더미의 폐기물을 골라 주세요.", 422)
        else:
            weighed[pile_id] = float(kg)
            _put(
                app,
                Evidence(
                    id=_weighed_id(scan, pile_id),
                    kind="human",
                    label=f"더미 {index} 계량 무게",
                    value=float(kg),
                    unit="kg",
                    excerpt=f"담당자가 적은 더미 {index}의 계량 무게 {_fmt(float(kg), 1)} kg",
                    source=f"보관장 3D 스캔 {scan['id']} · 담당자 입력",
                    as_of=str(app.today),
                    access=(org_id,),
                ),
            )
    scan.update(assign=assign, weighed=weighed)
    scan["derived"] = derive(app, scan)
    return view(app, app.store.put_scan(scan))


def update_layout(
    app: App, org_id: str, scan_id: str, patch: dict[str, Any], viewer: str
) -> dict[str, Any]:
    """상차 지점을 바꾼다. {"pickup": {"x", "y"}} 또는 {"pickup": null}(영상을 찍기 시작한 곳)."""
    if not isinstance(patch, dict) or "pickup" not in patch:
        raise ServiceError("INVALID_BODY", "pickup을 보내 주세요.", 422)
    scan = _done(app, org_id, scan_id, viewer)
    pickup = patch["pickup"]
    if pickup is None:
        scan["pickup"] = None
    else:
        if not isinstance(pickup, dict):
            raise ServiceError("INVALID_VALUE", "pickup은 {x, y}여야 합니다.", 422)
        point = []
        for key in ("x", "y"):
            value = pickup.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not -1000 <= value <= 1000
            ):
                raise ServiceError("INVALID_VALUE", "상차 지점 좌표가 잘못됐습니다.", 422)
            point.append(round(float(value), 3))
        bounds = scan["result"].get("bounds")
        if bounds and not (
            bounds["min"][0] - 5 <= point[0] <= bounds["max"][0] + 5
            and bounds["min"][1] - 5 <= point[1] <= bounds["max"][1] + 5
        ):
            raise ServiceError("INVALID_VALUE", "상차 지점이 보관장에서 너무 멉니다.", 422)
        scan["pickup"] = {"x": point[0], "y": point[1]}
    scan["derived"] = derive(app, scan)
    return view(app, app.store.put_scan(scan))
