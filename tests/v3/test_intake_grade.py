import json

import pytest

from app.v3.evidence.registry import InMemoryRegistry
from app.v3.intake.grade_model import (
    GRADES,
    SYNTHETIC_LABEL,
    GradeModelClient,
    GradeModelError,
    deploy_gate,
    evaluate,
    grade_by_thresholds,
    grade_stream,
    make_synthetic_dataset,
)

TODAY = "2026-11-17"


def stream(**extra):
    return {"id": "st_sprue", "orgId": "org_hanbit", "name": "스프루", "material": "PP", **extra}


def test_threshold_rule_boundaries():
    assert grade_by_thresholds("PP", 1.0, 0.5)["grade"] == "A"
    assert grade_by_thresholds("pp", 1.01, 0.5)["grade"] == "B"
    assert grade_by_thresholds("PP", 0.5, 2.0)["grade"] == "C"
    assert grade_by_thresholds("PP", 9, 0.1)["grade"] == "등외"
    unknown = grade_by_thresholds("PVC", 1, 0.5)
    assert unknown["grade"] == "A" and unknown["usedDefault"] is True
    assert grade_by_thresholds("PP", 1, None)["grade"] is None


def test_synthetic_dataset_is_deterministic_and_marked():
    first = make_synthetic_dataset(300, seed=7)
    assert first == make_synthetic_dataset(300, seed=7)
    assert first != make_synthetic_dataset(300, seed=8)
    assert all(row["synthetic"] is True for row in first)
    assert {row["grade"] for row in first} == set(GRADES)
    metrics = evaluate(first)
    assert metrics["accuracy"] == 1.0
    assert metrics["label"] == SYNTHETIC_LABEL


def test_deploy_gate_blocks_until_real_labels():
    synthetic = evaluate(make_synthetic_dataset(100, seed=1))
    assert deploy_gate(synthetic, 0)["allowed"] is False
    assert deploy_gate(synthetic, 500)["allowed"] is False
    real = [{"material": "PP", "contamination_pct": 0.5, "moisture_pct": 0.2, "grade": "A"}] * 40
    assert deploy_gate(evaluate(real), 40)["allowed"] is True
    wrong = [{**row, "grade": "C"} for row in real]
    assert deploy_gate(evaluate(wrong), 40)["allowed"] is False


def test_measured_grade_wins_and_mismatch_asks():
    registry = InMemoryRegistry()
    result = grade_stream(
        registry,
        "org_hanbit",
        stream(
            measure={"contaminationPct": 1.0, "moisturePct": 0.3, "evidence": ["measure:x:y:z@d"]},
            photo={"contaminationPct": 5.0, "evidence": ["model:det:v1:st_sprue"]},
        ),
        None,
        TODAY,
    )
    assert (result["grade"], result["basis"], result["label"]) == ("A", "measured", "측정 등급")
    assert result["photoEstimate"]["grade"] == "C"
    assert [c["key"] for c in result["checks"]] == ["grade_mismatch"]
    evidence = registry.get(result["evidence"][0], "org_hanbit")
    assert evidence.kind == "model" and evidence.value == "A"
    assert evidence.meta["inputEvidence"] == ["measure:x:y:z@d", "assumption:등급기준@v1"]
    assert registry.get("assumption:등급기준@v1", "org_other").kind == "assumption"
    with pytest.raises(PermissionError):
        registry.get(result["evidence"][0], "org_other")


def test_photo_estimate_needs_asked_moisture():
    registry = InMemoryRegistry()
    base = stream(photo={"contaminationPct": 2.0, "evidence": []})
    waiting = grade_stream(registry, "org_hanbit", base, None, TODAY)
    assert waiting["grade"] is None and waiting["label"] == "확인 필요"
    assert waiting["checks"][0]["key"] == "moisture_missing"
    answered = grade_stream(registry, "org_hanbit", base, None, TODAY, asked_moisture_pct=0.8)
    assert (answered["grade"], answered["basis"], answered["label"]) == (
        "B",
        "photo_estimate",
        "사진 추정",
    )
    assert answered["checks"] == []
    assert answered["evidence"] == ["model:rsai-grade:v1:st_sprue-photo_estimate"]


def test_no_inputs_asks_for_measurement():
    result = grade_stream(InMemoryRegistry(), "org_hanbit", stream(), None, TODAY)
    assert result["grade"] is None
    assert result["checks"][0]["key"] == "grade_inputs_missing"


def test_grade_stream_is_deterministic():
    args = (
        stream(measure={"contaminationPct": 2.5, "moisturePct": 0.9, "evidence": []}),
        None,
        TODAY,
    )
    one = grade_stream(InMemoryRegistry(), "org_hanbit", *args)
    two = grade_stream(InMemoryRegistry(), "org_hanbit", *args)
    assert one == two and one["grade"] == "B"


def test_endpoint_client_uses_transport():
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, json.loads(body)))
        return 200, {}, json.dumps(["B"]).encode()

    client = GradeModelClient("https://rsai-grade.example/score", "k", transport, version="3")
    result = client.predict("pp", 0.5, 0.2)
    assert (result["grade"], result["basis"], result["modelVersion"]) == ("B", "endpoint", "3")
    method, url, headers, body = calls[0]
    assert (method, url) == ("POST", "https://rsai-grade.example/score")
    assert headers["Authorization"] == "Bearer k"
    assert body["input_data"]["data"] == [["PP", 0.5, 0.2]]
    graded = grade_stream(
        InMemoryRegistry(),
        "org_hanbit",
        stream(measure={"contaminationPct": 0.5, "moisturePct": 0.2, "evidence": []}),
        client,
        TODAY,
    )
    assert graded["modelBasis"] == "endpoint"
    assert graded["evidence"] == ["model:rsai-grade:3:st_sprue-measured"]


def test_endpoint_failure_is_not_silently_replaced():
    bad_status = GradeModelClient("https://x.example", "k", lambda *a: (500, {}, b""))
    with pytest.raises(GradeModelError):
        bad_status.predict("PP", 1, 1)
    bad_grade = GradeModelClient("https://x.example", "k", lambda *a: (200, {}, b'["S"]'))
    with pytest.raises(GradeModelError):
        bad_grade.predict("PP", 1, 1)
    assert GradeModelClient().predict("PP", 1, 0.5)["basis"] == "threshold_rule"
