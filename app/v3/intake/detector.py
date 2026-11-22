"""사진·영상 경로. 탐지 채점 서버(H100의 rsai-detector)를 부르고 결과를 model 근거로 등록한다.

- 엔드포인트가 설정되지 않았으면 부르지 않는다. 탐지 결과를 지어내지 않는다(서비스가
  detectorUnavailable로 답한다).
- 응답은 기존 추론 결과 계약(app/ml/track.py의 run() 결과, app/ml/contract.py가 읽는 모양)이다:
  items[{material, area_ratio, confidence, count_estimate}], primary_material,
  contamination{overall_ratio, reliable, note} | null, detections[{id, label, kind, confidence, box, ratio}],
  mode("photos" | "video"), frames_analyzed, tracks_total(영상의 ByteTrack 트랙 수), rep_frame,
  backend, model_version, input_domain, latency_sec.
- 사진으로는 수지 종류(PP·PE·ABS)를 가를 수 없다. 재질 추정은 탐지 분류(경질 플라스틱, 필름 등)까지만
  등록하고 흐름의 material은 바꾸지 않는다.

탐지 서버는 H100 계산 인스턴스의 rsai-detector(app/ml/score_api.py를 GPU로 띄운 것, infra/h100/detector)다.
요청 본문은 여기서 정한 모양(REQUEST_SHAPE)이다. H100은 서비스하지 않을 때 끄므로, 꺼져 있으면 DetectorOffline이고
서비스는 사진·영상 분석만 쉰다(CPU 예비 서버는 두지 않는다, 2026-11-22 결정).
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from app.ml.contract import ITEM_OF, LABEL_KO  # 재질 분류 → 품목, 한국어 이름(순수 파이썬)
from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.intake._http import Transport, default_transport
from app.v3.textutil import josa

MODEL_NAME = "resource-ai-detector"
IMAGE_TYPES = ("jpg", "jpeg", "png", "webp", "bmp")
VIDEO_TYPES = ("mp4", "mov", "avi", "webm", "mkv")
MODES = ("photos", "video")
REQUEST_SHAPE = '{"input_data": {"filename", "media_type": "image | video", "content_base64"}}'
ENV_ENDPOINT, ENV_KEY, ENV_DEPLOYMENT = (
    "V3_DETECTOR_ENDPOINT",
    "V3_DETECTOR_API_KEY",
    "V3_DETECTOR_DEPLOYMENT",
)


class DetectorError(RuntimeError):
    """탐지 엔드포인트 호출이나 응답이 잘못됐다."""


class DetectorOffline(DetectorError):
    """탐지 서버에 닿지 않는다. H100 계산 인스턴스를 꺼 두었을 때다(서비스하지 않을 때 끈다)."""


# 탐지 서버가 꺼져 있으면 사설 주소로 보낸 연결이 응답 없이 시간을 넘긴다. 보내기 전에 짧게 확인한다
PROBE_TIMEOUT = 3.0


def tcp_probe(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """주소의 포트에 TCP 연결이 열리는가. 요청은 보내지 않는다."""
    import socket
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def media_type(filename: str) -> str | None:
    """파일 이름의 확장자로 사진("image")인지 영상("video")인지 본다. 모르는 형식이면 None."""
    suffix = str(filename or "").rsplit(".", 1)[-1].lower() if "." in str(filename or "") else ""
    if suffix in IMAGE_TYPES:
        return "image"
    if suffix in VIDEO_TYPES:
        return "video"
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _ratio(value: Any, what: str) -> float:
    number = _number(value)
    if number is None or not 0.0 <= number <= 1.0:
        raise DetectorError(
            f"탐지 엔드포인트 응답의 {josa(what, '이가')} 0~1 사이의 수가 아닙니다."
        )
    return number


def read_result(payload: Any) -> dict[str, Any]:
    """추론 결과 계약을 읽어 확인한다. 모양이 다르면 DetectorError. 값을 채워 넣지 않는다."""
    if isinstance(payload, str):  # score.py가 JSON 문자열을 한 번 더 감싸는 경우
        try:
            payload = json.loads(payload)
        except ValueError as error:
            raise DetectorError("탐지 엔드포인트 응답 형식이 잘못됐습니다.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise DetectorError("탐지 엔드포인트 응답에 items가 없습니다.")
    items = []
    for raw in payload["items"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("material"), str):
            raise DetectorError("탐지 엔드포인트 응답의 items 항목에 material이 없습니다.")
        items.append(
            {
                "material": raw["material"],
                "areaRatio": _ratio(raw.get("area_ratio"), "area_ratio"),
                "confidence": _ratio(raw.get("confidence"), "confidence"),
                "countEstimate": int(_number(raw.get("count_estimate")) or 0),
            }
        )
    primary = payload.get("primary_material")
    if primary is not None and primary not in {i["material"] for i in items}:
        raise DetectorError("탐지 엔드포인트 응답의 primary_material이 items에 없습니다.")
    contamination = payload.get("contamination")
    if contamination is not None:
        if not isinstance(contamination, dict):
            raise DetectorError("탐지 엔드포인트 응답의 contamination 형식이 잘못됐습니다.")
        contamination = {
            "overallRatio": _ratio(contamination.get("overall_ratio"), "overall_ratio"),
            "reliable": contamination.get("reliable") is True,
            "note": str(contamination.get("note") or ""),
            "judgedObjects": int(_number(contamination.get("judged_objects")) or 0),
        }
    detections = payload.get("detections")
    if detections is None:
        detections = []
    if not isinstance(detections, list) or not all(isinstance(d, dict) for d in detections):
        raise DetectorError("탐지 엔드포인트 응답의 detections 형식이 잘못됐습니다.")
    mode = payload.get("mode")
    if mode not in MODES:
        raise DetectorError("탐지 엔드포인트 응답의 mode가 photos나 video가 아닙니다.")
    return {
        "mode": mode,
        "primary": primary,
        "items": items,
        "contamination": contamination,
        "detections": [
            {
                key: d.get(key)
                for key in ("id", "label", "kind", "confidence", "box", "ratio")
                if key in d
            }
            for d in detections
        ],
        "framesAnalyzed": int(_number(payload.get("frames_analyzed")) or 0),
        # 영상이면 ByteTrack이 묶은 트랙 수, 사진이면 0이다(계약 그대로)
        "tracksTotal": int(_number(payload.get("tracks_total")) or 0),
        "repFrame": payload.get("rep_frame"),
        "backend": str(payload.get("backend") or ""),
        "modelVersion": str(payload.get("model_version") or ""),
        "inputDomain": payload.get("input_domain"),
    }


class DetectorClient:
    """탐지 채점 서버(H100 계산 인스턴스의 rsai-detector, infra/h100/detector). 주소가 없으면 configured가 거짓이고
    부르지 않는다. 서버에 닿지 않으면(H100을 꺼 두었을 때) DetectorOffline이다.

    probe(url) -> bool은 보내기 전 연결 확인이다. 주지 않으면 기본 transport일 때만 TCP 연결로 확인한다.
    """

    def __init__(
        self,
        endpoint_url: str = "",
        api_key: str = "",
        transport: Transport | None = None,
        deployment: str = "",
        probe: Callable[[str], bool] | None = None,
    ) -> None:
        self.endpoint_url = (endpoint_url or "").strip()
        self.api_key = api_key or ""
        self.transport = transport or default_transport
        self.deployment = (deployment or "").strip()
        self.probe = probe

    @property
    def configured(self) -> bool:
        return bool(self.endpoint_url)

    def detect(self, data: bytes, filename: str) -> dict[str, Any]:
        """파일 하나를 보내 추론 결과(read_result가 읽은 모양)를 받는다."""
        if not self.configured:
            raise DetectorError("탐지 모델이 연결되지 않았습니다")
        kind = media_type(filename)
        if kind is None:
            raise DetectorError("사진이나 영상 파일이 아닙니다.")
        body = json.dumps(
            {
                "input_data": {
                    "filename": filename,
                    "media_type": kind,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        if self.deployment:
            headers["azureml-model-deployment"] = self.deployment
        # 실제로 연결하는 transport일 때만 확인한다(시험의 가짜 transport는 연결하지 않는다)
        probe = self.probe or (tcp_probe if self.transport is default_transport else None)
        if probe is not None and not probe(self.endpoint_url):
            raise DetectorOffline("사진·영상 분석 서버가 지금 꺼져 있습니다.")
        try:
            status, _, raw = self.transport("POST", self.endpoint_url, headers, body)
        except (OSError, ValueError) as error:
            raise DetectorError(
                f"탐지 엔드포인트에 연결하지 못했습니다: {type(error).__name__}"
            ) from error
        if status != 200:
            raise DetectorError(f"탐지 엔드포인트가 {josa(str(status), '을를')} 돌려줬습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise DetectorError("탐지 엔드포인트 응답이 JSON이 아닙니다.") from error
        result = read_result(payload)
        expected = "video" if kind == "video" else "photos"
        if result["mode"] != expected:
            raise DetectorError("탐지 엔드포인트가 보낸 파일과 다른 종류의 결과를 돌려줬습니다.")
        return result


def client_from_env(env: Mapping[str, str], transport: Transport | None = None):
    """V3_DETECTOR_ENDPOINT가 있으면 DetectorClient, 없으면 None(탐지 모델 미연결)."""
    endpoint = (env.get(ENV_ENDPOINT) or "").strip()
    if not endpoint:
        return None
    return DetectorClient(
        endpoint, env.get(ENV_KEY) or "", transport, env.get(ENV_DEPLOYMENT) or ""
    )


def register_detection(
    registry: Any,
    org_id: str,
    stream: dict[str, Any],
    result: dict[str, Any],
    filename: str,
    today: str,
) -> dict[str, Any]:
    """탐지 결과를 model 근거로 등록하고, 흐름의 photo에 넣을 값과 화면에 줄 요약을 돌려준다.

    - 이물 비율(면적 기준)은 %로 바꿔 `model:<모델>:<버전>:<흐름>-오염비율` 근거가 된다. 검증된 촬영 환경의
      사진(reliable)일 때만이다. 그 밖의 사진은 개수(`…-이물개수`, 탐지한 물체와 이물이 보인 곳)만 남긴다.
      상태 모델이 돌지 않아 contamination이 없으면 등록하지 않는다(값을 지어내지 않는다).
    - 재질 추정은 `model:<모델>:<버전>:<흐름>-재질` 근거가 된다(값은 숫자가 아니고 분류 이름이다).
    """
    sid = stream["id"]
    version = result["modelVersion"] or "unknown"
    source = f"탐지 모델 {MODEL_NAME} {version} · {filename}"
    base_meta = {
        "ownerOrgId": org_id,
        "streamId": sid,
        "file": filename,
        "mode": result["mode"],
        "framesAnalyzed": result["framesAnalyzed"],
        "tracksTotal": result["tracksTotal"],
        "backend": result["backend"],
        "inputDomain": result["inputDomain"],
    }
    registered: list[str] = []
    material = None
    primary = result["primary"]
    if primary is not None:
        item = next(i for i in result["items"] if i["material"] == primary)
        label = LABEL_KO.get(primary, primary)
        evidence = registry.put(
            Evidence(
                id=ids.model_id(MODEL_NAME, version, f"{sid}-재질"),
                kind="model",
                label="사진 추정 재질",
                excerpt=f"사진에서 가장 넓게 잡힌 재질 분류: {label} (수지 종류는 사진으로 가르지 못함)",
                source=source,
                as_of=today,
                access=(org_id,),
                meta={**base_meta, "class": primary, "items": result["items"]},
            )
        )
        registered.append(evidence.id)
        material = {
            "class": primary,
            "label": label,
            "wasteItem": ITEM_OF.get(primary),  # 대회 품목 밖(유리·전지)이면 None
            "confidence": item["confidence"],
            "evidence": [evidence.id],
        }
    contamination = None
    objects = None
    found = result["contamination"]
    if found is not None and not found["reliable"]:
        # 검증된 촬영 환경(선별장)이 아닌 사진은 이물 비율(%)을 등록하지 않는다. 상태 모델이 깨끗한 물체에는 박스를
        # 거의 그리지 않아, 비율의 분모가 이물이 보인 물체만으로 차서 거의 항상 100%가 된다(공개 검증 사진 564장 중
        # 비율이 나온 299장의 298장이 100%, 2026-11-22). 대신 탐지한 물체와 이물이 보인 곳의 개수만 남긴다
        objects = _object_counts(registry, org_id, sid, result, found, version, source, base_meta, today)
        registered.append(objects["evidence"][0])
    elif found is not None:
        percent = round(found["overallRatio"] * 100.0, 4)
        note = found["note"] or ("검증된 값" if found["reliable"] else "검증 안 된 참고값")
        evidence = registry.put(
            Evidence(
                id=ids.model_id(MODEL_NAME, version, f"{sid}-오염비율"),
                kind="model",
                label="사진 추정 오염 비율",
                value=percent,
                unit="%",
                excerpt=f"사진에서 추정한 이물 면적 비율 {percent:g}% ({note})",
                source=source,
                as_of=today,
                access=(org_id,),
                meta={
                    **base_meta,
                    "overallRatio": found["overallRatio"],
                    "reliable": found["reliable"],
                    "judgedObjects": found["judgedObjects"],
                },
            )
        )
        registered.append(evidence.id)
        contamination = {
            "contaminationPct": percent,
            "reliable": found["reliable"],
            "note": note,
            "evidence": [evidence.id],
        }
    return {
        "material": material,
        "contamination": contamination,
        "objects": objects,
        "evidence": registered,
    }


def _object_counts(registry, org_id, sid, result, found, version, source, base_meta, today):
    """검증 안 된 사진의 탐지 결과를 개수로 남긴다: 탐지한 물체 수와 이물이 보인 곳(이물 박스) 수."""
    detections = result["detections"]
    items = sum(1 for d in detections if d.get("kind") == "item")
    spots = sum(1 for d in detections if d.get("kind") == "contaminant")
    note = found["note"] or "검증 안 된 참고값"
    evidence = registry.put(
        Evidence(
            id=ids.model_id(MODEL_NAME, version, f"{sid}-이물개수"),
            kind="model",
            label="사진에서 이물이 보인 곳",
            value=spots,
            unit="곳",
            excerpt=(
                f"탐지한 물체 {items}개 가운데 이물이 보인 곳 {spots}곳. 검증된 촬영 환경(선별장)의 사진이 "
                f"아니어서 이물 비율(%)은 내지 않았습니다. ({note})"
            ),
            source=source,
            as_of=today,
            access=(org_id,),
            meta={
                **base_meta,
                "items": items,
                "contaminantSpots": spots,
                "reliable": False,
                "judgedObjects": found["judgedObjects"],
            },
        )
    )
    return {
        "items": items,
        "contaminantSpots": spots,
        "note": note,
        "evidence": [evidence.id],
    }
