"""업체가 올린 허가증·매입 기준표를 확인하면 업체 프로필이 채워지고 업체 탐색에 나온다(메모리 저장소).

브라우저 QA에서 본 문제: 확인한 값이 doc 근거로만 등록되고 프로필의 허가 품목·매입 기준은 비어 있어,
새로 가입한 업체가 어떤 진단에도 나오지 않았다. 허가 품목은 표와 키-값에서 두 번씩 읽혀 겹쳐 보였다.
회사와 수치는 모두 가상이다.
"""

from __future__ import annotations

from app.v3 import service
from app.v3.agents.orchestrator import discoverable_profiles
from app.v3.intake.documents import extract_permit
from tests.v3.runtime_fixtures import make_app

VENDOR, EMITTER = "org_qa_vendor", "org_qa_emitter"


def _table(rows):
    cells = [
        {"rowIndex": r, "columnIndex": c, "content": text, "confidence": 0.98}
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
    ]
    return {"rowCount": len(rows), "columnCount": len(rows[0]), "cells": cells}


def _kv(key, value):
    return {"key": {"content": key}, "value": {"content": value}, "confidence": 0.95}


# QA가 올린 PDF를 Document Intelligence가 읽은 모양. 키-값 값이 다음 줄("처리 방법: …")까지 이어 읽혔다
PERMIT = {
    "content": "폐기물 처리업 허가증\n상호: (주)QA자원\n허가 번호: 제2026-QA-001호\n"
    "허가 품목: 폐합성수지(PP, PE, LDPE), 폐포장재\n처리 방법: 재활용(물질 재활용)\n"
    "허가일: 2026-01-15\n부산광역시장",
    "keyValuePairs": [
        _kv("상호", "(주)QA자원"),
        _kv("허가 품목", "폐합성수지(PP, PE, LDPE), 폐포장재 처리 방법: 재활용(물질 재활용)"),
    ],
}
PRICE_TABLE = {
    "content": "(주)QA자원 매입 기준표\n기준일 2026-11-01\n1회 계약 최소 물량 월 300kg. 수거 요일 화, 목.",
    "tables": [
        _table(
            [
                ["재질", "형태", "등급", "매입 단가(원/kg)", "오염도 상한", "수분 상한"],
                ["PP", "가공 전", "A", "300", "2%", "1%"],
                ["PP", "가공 전", "B", "200", "5%", "2%"],
                ["LDPE", "필름", "B", "120", "5%", "3%"],
            ]
        )
    ],
}


class FakeDocumentIntelligence:
    configured = True

    def __init__(self, layouts):
        self.layouts = layouts

    def analyze(self, data):
        return self.layouts[data]


def _world():
    app = make_app(
        documents=FakeDocumentIntelligence({b"%PDF-permit": PERMIT, b"%PDF-price": PRICE_TABLE})
    )
    app.store.put_org({"id": VENDOR, "name": "(주)QA자원", "role": "vendor", "demo": False})
    app.store.put_vendor_profile(
        {"orgId": VENDOR, "permits": [], "criteria": [], "fleet": {}, "guardrails": {}}
    )
    app.store.put_org(
        {
            "id": EMITTER,
            "name": "(주)QA정밀",
            "role": "emitter",
            "demo": False,
            "location": [128.85, 35.09],
        }
    )
    return app


def _upload_and_confirm(app, kind, filename, data):
    result = service.upload_document(app, VENDOR, kind, filename, data, VENDOR)
    fields = [{**f, "confirmed": True} for f in result["fields"]]
    return result, service.confirm_document(app, VENDOR, filename, fields, VENDOR)


def test_permit_items_are_not_listed_twice():
    fields = extract_permit(PERMIT)["fields"]
    assert [f["value"] for f in fields] == ["폐플라스틱 (PP, PE, LDPE)", "포장재"]
    # 다음 항목("처리 방법: …")은 문서 표기에 남지 않는다
    assert all("처리 방법" not in f["raw"] for f in fields)
    assert fields[0]["target"] == {
        "section": "permit",
        "wasteItem": "폐플라스틱",
        "materials": ["PP", "PE", "LDPE"],
    }


