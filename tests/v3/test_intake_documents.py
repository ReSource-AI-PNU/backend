import base64
import json

import pytest

from app.v3.evidence.registry import InMemoryRegistry
from app.v3.intake.documents import (
    DocumentError,
    DocumentIntelligenceClient,
    confirm_fields,
    extract_contract,
    extract_material_spec,
    extract_msds,
    extract_permit,
    extract_purchase_criteria,
    layout_text,
)


def table(rows, confidence=None):
    cells = []
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            cell = {"rowIndex": r, "columnIndex": c, "content": text}
            if confidence and (r, c) in confidence:
                cell["confidence"] = confidence[(r, c)]
            cells.append(cell)
    return {"rowCount": len(rows), "columnCount": len(rows[0]), "cells": cells}


def kv(key, value, confidence=0.95):
    return {"key": {"content": key}, "value": {"content": value}, "confidence": confidence}


def test_client_submits_and_polls():
    calls = []
    answers = [
        (202, {"operation-location": "https://di.example/results/1"}, b""),
        (200, {}, json.dumps({"status": "running"}).encode()),
        (
            200,
            {},
            json.dumps({"status": "succeeded", "analyzeResult": {"content": "허가증"}}).encode(),
        ),
    ]

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        return answers[len(calls) - 1]

    client = DocumentIntelligenceClient("https://di.example/", "key", transport)
    waits = []
    layout = client.analyze(b"%PDF", sleep=waits.append)
    assert layout == {"content": "허가증"}
    assert waits == [1.0]
    method, url, headers, body = calls[0]
    assert method == "POST" and "/documentModels/prebuilt-layout:analyze?api-version=" in url
    assert headers["Ocp-Apim-Subscription-Key"] == "key"
    assert json.loads(body)["base64Source"] == base64.b64encode(b"%PDF").decode()
    assert calls[1][:2] == ("GET", "https://di.example/results/1")


def test_client_errors():
    with pytest.raises(DocumentError):
        DocumentIntelligenceClient().begin_analyze(b"x")
    rejected = DocumentIntelligenceClient("https://di.example", "k", lambda *a: (401, {}, b""))
    with pytest.raises(DocumentError):
        rejected.begin_analyze(b"x")
    answers = iter(
        [
            (202, {"Operation-Location": "https://di.example/r"}, b""),
            (
                200,
                {},
                json.dumps({"status": "failed", "error": {"message": "손상된 파일"}}).encode(),
            ),
        ]
    )
    failed = DocumentIntelligenceClient("https://di.example", "k", lambda *a: next(answers))
    with pytest.raises(DocumentError, match="손상된 파일"):
        failed.analyze(b"x", sleep=lambda s: None)


def test_extract_permit_from_table_and_key_value():
    layout = {
        "content": "폐기물처리업 허가증\n상호: (주)새길자원\n허가번호: 제2026-01호",
        "tables": [table([["폐기물 종류", "세부 재질"], ["폐합성수지", "PP, PE"], ["폐목재", ""]])],
        "keyValuePairs": [kv("허가 품목", "폐합성수지(PET), 기타 잔재물", 0.6)],
    }
    result = extract_permit(layout)
    assert result["businessName"] == "(주)새길자원"
    assert result["permitNumber"] == "제2026-01호"
    assert result["permits"][0] == {
        "wasteItem": "폐플라스틱",
        "materials": ["PP", "PE"],
        "raw": "폐합성수지 PP, PE",
        "needsReview": False,
        "reviewReasons": [],
    }
    assert result["permits"][1]["wasteItem"] == "폐목재"
    low = result["permits"][2]
    assert low["materials"] == ["PET"] and low["needsReview"] is True
    assert len(result["permits"]) == 3
    assert all(f["confirmed"] is False for f in result["fields"])


