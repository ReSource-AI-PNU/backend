"""등급 모델. 측정값이 있으면 측정 등급을 우선하고, 없으면 사진 추정으로 낸다.

기준값(grade_thresholds.v1.json)은 팀 가정값이다. 합성 데이터는 이 기준값에서 만들기 때문에
합성 데이터 점수는 모델이 기준값을 얼마나 따라 하는지만 보여 주고, 실제 등급 정확도가 아니다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.intake._http import Transport, default_transport
from app.v3.textutil import josa

GRADES = ("A", "B", "C", "등외")
MODEL_NAME = "resource-ai-grade"
ENDPOINT_NAME = "rsai-grade"
FEATURES = ("material", "contamination_pct", "moisture_pct")
SYNTHETIC_LABEL = "합성 데이터 기준"
_THRESHOLD_FILE = Path(__file__).with_name("grade_thresholds.v1.json")
_cache: dict[str, Any] = {}


class GradeModelError(RuntimeError):
    """등급 엔드포인트가 설정돼 있는데 호출하지 못했거나 응답이 잘못됐다. 규칙으로 몰래 대신하지 않는다."""


def thresholds() -> dict[str, Any]:
    if not _cache:
        _cache.update(json.loads(_THRESHOLD_FILE.read_text(encoding="utf-8")))
    return _cache


def thresholds_evidence() -> Evidence:
    """등급 기준값 전체를 가리키는 assumption 근거. 화면에는 "가정값"으로 나온다."""
    data = thresholds()
    return Evidence(
        id=ids.assumption_id("등급기준", data["version"]),
        kind="assumption",
        label="재질별 등급 기준값",
        excerpt=json.dumps(
            {"default": data["default"], "materials": data["materials"]},
            ensure_ascii=False,
            sort_keys=True,
        ),
        source=f"app/v3/intake/{_THRESHOLD_FILE.name} ({data['note']})",
        as_of=data["asOf"],
        meta={"version": data["version"], "file": _THRESHOLD_FILE.name},
    )


def _fmt(value: float) -> str:
    return f"{value:g}"


def grade_by_thresholds(
    material: str | None, contamination_pct: float | None, moisture_pct: float | None
) -> dict[str, Any]:
    """기준값 규칙. 오염도와 수분이 둘 다 상한 이하인 가장 높은 등급을 준다."""
    data = thresholds()
    key = (material or "").strip().upper()
    table = data["materials"].get(key)
    used_default = table is None
    if used_default:
        table = data["default"]
    base = {"thresholdsVersion": data["version"], "usedDefault": used_default}
    if contamination_pct is None or moisture_pct is None:
        lacking = "오염도" if contamination_pct is None else "수분"
        return {"grade": None, "rule": f"{lacking} 값이 없어 등급을 낼 수 없습니다.", **base}
    name = key if not used_default else f"{key or '재질 미확인'}(기본 기준)"
    for grade in GRADES[:-1]:
        limit = table[grade]
        if (
            contamination_pct <= limit["maxContaminationPct"]
            and moisture_pct <= limit["maxMoisturePct"]
        ):
            rule = (
                f"{name} {grade}등급 상한: 오염도 {_fmt(limit['maxContaminationPct'])}% 이하, "
                f"수분 {_fmt(limit['maxMoisturePct'])}% 이하"
            )
            return {"grade": grade, "rule": rule, **base}
    limit = table["C"]
    rule = (
        f"{name} C등급 상한(오염도 {_fmt(limit['maxContaminationPct'])}%, "
        f"수분 {_fmt(limit['maxMoisturePct'])}%)을 넘음"
    )
    return {"grade": "등외", "rule": rule, **base}


class _Lcg:
    """재현되는 난수. random 모듈은 파이썬 판에 따라 수열이 달라질 수 있어 쓰지 않는다."""

    def __init__(self, seed: int) -> None:
        self.state = seed % 2**32

    def uniform(self) -> float:
        self.state = (1664525 * self.state + 1013904223) % 2**32
        return self.state / 2**32


def make_synthetic_dataset(n: int, seed: int = 0) -> list[dict[str, Any]]:
    """기준값에서 합성 학습 데이터를 만든다. 모든 행에 synthetic=True가 붙는다."""
    data = thresholds()
    materials = sorted(data["materials"])
    rng = _Lcg(seed)
    rows = []
    for _ in range(max(0, n)):
        material = materials[int(rng.uniform() * len(materials)) % len(materials)]
        # 낮은 값 쪽에 몰리게 제곱한다. 등급 경계 근처가 고루 나오게 하려는 것이다.
        contamination = round(rng.uniform() ** 2 * 15, 2)
        moisture = round(rng.uniform() ** 2 * 6, 2)
        rows.append(
            {
                "material": material,
                "contamination_pct": contamination,
                "moisture_pct": moisture,
                "grade": grade_by_thresholds(material, contamination, moisture)["grade"],
                "synthetic": True,
                "thresholds_version": data["version"],
            }
        )
    return rows


class GradeModelClient:
    """Azure ML 관리형 온라인 엔드포인트 rsai-grade. 주소가 없으면 기준값 규칙으로 답한다."""

    def __init__(
        self,
        endpoint_url: str = "",
        api_key: str = "",
        transport: Transport | None = None,
        version: str = "v1",
    ) -> None:
        self.endpoint_url = endpoint_url.strip()
        self.api_key = api_key
        self.transport = transport or default_transport
        self.version = version

    @property
    def configured(self) -> bool:
        return bool(self.endpoint_url)

    def predict(
        self, material: str | None, contamination_pct: float | None, moisture_pct: float | None
    ) -> dict[str, Any]:
        if not self.configured or contamination_pct is None or moisture_pct is None:
            result = grade_by_thresholds(material, contamination_pct, moisture_pct)
            return {
                **result,
                "basis": "threshold_rule",
                "modelVersion": result["thresholdsVersion"],
            }
        body = json.dumps(
            {
                "input_data": {
                    "columns": list(FEATURES),
                    "data": [[(material or "").strip().upper(), contamination_pct, moisture_pct]],
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        try:
            status, _, raw = self.transport("POST", self.endpoint_url, headers, body)
        except OSError as error:
            raise GradeModelError(f"등급 엔드포인트에 연결하지 못했습니다: {error}") from error
        if status != 200:
            raise GradeModelError(f"등급 엔드포인트가 {josa(str(status), '을를')} 돌려줬습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, str):  # score.py가 JSON 문자열을 한 번 더 감싸는 경우
                payload = json.loads(payload)
            grade = payload[0] if isinstance(payload, list) else payload["predictions"][0]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise GradeModelError("등급 엔드포인트 응답 형식이 잘못됐습니다.") from error
        if grade not in GRADES:
            raise GradeModelError(f"등급 엔드포인트가 모르는 등급을 돌려줬습니다: {grade!r}")
        return {
            "grade": grade,
            "rule": f"{ENDPOINT_NAME} 엔드포인트 예측",
            "basis": "endpoint",
            "modelVersion": self.version,
        }


def _evidence_ids(block: dict[str, Any] | None) -> list[str]:
    return list((block or {}).get("evidence") or [])


def grade_stream(
    registry: Any,
    org_id: str,
    stream: dict[str, Any],
    client: GradeModelClient | None,
    today: str,
    *,
    asked_moisture_pct: float | None = None,
) -> dict[str, Any]:
    """흐름 하나의 등급. 결과는 model 근거로 등록한다.

    사진 추정에 쓰는 수분은 질문으로 받은 값(asked_moisture_pct 또는 stream["askedMoisturePct"])이다.
    측정값이 함께 있을 때는 비교를 위해 측정 수분을 쓴다.
    """
    client = client or GradeModelClient()
    stream_id = stream["id"]
    material = stream.get("material")
    measure = stream.get("measure") or {}
    photo = stream.get("photo") or {}
    if asked_moisture_pct is None:
        asked_moisture_pct = stream.get("askedMoisturePct")
    assumption = registry.put(thresholds_evidence())
    checks: list[dict[str, Any]] = []

    def run(basis: str, contamination: float, moisture: float | None, inputs: list[str]) -> dict:
        predicted = client.predict(material, contamination, moisture)
        result = {
            "grade": predicted["grade"],
            "basis": basis,
            "rule": predicted["rule"],
            "modelBasis": predicted["basis"],
            "inputs": {
                "material": material,
                "contaminationPct": contamination,
                "moisturePct": moisture,
            },
            "evidence": [],
        }
        if predicted["grade"] is None:
            return result
        label = "측정 등급" if basis == "measured" else "사진 추정 등급"
        input_evidence = [*inputs, assumption.id]
        evidence = registry.put(
            Evidence(
                id=ids.model_id(ENDPOINT_NAME, predicted["modelVersion"], f"{stream_id}-{basis}"),
                kind="model",
                label=f"{stream.get('name') or stream_id} {label}",
                value=predicted["grade"],
                excerpt=(
                    f"{label} {predicted['grade']}: 재질 {material or '미확인'}, "
                    f"오염도 {_fmt(contamination)}%, 수분 {_fmt(moisture)}%. {predicted['rule']}"
                ),
                source=(
                    f"{ENDPOINT_NAME} 엔드포인트 ({MODEL_NAME} {predicted['modelVersion']})"
                    if predicted["basis"] == "endpoint"
                    else f"등급 기준값 규칙 {predicted['modelVersion']} (가정값)"
                ),
                as_of=today,
                access=(org_id,),
                meta={
                    "tool": "grade_stream",
                    "basis": basis,
                    "modelBasis": predicted["basis"],
                    "inputs": result["inputs"],
                    "inputEvidence": input_evidence,
                    "streamId": stream_id,
                },
            )
        )
        result["evidence"] = [evidence.id]
        return result

    measured = None
    if measure.get("contaminationPct") is not None and measure.get("moisturePct") is not None:
        measured = run(
            "measured", measure["contaminationPct"], measure["moisturePct"], _evidence_ids(measure)
        )

    estimate = None
    if photo.get("contaminationPct") is not None:
        moisture = asked_moisture_pct
        if moisture is None and measured is not None:
            moisture = measure["moisturePct"]
        if moisture is None:
            checks.append(
                {
                    "key": "moisture_missing",
                    "status": "conditional",
                    "text": "수분 값이 없어 사진만으로는 등급을 낼 수 없습니다.",
                    "ask": "이 흐름의 수분은 대략 몇 %인가요? 모르면 '모름'으로 답해 주세요.",
                    "field": "moisturePct",
                    "evidence": _evidence_ids(photo),
                }
            )
        estimate = run("photo_estimate", photo["contaminationPct"], moisture, _evidence_ids(photo))

    if measured and estimate and estimate["grade"] and measured["grade"] != estimate["grade"]:
        checks.append(
            {
                "key": "grade_mismatch",
                "status": "conditional",
                "text": (
                    f"측정 등급({measured['grade']})과 사진 추정 등급({estimate['grade']})이 다릅니다. "
                    "측정 등급을 씁니다."
                ),
                "ask": "측정한 시료와 사진 속 물량이 같은 로트인가요? 다르면 최근 측정값을 올려 주세요.",
                "field": "measure",
                "evidence": [*measured["evidence"], *estimate["evidence"]],
            }
        )

    chosen = measured or (estimate if estimate and estimate["grade"] else None)
    if chosen is None and not checks:
        checks.append(
            {
                "key": "grade_inputs_missing",
                "status": "conditional",
                "text": "측정값도 사진 추정값도 없어 등급을 낼 수 없습니다.",
                "ask": "오염도와 수분 측정값이 있으면 CSV로 올려 주세요.",
                "field": "measure",
                "evidence": [],
            }
        )
    return {
        "streamId": stream_id,
        "grade": chosen["grade"] if chosen else None,
        "basis": chosen["basis"] if chosen else None,
        "label": {"measured": "측정 등급", "photo_estimate": "사진 추정"}.get(
            chosen["basis"] if chosen else "", "확인 필요"
        ),
        "modelBasis": chosen["modelBasis"] if chosen else None,
        "evidence": chosen["evidence"] if chosen else [],
        "measured": measured,
        "photoEstimate": estimate,
        "checks": checks,
        "assumptions": [assumption.id],
    }


def evaluate(
    dataset: Iterable[dict[str, Any]],
    predict: Callable[[str | None, float, float], str | None] | None = None,
) -> dict[str, Any]:
    """정확도와 등급별 재현율. 합성 행이 하나라도 섞여 있으면 "합성 데이터 기준"이라고 표시한다."""
    rows = list(dataset)
    if predict is None:

        def predict(material: str | None, contamination: float, moisture: float) -> str | None:
            return grade_by_thresholds(material, contamination, moisture)["grade"]

    support = dict.fromkeys(GRADES, 0)
    hit = dict.fromkeys(GRADES, 0)
    correct = 0
    for row in rows:
        truth = row["grade"]
        guess = predict(row.get("material"), row["contamination_pct"], row["moisture_pct"])
        support[truth] = support.get(truth, 0) + 1
        if guess == truth:
            correct += 1
            hit[truth] = hit.get(truth, 0) + 1
    synthetic = sum(1 for row in rows if row.get("synthetic"))
    return {
        "n": len(rows),
        "accuracy": correct / len(rows) if rows else None,
        "perGrade": {
            grade: {
                "support": support[grade],
                "recall": hit[grade] / support[grade] if support[grade] else None,
            }
            for grade in GRADES
        },
        "syntheticCount": synthetic,
        "realCount": len(rows) - synthetic,
        "synthetic": synthetic > 0,
        "label": SYNTHETIC_LABEL if synthetic > 0 else "실제 등급 기준",
    }


def deploy_gate(
    metrics: dict[str, Any],
    real_label_count: int,
    *,
    min_real_labels: int = 30,
    min_accuracy: float = 0.9,
) -> dict[str, Any]:
    """배포 게이트. 실제 등급이 쌓이기 전에는 합성 점수가 아무리 좋아도 통과시키지 않는다."""
    base = {"minRealLabels": min_real_labels, "minAccuracy": min_accuracy}
    if real_label_count < min_real_labels:
        return {
            "allowed": False,
            "reason": (
                f"실제 등급 기록이 {real_label_count}건뿐입니다. {min_real_labels}건 이상 쌓인 뒤에 "
                "실제 등급 기준으로 다시 평가합니다."
            ),
            **base,
        }
    if metrics.get("synthetic"):
        return {
            "allowed": False,
            "reason": f"이 점수는 {SYNTHETIC_LABEL}입니다. 실제 등급 기록만으로 평가한 점수가 필요합니다.",
            **base,
        }
    accuracy = metrics.get("accuracy")
    if accuracy is None or accuracy < min_accuracy:
        return {
            "allowed": False,
            "reason": f"실제 등급 기준 정확도가 기준({min_accuracy:.0%})에 못 미칩니다.",
            **base,
        }
    return {"allowed": True, "reason": "실제 등급 기준 정확도가 기준을 넘었습니다.", **base}
