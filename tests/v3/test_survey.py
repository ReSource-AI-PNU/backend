"""가상 입력으로 설문 요구를 검증한다. 모델·메일·외부 서비스는 호출하지 않는다."""

import io

import pytest
from PIL import Image

from app.v3 import service, survey
from app.v3.a2a.message import Message
from app.v3.evidence.validator import validate_message
from app.v3.judge.route import check_loading
from tests.v3.runtime_fixtures import DAON, HANBIT, MISO, SAEGIL, make_app, to_approval


def ranged(app):
    profile = app.store.get_vendor_profile(SAEGIL)
    for c in profile["criteria"]:
        if c["id"] == "pc_saegil_pp_a":
            c.update(priceLow=300, priceHigh=340, unitPriceKrwPerKg=None)
    service.update_vendor(
        app,
        SAEGIL,
        {"criteria": profile["criteria"], "collectionPolicy": "distance_vehicle"},
        SAEGIL,
    )
    return survey.start_quote(
        app,
        HANBIT,
        {"streamId": "st_sprue", "vendorOrgId": SAEGIL, "criterionId": "pc_saegil_pp_a"},
    )


def narrow(app, q, low=320, high=330, facts=None):
    return survey.change_quote(
        app,
        SAEGIL,
        q["id"],
        {
            "revision": q["revision"],
            "low": low,
            "high": high,
            "confirmed": facts or ["material"],
            "reason": "담당자가 원자재 자료와 수거 조건을 대조했습니다.",
        },
    )


def test_range_is_not_converted_into_a_point_until_review_and_emitter_acceptance():
    app = make_app(negotiate=True)
    q = ranged(app)
    assert q["low"] == 300 and q["high"] == 340
    assert app.store.get_vendor_profile(SAEGIL)["criteria"][0]["unitPriceKrwPerKg"] is None
    with pytest.raises(service.ServiceError):
        narrow(app, q, 320, 320)
    q = narrow(app, q)
    assert q["history"][0]["low"] == 300
    q = narrow(app, q, 325, 325, list(survey.FACTS))
    assert q["status"] == "offered"
    q = survey.change_quote(app, HANBIT, q["id"], {"revision": q["revision"]}, accept=True)
    assert q["status"] == "accepted"
    result = to_approval(app)
    sprue = next(a for a in result["result"]["assignments"] if a["streamId"] == "st_sprue")
    assert sprue["unitPrice"]["value"] == 325
    assert result["session"]["status"] == "awaiting_approval"
    for t in result["threads"]:
        for m in t["messages"]:
            assert validate_message(
                Message.from_dict({**m, "threadId": t["id"]}), app.registry, today=app.today
            ).ok


def test_narrowing_rejects_widening_wrong_org_stale_revision_and_changed_facts():
    app = make_app()
    q = ranged(app)
    with pytest.raises(service.ServiceError):
        narrow(app, q, 299, 330)
    with pytest.raises(service.ServiceError):
        survey.change_quote(app, MISO, q["id"], {"revision": 1})
    q = narrow(app, q)
    with pytest.raises(service.ServiceError) as error:
        narrow(app, {**q, "revision": 1})
    assert error.value.status == 409
    service.update_stream(app, HANBIT, "st_sprue", {"monthlyKg": 900}, HANBIT)
    with pytest.raises(service.ServiceError):
        narrow(app, q)
    assert survey.view(app, DAON)["quotes"] == []


def test_distance_vehicle_policy_does_not_invent_a_minimum_but_still_checks_capacity():
    app = make_app()
    p = app.store.get_vendor_profile(SAEGIL)
    for c in p["criteria"]:
        c.pop("minMonthlyKg", None)
    p["collectionPolicy"] = "distance_vehicle"
    s = app.store.get_stream("st_sprue")
    s["monthlyKg"] = 100
    r = check_loading(app.registry, org_id=HANBIT, today=app.today, streams=[s], vendor=p)
    assert next(c for c in r["claims"] if c["id"] == "minMonthlyKg")["value"] == 0
    assert next(c for c in r["checks"] if c["key"] == "min_volume")["status"] == "met"
    s["monthlyKg"] = 1_000_000
    r = check_loading(app.registry, org_id=HANBIT, today=app.today, streams=[s], vendor=p)
    assert next(c for c in r["checks"] if c["key"] == "loading")["status"] != "met"


