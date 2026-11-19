"""허가 품목 일치와 허가증 원문 대조를 가른다(2026-11-19 평가 대응: 자원순환 판단의 신뢰성 10/20).

같은 PP 러너 1,000kg/월과 같은 업체((주)새길자원이 가장 좋은 단가라 배정된다)를 두고 허가 근거만 바꾼다.
A 업체가 직접 입력한 PP 품목만 → 문의 후보로 남고 결과표·승인 카드 모두 "허가증 원본 확인 필요", 확정 보류
B 허가증 업로드·추출만 → "검토 대기"(승인 대기 중에 올려도 결과표가 바뀐다), 담당자가 원문을 대조하면 "대조 완료"
C 원문의 품목은 맞지만 처리 방식이 다름 → 그 경로 제외, 사유 표시, 손익이 진행 가능한 합계에서 빠짐
D 다른 업체에는 대조 기록이 있지만 배정 업체는 직접 입력만 → 배정 업체는 여전히 "원본 확인 필요"
E 다른 업체·사업장의 허가증, 다른 조직의 doc 근거로는 확인 상태를 채우지 않는다
회사와 수치는 모두 가상이다.
"""

import pytest

from app.v3 import service
from app.v3.evidence.model import Evidence
from app.v3.judge.permits import permit_level
from tests.v3.judge_fixtures import registry, stream, vendor
from tests.v3.runtime_fixtures import HANBIT, MISO, NAMHAE, SAEGIL, make_app, to_approval

PERMIT_TEXT = (
    "폐기물처리업 허가증\n"
    "상호: {name}\n"
    "소재지: {address}\n"
    "업종: {business}\n"
    "영업대상 폐기물: 폐플라스틱(PP, PE)\n"
)


def _runner_world():
    """단일 PP 러너 1,000kg/월. 세 업체 모두 허가 품목을 직접 입력만 한 상태에서 시작한다."""
    app = make_app()
    for sid in [s["id"] for s in app.store.list_streams(HANBIT)]:
        if sid != "st_sprue":
            app.store._data["streams"].pop(sid)
    runner = app.store.get_stream("st_sprue")
    runner.update(monthlyKg=1000, name="PP 러너")
    app.store.put_stream(runner)
    for org in (SAEGIL, NAMHAE, MISO):
        profile = app.store.get_vendor_profile(org)
        profile.pop("permitDocuments", None)
        profile.pop("permitChecks", None)
        app.store.put_vendor_profile(profile)
        service.update_vendor(app, org, {"permits": []}, org)
        service.update_vendor(
            app, org, {"permits": [{"wasteItem": "폐플라스틱", "materials": ["PP"]}]}, org
        )
    return app


def _upload(app, org, *, name, address, business="폐기물 종합재활용업", filename="허가증.txt"):
    text = PERMIT_TEXT.format(name=name, address=address, business=business)
    return service.upload_document(app, org, "permit", filename, text.encode("utf-8"), org)


def _row(view, sid="st_sprue"):
    return next(a for a in view["result"]["assignments"] if a["streamId"] == sid)


def _approval(view, vendor_id):
    thread = next(t["id"] for t in view["threads"] if t["vendorOrgId"] == vendor_id)
    return next(a for a in view["approvals"] if a["threadId"] == thread)


def _check(app, view, vendor_id, *, found=True, fits=True, document_id=None, page=1):
    profile = app.store.get_vendor_profile(vendor_id)
    document_id = document_id or profile["permitDocuments"][-1]["id"]
    return service.record_permit_check(
        app,
        view["session"]["id"],
        {
            "streamId": "st_sprue",
            "vendorOrgId": vendor_id,
            "documentId": document_id,
            "page": page,
            "itemFound": found,
            "processFits": fits,
            "itemQuote": "영업대상 폐기물: 폐플라스틱(PP, PE)",
            "processQuote": "업종: 폐기물 종합재활용업",
        },
        HANBIT,
        "김담당",
    )


def _approve_both(app, view, vendor_id):
    sid = view["session"]["id"]
    thread = next(t["id"] for t in view["threads"] if t["vendorOrgId"] == vendor_id)
    service.approve(app, sid, thread, vendor_id)
    service.approve(app, sid, thread, HANBIT)
    return service.get_view(app, sid, HANBIT)


# ── A. 직접 입력만 ──────────────────────────────────────────────────────


