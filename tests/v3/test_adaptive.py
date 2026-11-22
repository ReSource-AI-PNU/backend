import copy

import pytest

from app.v3 import adaptive, service
from tests.v3.runtime_fixtures import HANBIT, SAEGIL, make_app, to_approval


def setup():
    app = make_app()
    view = to_approval(app)
    return app, view["session"]["id"]


def options(**changes):
    return {"streamId": "st_sprue", "monthlyKg": 800, "maxPickupsPerMonth": None, **changes}


def test_preview_is_isolated_and_apply_requires_fresh_token_and_reapproval():
    app, sid = setup()
    before = app.store.snapshot()
    evidence = app.registry.list()
    p = adaptive.preview(app, sid, options(), HANBIT)
    assert app.store.snapshot() == before
    assert app.registry.list() == evidence
    assert (
        p["result"]["totals"]["monthlyAfter"]["value"]
        != before["sessions"][sid]["work"]["result"]["totals"]["monthlyAfter"]["value"]
    )
    applied = adaptive.apply(app, sid, {**options(), "token": p["token"]}, HANBIT)
    assert adaptive.apply(app, sid, {**options(), "token": p["token"]}, HANBIT) == applied
    calls = []
    adaptive.launch_pending(app, lambda s, r: calls.append((s, r)))
    assert len(calls) == 1
    adaptive.launch_pending(app, lambda s, r: calls.append((s, r)))
    assert len(calls) == 1
    service.execute_run(app, *calls[0])
    result = service.get_view(app, applied["sessionId"], HANBIT)
    assert result["session"]["status"] == "awaiting_approval"
    assert not result["documents"]
    assert all(not a.get("emitter") and not a.get("vendor") for a in result["approvals"])


def test_vendor_change_queues_once_and_blocks_old_approval():
    app, sid = setup()
    profile = service.get_vendor(app, SAEGIL, SAEGIL)
    profile["criteria"][0]["minMonthlyKg"] = 9999
    service.update_vendor(app, SAEGIL, {"criteria": profile["criteria"]}, SAEGIL)
    change = adaptive.state(app, sid, HANBIT)["change"]
    assert change and change["state"] == "queued"
    count = len(app.store.list_sessions())
    service.update_vendor(app, SAEGIL, {"criteria": profile["criteria"]}, SAEGIL)
    assert len(app.store.list_sessions()) == count
    thread = next(t for t in app.store.list_threads(session_id=sid) if t["vendorOrgId"] == SAEGIL)
    with pytest.raises(service.ServiceError, match="업체의 조건이 바뀌어"):
        service.approve(app, sid, thread["id"], HANBIT)
    adaptive.execute(app, change["sessionId"])
    assert adaptive.state(app, sid, HANBIT)["change"]["state"] in ("ready", "needs_input")


def test_stale_and_foreign_preview_rejected():
    app, sid = setup()
    p = adaptive.preview(app, sid, options(), HANBIT)
    service.update_stream(app, HANBIT, "st_sprue", {"monthlyKg": 900}, HANBIT)
    with pytest.raises(service.ServiceError) as e:
        adaptive.apply(app, sid, {**options(), "token": p["token"]}, HANBIT)
    assert e.value.code == "STALE_PREVIEW"
    with pytest.raises(service.ServiceError):
        adaptive.preview(app, sid, options(), SAEGIL)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, 0, "800"])
def test_invalid_amount(value):
    app, sid = setup()
    with pytest.raises(service.ServiceError):
        adaptive.preview(app, sid, options(monthlyKg=value), HANBIT)


def test_pickup_limit_never_rewrites_vendor_terms():
    app, sid = setup()
    before = copy.deepcopy(app.store.list_vendor_profiles())
    p = adaptive.preview(app, sid, options(maxPickupsPerMonth=1), HANBIT)
    assert p["excluded"]
    assert app.store.list_vendor_profiles() == before
    excluded = {x["vendorOrgId"] for x in p["excluded"]}
    assert not any(a.get("vendorOrgId") in excluded for a in p["result"]["assignments"])


def test_confirmed_documents_and_approvals_are_preserved_when_new_terms_arrive():
    from tests.v3.runtime_fixtures import approve_all

    app, sid = setup()
    confirmed = approve_all(app, service.get_view(app, sid, HANBIT))
    assert confirmed["session"]["status"] == "confirmed"
    documents = app.store.list_documents(sid)
    approvals = app.store.list_approvals(sid)
    profile = service.get_vendor(app, SAEGIL, SAEGIL)
    profile["criteria"][0]["unitPriceKrwPerKg"] -= 50
    service.update_vendor(app, SAEGIL, {"criteria": profile["criteria"]}, SAEGIL)
    change = adaptive.state(app, sid, HANBIT)["change"]
    assert change and change["sessionId"] != sid
    adaptive.execute(app, change["sessionId"])
    assert app.store.get_session(sid)["status"] == "confirmed"
    assert app.store.list_documents(sid) == documents
    assert app.store.list_approvals(sid) == approvals
    assert not app.store.list_documents(change["sessionId"])


def test_unrelated_vendor_edit_does_not_queue_work():
    app, sid = setup()
    service.update_vendor(
        app, SAEGIL, {"model": {"provider": "foundry", "name": "another"}}, SAEGIL
    )
    assert adaptive.state(app, sid, HANBIT) == {"change": None}


def test_document_criteria_update_queues_research():
    app, sid = setup()
    rows = [
        {
            "material": "PP",
            "grade": "A",
            "form": None,
            "unitPriceKrwPerKg": 210,
            "minMonthlyKg": 9000,
        }
    ]
    service.confirm_criteria(app, SAEGIL, "새 기준표", rows, SAEGIL)
    assert adaptive.state(app, sid, HANBIT)["change"]