def test_confirmed_documents_fill_the_vendor_profile_once():
    app = _world()
    assert VENDOR not in discoverable_profiles(app.store, app.store.get_org(EMITTER))
    _, permit = _upload_and_confirm(app, "permit", "permit.pdf", b"%PDF-permit")
    assert permit["profile"]["permitsAdded"] == 2
    upload, price = _upload_and_confirm(app, "purchase_criteria", "price-table.pdf", b"%PDF-price")
    # 표 밖의 최소 물량도 뽑아 모든 행에 쓴다
    assert any(f["field"] == "criteria-minMonthlyKg" for f in upload["fields"])
    assert price["profile"]["criteriaAdded"] == 3

    profile = service.get_vendor(app, VENDOR, VENDOR)  # 다시 읽어도(새로 고침) 남아 있다
    permits = {p["wasteItem"]: p for p in profile["permits"]}
    assert permits["폐플라스틱"]["materials"] == ["PP", "PE", "LDPE"]
    assert permits["포장재"]["materials"] == []
    assert permits["폐플라스틱"]["evidence"] == ["doc:org_qa_vendor:permit.pdf:permit-1"]
    criteria = {(c["material"], c["grade"], c["form"]): c for c in profile["criteria"]}
    pp_a = criteria[("PP", "A", "가공 전")]
    assert pp_a["unitPriceKrwPerKg"] == 300 and pp_a["maxContaminationPct"] == 2
    assert pp_a["maxMoisturePct"] == 1 and pp_a["minMonthlyKg"] == 300
    assert criteria[("LDPE", "B", "필름")]["unitPriceKrwPerKg"] == 120
    # 기준의 값마다 확인한 doc 근거가 붙는다
    for evidence_id in pp_a["evidence"]:
        found = app.registry.get(evidence_id, VENDOR)
        assert found.kind == "doc" and found.meta["confirmedBy"] == VENDOR
    values = sorted(app.registry.get(e, VENDOR).value for e in pp_a["evidence"])
    assert values == [1, 2, 300, 300]  # 수분·오염도 상한, 단가, 최소 물량

    # 같은 문서를 다시 확인해도 품목과 기준이 겹쳐 생기지 않는다
    _, again = _upload_and_confirm(app, "permit", "permit.pdf", b"%PDF-permit")
    _, again_price = _upload_and_confirm(app, "purchase_criteria", "price-table.pdf", b"%PDF-price")
    assert again["profile"]["permitsAdded"] == again["profile"]["permitsUpdated"] == 0
    assert again_price["profile"]["criteriaAdded"] == again_price["profile"]["criteriaUpdated"] == 0
    profile = service.get_vendor(app, VENDOR, VENDOR)
    assert len(profile["permits"]) == 2 and len(profile["criteria"]) == 3


def test_unconfirmed_values_do_not_change_the_profile():
    app = _world()
    result = service.upload_document(app, VENDOR, "permit", "permit.pdf", b"%PDF-permit", VENDOR)
    done = service.confirm_document(app, VENDOR, "permit.pdf", result["fields"], VENDOR)
    assert done["evidence"] == [] and done["profile"] is None
    assert service.get_vendor(app, VENDOR, VENDOR)["permits"] == []
    # 단가를 확인하지 않은 새 매입 기준은 만들지 않는다
    price = service.upload_document(
        app, VENDOR, "purchase_criteria", "price.pdf", b"%PDF-price", VENDOR
    )
    limits_only = [
        {**f, "confirmed": f["field"].endswith("maxContaminationPct")} for f in price["fields"]
    ]
    done = service.confirm_document(app, VENDOR, "price.pdf", limits_only, VENDOR)
    assert done["profile"]["criteriaAdded"] == 0 and len(done["profile"]["skipped"]) == 3
    assert service.get_vendor(app, VENDOR, VENDOR)["criteria"] == []


def test_a_real_emitter_finds_the_vendor_after_confirmation():
    app = _world()
    _upload_and_confirm(app, "permit", "permit.pdf", b"%PDF-permit")
    _upload_and_confirm(app, "purchase_criteria", "price-table.pdf", b"%PDF-price")
    assert VENDOR in discoverable_profiles(app.store, app.store.get_org(EMITTER))
    service.update_stream(
        app,
        EMITTER,
        "st_qa_pp",
        {
            "name": "PP 사출 불량품",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "가공 전",
            "monthlyKg": 400,
        },
        EMITTER,
    )
    view = service.create_session(app, EMITTER, EMITTER)
    session_id = view["session"]["id"]
    view = service.run_session(app, session_id, EMITTER)
    for _ in range(3):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                service.answer_question(app, session_id, question["id"], "unknown", EMITTER)
        view = service.run_session(app, session_id, EMITTER)
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == VENDOR)
    assert thread["status"] != "not_opened" and thread["messages"]
    # 업체도 그 문의를 받은 대화를 본다
    vendor_view = service.get_view(app, session_id, VENDOR)
    assert [t["vendorOrgId"] for t in vendor_view["threads"]] == [VENDOR]