def test_a_typed_permit_stays_candidate_but_cannot_be_confirmed():
    app = _runner_world()
    view = to_approval(app)
    row = _row(view)
    assert row["vendorOrgId"] == SAEGIL  # 문의 후보로 남아 배정까지 간다
    assert row["permit"]["level"] == "registered"
    assert row["permit"]["label"] == "등록 품목 일치 · 허가증 원본 확인 필요"
    assert row["permit"]["status"] == "conditional" and row["status"] == "conditional"
    assert row["permit"]["todo"]["items"]  # 제출받을 문서와 확인할 항목
    approval = _approval(view, SAEGIL)
    assert approval["permitHold"] is True
    assert [p["label"] for p in approval["permits"]] == [row["permit"]["label"]]
    # 법령 요건도 배정 업체 하나로 같은 상태를 낸다
    section = next(r for r in view["result"]["regulation"] if r["streamId"] == "st_sprue")
    holder = next(i for i in section["items"] if i["key"] == "permit_holder")
    assert holder["status"] == "conditional" and "원본 확인 필요" in holder["note"]
    # 업체는 먼저 승인할 수 있지만 배출 기업의 승인(확정)은 서버가 막는다
    sid = view["session"]["id"]
    thread = approval["threadId"]
    service.approve(app, sid, thread, SAEGIL)
    with pytest.raises(service.ServiceError) as error:
        service.approve(app, sid, thread, HANBIT)
    assert error.value.code == "PERMIT_UNVERIFIED"
    assert "허가증 원본 확인 필요" in error.value.message
    after = service.get_view(app, sid, HANBIT)
    assert after["session"]["status"] == "awaiting_approval"
    assert next(t for t in after["threads"] if t["id"] == thread)["status"] == "agreed"
    assert _approval(after, SAEGIL)["permitHold"] is True


def test_a_registry_name_collision_does_not_verify_typed_permit():
    """재현된 결함: 가상 업체의 상호가 공공 명부의 실제 상호와 같고 차고지가 없으면 met이 됐다."""
    reg = registry()
    typed = vendor(
        name="(주)그린자원",
        fleet={},
        permits=[
            {"wasteItem": "폐플라스틱", "materials": ["PP"], "evidence": ["human:x:허가품목"]}
        ],
    )
    level = permit_level(reg, stream(), typed)
    assert level["level"] == "registered" and level["source"] == "typed"
    # 차고지가 명부 업체 근처여도 명부는 후보 자료일 뿐이다(원본 확인 필요)
    near = {**typed, "fleet": {"depot": [128.57, 35.22]}}
    level = permit_level(reg, stream(), near)
    assert level["status"] != "met"


# ── B. 업로드·추출만 → 검토 대기 → 원문 대조 완료 ───────────────────────


def test_b_upload_is_pending_review_until_staff_compares_original():
    app = _runner_world()
    view = to_approval(app)
    assert _row(view)["permit"]["level"] == "registered"
    # 승인 대기 중에 업체가 허가증을 올렸다(AI가 읽음, 아무도 대조하지 않음)
    uploaded = _upload(app, SAEGIL, name="(주)새길자원", address="부산광역시 강서구 미음동")
    document = uploaded["permitDocument"]
    assert document["items"][0]["wasteItem"] == "폐플라스틱" and document["items"][0]["page"] == 1
    assert document["holder"]["name"] == "(주)새길자원"
    view = service.get_view(app, view["session"]["id"], HANBIT)
    row = _row(view)
    assert (
        row["permit"]["level"] == "pending_review" and row["permit"]["label"] == "허가증 검토 대기"
    )
    assert row["permit"]["document"]["id"] == document["id"]
    approval = _approval(view, SAEGIL)
    assert approval["permitHold"] is True and approval["permits"][0]["level"] == "pending_review"
    with pytest.raises(service.ServiceError) as error:
        service.approve(app, view["session"]["id"], approval["threadId"], HANBIT)
    assert error.value.code == "PERMIT_UNVERIFIED" and "허가증 검토 대기" in error.value.message
    # 업체가 추출값을 확인해도(예전 확인 흐름) 원문 대조가 아니다
    fields = [{**f, "confirmed": True} for f in uploaded["fields"]]
    service.confirm_document(app, SAEGIL, "허가증.txt", fields, SAEGIL)
    view = service.get_view(app, view["session"]["id"], HANBIT)
    assert _row(view)["permit"]["level"] == "pending_review"

    # 배출 기업 담당자가 원문 1쪽에서 품목과 처리 방법을 대조했다
    view = _check(app, view, SAEGIL)
    row = _row(view)
    permit = row["permit"]
    assert permit["level"] == "verified" and permit["label"] == "허가증 대조 완료"
    assert permit["status"] == "met" and permit["todo"] is None
    check = permit["check"]
    assert check["page"] == 1 and check["document"] == "허가증.txt"
    assert check["wasteItem"] == "폐플라스틱" and check["materials"] == ["PP"]
    assert check["process"] == "재활용"
    assert check["checkedBy"] == {"orgId": HANBIT, "orgName": "(주)한빛정밀", "userName": "김담당"}
    assert check["checkedAt"]
    # 대조 범위는 확인한 품목과 처리 방식으로 한정한다
    assert "이 폐기물과 처리 방식에만 해당합니다" in permit["text"]
    # 근거가 열린다: 대조 기록(사람 확인)과 AI가 읽은 허가증 표기(쪽 포함)
    opened = service.get_evidence(app, check["evidence"], HANBIT)["evidence"]
    assert opened["kind"] == "human" and "1쪽 원문 대조" in opened["excerpt"]
    assert "김담당" in opened["source"]
    read = service.get_evidence(app, document["evidence"][0], HANBIT)["evidence"]
    assert "1쪽" in read["excerpt"] and "담당자 대조 전" in read["source"]
    approval = _approval(view, SAEGIL)
    assert approval["permitHold"] is False and approval["permits"][0]["level"] == "verified"
    section = next(r for r in view["result"]["regulation"] if r["streamId"] == "st_sprue")
    holder = next(i for i in section["items"] if i["key"] == "permit_holder")
    assert holder["status"] == "met" and holder["permit"]["level"] == "verified"
    # 업체 뷰에는 담당자 이름과 대조 근거 ID가 실리지 않는다(문장, 진행 기록에도 이름이 없다)
    seen = service.get_view(app, view["session"]["id"], SAEGIL)
    assert "김담당" not in str(seen)
    mine = next(a for a in seen["result"]["assignments"] if a["streamId"] == "st_sprue")
    assert mine["permit"]["level"] == "verified"
    assert mine["permit"]["check"]["checkedBy"]["userName"] is None
    assert check["evidence"] not in mine["permit"]["evidence"]
    with pytest.raises(service.ServiceError):
        service.get_evidence(app, check["evidence"], SAEGIL)
    done = _approve_both(app, view, SAEGIL)
    assert done["session"]["status"] == "confirmed"