def test_extract_purchase_criteria():
    layout = {
        "tables": [
            table(
                [
                    [
                        "재질",
                        "등급",
                        "형태",
                        "오염도 상한(%)",
                        "수분(%)",
                        "매입 단가(원/kg)",
                        "최소 물량(kg/월)",
                    ],
                    ["PP", "A", "분쇄", "2", "1", "320", "500"],
                    ["PP", "B등급", "분쇄", "5% 이하", "3", "180,000원/톤", "1,000"],
                    ["LDPE 필름", "S", "필름", "10", "5", "협의", ""],
                ],
                confidence={(1, 5): 0.99, (2, 3): 0.5},
            )
        ]
    }
    result = extract_purchase_criteria(layout)
    first, second, third = result["criteria"]
    assert first == {
        "material": "PP",
        "grade": "A",
        "form": "분쇄",
        "maxContaminationPct": 2.0,
        "maxMoisturePct": 1.0,
        "minMonthlyKg": 500.0,
        "unitPriceKrwPerKg": 320.0,
        "id": "pc_pp_a_1",
        "raw": "PP | A | 분쇄 | 2 | 1 | 320 | 500",
        "needsReview": False,
        "reviewReasons": [],
    }
    assert second["grade"] == "B" and second["unitPriceKrwPerKg"] == 180.0
    assert second["maxContaminationPct"] == 5.0 and second["minMonthlyKg"] == 1000.0
    assert second["needsReview"] is True
    assert third["material"] == "LDPE" and third["grade"] is None
    assert third["unitPriceKrwPerKg"] is None and third["needsReview"] is True
    price = next(f for f in result["fields"] if f["field"] == "pc_pp_a_1-단가")
    assert (price["value"], price["unit"], price["needsReview"]) == (320.0, "원/kg", False)


def test_processing_fee_is_negative():
    layout = {"tables": [table([["품목", "처리비(원/kg)"], ["PP+ABS 혼합", "80"]])]}
    entry = extract_purchase_criteria(layout)["criteria"][0]
    assert entry["unitPriceKrwPerKg"] == -80.0
    assert entry["needsReview"] is True  # 한 칸에 재질이 둘


def test_extract_emitter_documents():
    spec = extract_material_spec(
        {
            "tables": [
                table(
                    [
                        ["품명", "재질", "공급사"],
                        ["HJ-500", "PP (호모)", "가상화학"],
                        ["포장 필름", "", ""],
                    ]
                )
            ]
        }
    )
    assert spec["materials"][0]["material"] == "PP"
    assert spec["materials"][0]["supplier"] == "가상화학"
    assert spec["materials"][1]["needsReview"] is True

    msds = extract_msds(
        {
            "content": "제품명: HJ-500 폴리프로필렌",
            "tables": [
                table(
                    [
                        ["화학물질명", "CAS No.", "함유량(%)"],
                        ["Polypropylene (PP)", "9003-07-0", "98~100"],
                    ]
                )
            ],
        }
    )
    assert msds["productName"] == "HJ-500 폴리프로필렌"
    assert msds["components"][0]["casNo"] == "9003-07-0"
    assert msds["components"][0]["contentPct"] == 98.0
    assert msds["components"][0]["needsReview"] is True
    assert msds["resinCandidates"] == ["PP"]

    contract = extract_contract(
        {
            "content": "폐기물 위탁처리 계약서\n수탁자: (주)가상환경\n처리 방법: 혼합 소각\n단가: 250원/kg (부가세 별도)",
        }
    )
    assert contract["method"] == "혼합 소각"
    assert contract["unitCostKrwPerKg"] == 250.0
    assert contract["vendorName"] == "(주)가상환경"
    per_ton = extract_contract(
        {"content": "본 폐기물은 매립 처리하며 처리비는 120,000원/톤으로 한다."}
    )
    assert per_ton["method"] == "매립" and per_ton["unitCostKrwPerKg"] == 120.0
    assert all(f["needsReview"] for f in per_ton["fields"])
    empty = extract_contract({"content": "내용 없음"})
    assert empty["method"] is None and empty["unitCostKrwPerKg"] is None


def test_layout_text_falls_back_to_paragraphs():
    assert layout_text({"paragraphs": [{"content": "가"}, {"content": "나"}]}) == "가\n나"


def test_confirm_fields_registers_only_confirmed_values():
    registry = InMemoryRegistry()
    fields = extract_purchase_criteria(
        {
            "tables": [
                table([["재질", "등급", "단가(원/kg)", "최소 물량"], ["PP", "A", "320", "500"]])
            ]
        }
    )["fields"]
    assert (
        confirm_fields(registry, "org_saegil", "매입기준표", fields, "김담당", today="2026-11-17")
        == []
    )
    assert registry.list() == []
    fields[0]["confirmed"] = True
    fields[1]["confirmed"] = True
    fields[1]["value"] = None  # 값이 없는 항목은 확인해도 등록하지 않는다
    ids_ = confirm_fields(
        registry, "org_saegil", "매입기준표", fields, "김담당", today="2026-11-17"
    )
    assert ids_ == ["doc:org_saegil:매입기준표:pc_pp_a_1-단가"]
    evidence = registry.get(ids_[0], "org_saegil")
    assert (evidence.kind, evidence.value, evidence.unit, evidence.as_of) == (
        "doc",
        320.0,
        "원/kg",
        "2026-11-17",
    )
    assert "김담당 확인" in evidence.source
    with pytest.raises(PermissionError):
        registry.get(ids_[0], "org_hanbit")