def test_weighing_import_is_month_scoped_idempotent_and_does_not_invent_quality():
    app = make_app()
    data = "계량번호,폐기물 이름,계량일,중량(kg)\nA,스프루·런너,2026-10-01,200\nB,스프루·런너,2026-11-01,300\nC,스프루·런너,2026-11-01,300\n".encode()
    before = app.store.get_stream("st_sprue")["measure"]
    result = survey.import_weights(app, HANBIT, "test.csv", data, "2026-11")
    assert result["totals"] == {"st_sprue": 600}
    assert result["accepted"] == 2
    again = survey.import_weights(app, HANBIT, "renamed.csv", data, "2026-11")
    assert again["accepted"] == 0 and again["duplicates"] == 2
    s = app.store.get_stream("st_sprue")
    assert s["measure"] == before and s["monthlyKgBasis"] == "measured"
    broken = data.replace(b",300", b",-1")
    with pytest.raises(service.ServiceError):
        survey.import_weights(app, HANBIT, "x.csv", broken, "2026-11")
    assert len([r for r in app.store.list_survey_records() if r["kind"] == "weight"]) == 2


def test_photo_is_private_until_specific_recipient_is_selected_and_can_be_revoked():
    app = make_app()
    ranged(app)
    out = io.BytesIO()
    Image.new("RGB", (30, 20)).save(out, format="PNG")
    p = survey.upload_photo(app, HANBIT, "st_sprue", out.getvalue())
    with pytest.raises(service.ServiceError):
        survey.photo(app, SAEGIL, p["id"])
    survey.photo(app, HANBIT, p["id"], shared_with=[SAEGIL])
    assert survey.photo(app, SAEGIL, p["id"])["data"].startswith("data:image/jpeg;base64,")
    assert "data" not in survey.view(app, SAEGIL)["sharedPhotos"][0]
    with pytest.raises(service.ServiceError):
        survey.photo(app, MISO, p["id"])
    survey.photo(app, HANBIT, p["id"], shared_with=[])
    with pytest.raises(service.ServiceError):
        survey.photo(app, SAEGIL, p["id"])
    with pytest.raises(service.ServiceError):
        survey.upload_photo(app, HANBIT, "st_sprue", b"<svg>bad</svg>")


def test_document_range_can_be_kept_without_choosing_an_endpoint():
    app = make_app()
    result = service.confirm_criteria(
        app,
        SAEGIL,
        "가상 매입표",
        [
            {
                "material": "PP",
                "grade": "A",
                "form": "가공 전",
                "unitPriceKrwPerKg": None,
                "priceLow": 300,
                "priceHigh": 340,
            }
        ],
        SAEGIL,
    )
    row = next(c for c in result["profile"]["criteria"] if c.get("priceLow") == 300)
    assert row["unitPriceKrwPerKg"] is None
    assert app.registry.get(row["rangeEvidence"][0], HANBIT).value == 300


def test_a_changed_vendor_condition_invalidates_a_previously_accepted_point():
    app = make_app()
    q = ranged(app)
    q = narrow(app, q, 325, 325, list(survey.FACTS))
    survey.change_quote(app, HANBIT, q["id"], {"revision": q["revision"]}, accept=True)
    survey.apply_deals(app, HANBIT)
    assert app.store.get_stream("st_sprue")["rangeDeals"]
    service.update_vendor(app, SAEGIL, {"collectionPolicy": "combined"}, SAEGIL)
    survey.apply_deals(app, HANBIT)
    assert app.store.get_stream("st_sprue")["rangeDeals"] == {}


def test_initial_input_does_not_require_or_invent_material_quality_or_quantity():
    app = make_app()
    s = service.update_stream(
        app,
        HANBIT,
        "st_unknown",
        {"name": "미확인 스크랩", "material": "", "monthlyKg": None},
        HANBIT,
    )
    assert not s.get("material") and s["monthlyKg"] is None and not s.get("measure")