# ── C. 처리 방식 불일치 → 경로 제외 ──────────────────────────────────


def test_c_process_mismatch_excludes_route_and_its_money():
    app = _runner_world()
    view = to_approval(app)
    before = _row(view)
    assert before["vendorOrgId"] == SAEGIL
    _upload(app, SAEGIL, name="(주)새길자원", address="부산광역시 강서구 미음동")
    view = service.get_view(app, view["session"]["id"], HANBIT)
    view = _check(app, view, SAEGIL, found=True, fits=False)
    row = _row(view)
    assert row["vendorOrgId"] != SAEGIL  # 다른 적합한 후보(또는 지금 방식)로 바뀐다
    blocked = {b["vendorOrgId"]: b for b in row["blockedRoutes"]}
    assert blocked[SAEGIL]["permit"]["label"] == "이 경로로 진행 불가"
    assert (
        "처리 방식 재활용(재활용업)이 허가증의 업종·처리 방법과 다릅니다" in blocked[SAEGIL]["note"]
    )
    # 진행 불가 경로는 대안(진행 가능한 선택지)에도 없고, 손익이 진행 가능한 합계에 남지 않는다
    assert SAEGIL not in {a["vendorOrgId"] for a in row["alternatives"]}
    assert "monthlyAfter" not in blocked[SAEGIL]
    claims = {c["id"]: c for c in view["result"].get("claims") or []}
    total = view["result"]["totals"]["difference"]
    total_value = (
        (total or {}).get("value")
        if isinstance(total, dict)
        else claims.get(total, {}).get("value")
    )
    rows = [a for a in view["result"]["assignments"] if a.get("difference")]
    assert total_value == pytest.approx(sum(a["difference"]["value"] for a in rows))
    assert before["difference"]["value"] != pytest.approx(row["difference"]["value"])
    # 새길자원과의 대화에서 이 폐기물이 빠졌으니 그 대화로 확정할 수 없다
    thread = next(t for t in view["threads"] if t["vendorOrgId"] == SAEGIL)
    assert thread["status"] in ("declined", "open") or "st_sprue" not in (
        _approval(view, SAEGIL)["streamIds"] if thread["status"] == "agreed" else []
    )


# ── D. 다른 업체의 대조 기록으로 배정 업체를 채우지 않는다 ───────────────


def test_d_other_vendor_check_does_not_fill_assigned_vendor():
    app = _runner_world()
    view = to_approval(app)
    assert _row(view)["vendorOrgId"] == SAEGIL
    _upload(app, MISO, name="(주)미소환경", address="부산광역시 사상구 학장동")
    view = service.get_view(app, view["session"]["id"], HANBIT)
    view = _check(app, view, MISO)
    row = _row(view)
    assert row["vendorOrgId"] == SAEGIL
    assert row["permit"]["level"] == "registered"  # 배정 업체는 여전히 원본 확인 필요
    miso = next(a for a in row["alternatives"] if a["vendorOrgId"] == MISO)
    assert miso["permit"]["level"] == "verified"
    section = next(r for r in view["result"]["regulation"] if r["streamId"] == "st_sprue")
    holder = next(i for i in section["items"] if i["key"] == "permit_holder")
    assert holder["status"] == "conditional" and holder["note"].startswith("(주)새길자원:")
    assert _approval(view, SAEGIL)["permitHold"] is True