def test_emitter_does_not_receive_vendor_route_cost_or_other_customers():
    app = make_app()
    q = ranged(app)
    assert "route" not in q
    mine = survey.view(app, HANBIT)
    assert "route" not in mine["quotes"][0]
    assert "costPerKm" not in str(mine)
    assert "rangeDeals" not in str(service.list_streams(app, HANBIT, SAEGIL))


def test_quote_cas_and_records_survive_sql_restart_and_org_purge(tmp_path):
    from sqlalchemy import create_engine

    from app.v3.sql import SqlStore, create_tables
    from app.v3.store import VersionConflict

    engine = create_engine(f"sqlite:///{tmp_path / 'survey.db'}")
    create_tables(engine)
    store = SqlStore(engine)
    record = {
        "id": "rq_1",
        "orgId": HANBIT,
        "vendorOrgId": SAEGIL,
        "kind": "range_quote",
        "revision": 1,
    }
    store.put_survey_record(record)
    store.put_survey_record({**record, "revision": 2}, expected_revision=1)
    with pytest.raises(VersionConflict):
        store.put_survey_record({**record, "revision": 2}, expected_revision=1)
    assert SqlStore(engine).get_survey_record("rq_1")["revision"] == 2
    store.purge_orgs([SAEGIL])
    assert store.list_survey_records() == []
    engine.dispose()


def test_changed_range_blocks_an_existing_agreement_instead_of_reusing_a_stale_price():
    app = make_app()
    q = ranged(app)
    q = narrow(app, q, 325, 325, list(survey.FACTS))
    survey.change_quote(app, HANBIT, q["id"], {"revision": q["revision"]}, accept=True)
    view = to_approval(app)
    chosen = next(a for a in view["result"]["assignments"] if a["streamId"] == "st_sprue")
    p = app.store.get_vendor_profile(SAEGIL)
    for c in p["criteria"]:
        if c["id"] == q["criterionId"]:
            c["priceHigh"] = 330
    service.update_vendor(app, SAEGIL, {"criteria": p["criteria"]}, SAEGIL)
    fresh = service.get_view(app, view["session"]["id"], HANBIT)
    approval = next(a for a in fresh["approvals"] if a["threadId"] == chosen["threadId"])
    assert "VENDOR_TERMS_CHANGED" in approval["blockedCodes"]


def test_price_previews_have_readable_calculation_evidence_and_hide_private_hashes():
    app = make_app()
    q = ranged(app)
    assert "criterionHash" not in q and "streamHash" not in q
    row = next(c for c in survey.view(app, HANBIT)["candidates"] if c["streamId"] == "st_sprue")
    assert row["moneyEvidence"]
    for evidence in row["moneyEvidence"]:
        app.registry.get(evidence, HANBIT)


def test_read_only_preview_does_not_write_calculation_evidence():
    app = make_app()
    ranged(app)
    app.read_only = True
    assert survey.view(app, HANBIT)["candidates"]


def test_cp949_weights_and_duplicate_columns_are_handled_without_fabricating_quality():
    app = make_app()
    good = "폐기물 이름,계량일,중량(kg)\n스프루·런너,2026-11-01,700\n".encode("cp949")
    assert (
        survey.import_weights(app, HANBIT, "계량.csv", good, "2026-11")["totals"]["st_sprue"] == 700
    )
    bad = "폐기물 이름,계량일,측정일,중량(kg)\n스프루·런너,2026-11-01,2026-11-01,800\n".encode()
    with pytest.raises(service.ServiceError):
        survey.import_weights(app, HANBIT, "계량.csv", bad, "2026-11")


def test_photo_recipient_can_be_a_normal_ai_inquiry_without_a_range_quote():
    app = make_app()
    to_approval(app)
    recipients = survey.view(app, HANBIT)["recipients"]
    assert any(r["streamId"] == "st_sprue" and r["vendorOrgId"] == SAEGIL for r in recipients)
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, format="JPEG")
    p = survey.upload_photo(app, HANBIT, "st_sprue", out.getvalue())
    survey.photo(app, HANBIT, p["id"], shared_with=[SAEGIL])
    assert survey.photo(app, SAEGIL, p["id"])["data"]