def test_d_check_record_is_scoped_to_vendor_item_and_process():
    """대조 기록은 그 업체 · 그 품목·재질 · 그 처리 방식에만 쓴다."""
    reg = registry()
    document = {
        "id": "pd_1",
        "name": "허가증.pdf",
        "holder": {"name": "(주)새길자원"},
        "items": [],
        "evidence": [],
    }
    record = {
        "id": "pc_1",
        "vendorOrgId": "org_saegil",
        "documentId": "pd_1",
        "wasteItem": "폐플라스틱",
        "materials": ["PP"],
        "process": "재활용",
        "itemFound": True,
        "processFits": True,
        "checkedBy": {"orgId": "org_hanbit", "orgName": "(주)한빛정밀"},
        "checkedAt": "2026-11-19T00:00:00+00:00",
    }
    base = vendor(orgId="org_saegil", name="(주)새길자원", permitDocuments=[document])
    assert permit_level(reg, stream(), {**base, "permitChecks": [record]})["level"] == "verified"
    # 다른 재질, 다른 처리 방식, 다른 업체의 기록은 쓰지 않는다
    pe = {**record, "materials": ["PE"]}
    assert permit_level(reg, stream(), {**base, "permitChecks": [pe]})["level"] != "verified"
    burn = {**base, "process": "열분해", "permitChecks": [record]}
    assert permit_level(reg, stream(), burn)["level"] != "verified"
    other = {**record, "vendorOrgId": "org_miso"}
    assert permit_level(reg, stream(), {**base, "permitChecks": [other]})["level"] != "verified"


# ── E. 다른 업체·사업장의 허가증, 다른 조직의 근거 ─────────────────────


def test_e_other_company_permit_is_ignored():
    app = _runner_world()
    view = to_approval(app)
    uploaded = _upload(
        app,
        SAEGIL,
        name="(주)가상다른자원",
        address="부산광역시 사하구 신평동",
        filename="남의허가증.txt",
    )
    fields = [{**f, "confirmed": True} for f in uploaded["fields"]]
    service.confirm_document(app, SAEGIL, "남의허가증.txt", fields, SAEGIL)
    view = service.get_view(app, view["session"]["id"], HANBIT)
    permit = _row(view)["permit"]
    assert permit["level"] == "registered" and permit["status"] == "conditional"
    assert permit["ignored"] and "다른 업체의 허가증" in permit["ignored"][0]["reason"]
    with pytest.raises(service.ServiceError) as error:
        _check(app, view, SAEGIL, document_id=uploaded["permitDocument"]["id"])
    assert error.value.code == "PERMIT_DOCUMENT_IGNORED"


def test_e_other_org_doc_evidence_does_not_count():
    reg = registry()
    reg.put(
        Evidence(
            id="doc:org_other:허가증:품목",
            kind="doc",
            excerpt="영업대상 폐기물: 폐플라스틱(PP)",
            source="다른 업체의 허가증",
            as_of="2026-11-10",
            access=("org_other",),
        )
    )
    mixed = vendor(
        orgId="org_saegil",
        name="(주)새길자원",
        permits=[
            {
                "wasteItem": "폐플라스틱",
                "materials": ["PP"],
                "evidence": ["doc:org_other:허가증:품목"],
            }
        ],
    )
    level = permit_level(reg, stream(), mixed)
    assert level["level"] == "registered" and "doc:org_other:허가증:품목" not in level["evidence"]


def test_check_api_errors():
    app = _runner_world()
    view = to_approval(app)
    sid = view["session"]["id"]
    with pytest.raises(service.ServiceError) as error:
        _check(app, view, SAEGIL, document_id="pd_none")
    assert error.value.code == "PERMIT_DOCUMENT_NOT_FOUND"
    _upload(app, SAEGIL, name="(주)새길자원", address="부산광역시 강서구 미음동")
    with pytest.raises(service.ServiceError) as error:
        _check(app, view, SAEGIL, page=3)
    assert error.value.code == "INVALID_PAGE"
    with pytest.raises(service.ServiceError) as error:
        service.record_permit_check(
            app, sid, {"streamId": "st_sprue", "vendorOrgId": SAEGIL}, SAEGIL
        )
    assert error.value.code in ("PERMIT_CHECK_FORBIDDEN", "SESSION_NOT_FOUND")
