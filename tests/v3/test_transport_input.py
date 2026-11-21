"""업체가 업체 정보에서 운반 조건(노선 수거 운반비, 노선 밖 공장의 별도 수거)을 고친다(PUT /vendors의 transport).

회사와 금액은 모두 가상이다. 화면의 네 가지 답(수거 불가, 수거 가능·추가비 없음, 수거 가능·추가비 있음, 비용 미정)이
judge/pickup.py가 읽는 transport.offRoute 모양으로 저장되고, 누가 언제 정했는지가 담당자 입력 근거로 남는지 본다.
"""

from __future__ import annotations

import copy

import pytest

from app.v3 import service, transport_input
from tests.v3.runtime_fixtures import HANBIT, MISO, make_app

NO_PICKUP = {"status": "unavailable"}
FREE_VISIT = {"status": "visit", "basis": "included"}
PAID_VISIT = {
    "status": "visit",
    "basis": "separate",
    "feePerPickupKrw": 30000,
    "pickupsPerMonth": 2,
}
FEE_ONLY = {"status": "visit", "basis": "separate", "feePerPickupKrw": 30000}
UNDECIDED = {"status": "visit", "basis": "undecided"}


# ── 입력 검사 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        (NO_PICKUP, {"status": "unavailable"}),
        (FREE_VISIT, {"status": "visit", "basis": "included", "paidBy": "vendor"}),
        (
            PAID_VISIT,
            {
                "status": "visit",
                "basis": "separate",
                "paidBy": "emitter",
                "feePerPickupKrw": 30000,
                "pickupsPerMonth": 2,
            },
        ),
        (
            FEE_ONLY,
            {"status": "visit", "basis": "separate", "paidBy": "emitter", "feePerPickupKrw": 30000},
        ),
        (
            {"status": "visit", "basis": "separate", "feePerMonthKrw": 60000},
            {"status": "visit", "basis": "separate", "paidBy": "emitter", "feePerMonthKrw": 60000},
        ),
        (UNDECIDED, {"status": "visit", "basis": "undecided"}),
    ],
)
def test_four_answers_are_stored_in_the_shape_pickup_reads(given, stored):
    assert transport_input.clean_patch({"offRoute": given}) == {"offRoute": stored}


def test_route_terms_keep_the_old_paid_by_shape():
    # 예전 프로필의 {"paidBy": "vendor"}와 {"paidBy": "emitter", "feePerPickupKrw"}를 그대로 돌려보내도 받는다
    assert transport_input.clean_patch({"paidBy": "vendor", "evidence": ["x"]}) == {
        "base": {"basis": "included", "paidBy": "vendor"}
    }
    assert transport_input.clean_patch({"paidBy": "emitter", "feePerPickupKrw": 30000}) == {
        "base": {"basis": "separate", "paidBy": "emitter", "feePerPickupKrw": 30000}
    }
    # 따로 받는다는 것만 알고 금액을 모르면 금액 없이 둔다(금액 확인 필요)
    assert transport_input.clean_patch({"paidBy": "emitter"}) == {
        "base": {"basis": "separate", "paidBy": "emitter"}
    }
    assert transport_input.clean_patch(None) == {}
    assert transport_input.clean_patch({"offRoute": None}) == {"offRoute": None}


@pytest.mark.parametrize(
    ("given", "words"),
    [
        ({"offRoute": {"status": "maybe"}}, "visit"),
        ({"offRoute": {"status": "unavailable", "feePerPickupKrw": 1000}}, "수거 불가면"),
        ({"offRoute": {"status": "visit"}}, "단가에 포함(included)"),
        ({"offRoute": {**PAID_VISIT, "feePerPickupKrw": 0}}, "0보다 크고"),
        ({"offRoute": {**PAID_VISIT, "feePerPickupKrw": True}}, "0보다 크고"),
        ({"offRoute": {**PAID_VISIT, "pickupsPerMonth": 500}}, "한 달 수거 횟수"),
        ({"offRoute": {**PAID_VISIT, "feePerMonthKrw": 60000}}, "하나만"),
        (
            {
                "offRoute": {
                    "status": "visit",
                    "basis": "separate",
                    "feePerMonthKrw": 1,
                    "pickupsPerMonth": 2,
                }
            },
            "한 달 금액이면",
        ),
        ({"offRoute": {**PAID_VISIT, "paidBy": "vendor"}}, "공장이 냅니다"),
        ({"offRoute": {**FREE_VISIT, "feePerPickupKrw": 1000}}, "적지 않습니다"),
        ({"offRoute": {**PAID_VISIT, "memo": "x"}}, "memo"),
        ({"distanceKm": 3}, "distanceKm"),
        ({"offRoute": "visit"}, "객체"),
    ],
)
def test_wrong_terms_are_rejected_with_a_sentence(given, words):
    with pytest.raises(transport_input.TransportError) as caught:
        transport_input.clean_patch(given)
    assert words in str(caught.value)


def test_describe_writes_what_the_vendor_chose():
    assert transport_input.describe({"status": "unavailable"}, off_route=True).endswith(
        "(수거 불가)."
    )
    paid = transport_input.clean_patch({"offRoute": PAID_VISIT})["offRoute"]
    assert transport_input.describe(paid, off_route=True) == (
        "수거 노선 밖 공장도 따로 수거하러 갑니다. 추가 운반비는 회당 30,000원이고 공장이 냅니다. "
        "한 달 수거 횟수는 2회입니다."
    )
    fee_only = transport_input.clean_patch({"offRoute": FEE_ONLY})["offRoute"]
    assert transport_input.describe(fee_only, off_route=True).endswith(
        "한 달 수거 횟수는 정하지 않았습니다."
    )
    assert "아직 정하지 않았습니다" in transport_input.describe(
        transport_input.clean_patch({"offRoute": UNDECIDED})["offRoute"], off_route=True
    )


# ── 저장과 근거 ──────────────────────────────────────────────────────


# 이 파일의 시험은 시드 그대로의 세계(miso_visits=False)를 쓴다: 미소환경은 노선 밖 조건을 아직 밝히지 않았다


def _transport(app, vendor=MISO):
    return service.get_vendor(app, vendor, vendor).get("transport") or {}


def test_off_route_terms_are_saved_with_who_and_when():
    app = make_app(miso_visits=False)
    before = copy.deepcopy(_transport(app))
    assert (
        before.get("paidBy") == "vendor" and "offRoute" not in before
    )  # 시연 미소환경: 단가에 포함

    service.update_vendor(app, MISO, {"transport": {"offRoute": PAID_VISIT}}, MISO)
    saved = _transport(app)
    # 노선 수거 조건과 그 근거는 그대로 두고 노선 밖 조건만 더한다
    assert {k: saved[k] for k in before} == before
    off = saved["offRoute"]
    assert {
        k: off[k] for k in ("status", "basis", "paidBy", "feePerPickupKrw", "pickupsPerMonth")
    } == {
        "status": "visit",
        "basis": "separate",
        "paidBy": "emitter",
        "feePerPickupKrw": 30000,
        "pickupsPerMonth": 2,
    }
    fee, count, stance = off["evidence"]
    assert off["feeEvidence"] == [fee] and off["pickupsEvidence"] == [count]
    fee_ev = app.registry.get(fee, MISO)
    assert (fee_ev.kind, fee_ev.value, fee_ev.unit) == ("human", 30000.0, "원/회")
    assert (
        app.registry.get(count, MISO).value == 2.0 and app.registry.get(count, MISO).unit == "회/월"
    )
    stance_ev = app.registry.get(stance, MISO)
    name = service.get_vendor(app, MISO, MISO)["name"]
    # 누가(업체 이름 · 담당자 입력), 언제(오늘), 무엇을(고른 조건 문장) 정했는지
    assert stance_ev.source == f"{name} 담당자 입력(업체 정보 · 노선 밖 공장 수거 조건)"
    assert stance_ev.as_of == app.today
    assert "회당 30,000원" in stance_ev.excerpt and "2회" in stance_ev.excerpt
    assert stance_ev.meta["enteredBy"] == name


def test_sending_the_saved_terms_back_changes_nothing():
    # 업체 화면은 받은 프로필을 그대로 다시 보낸다. 같은 조건이면 근거 ID와 판(version)이 그대로여야 한다
    app = make_app(miso_visits=False)
    service.update_vendor(app, MISO, {"transport": {"offRoute": PAID_VISIT}}, MISO)
    saved = copy.deepcopy(_transport(app))
    versions = {e: app.registry.get(e, MISO).version for e in saved["offRoute"]["evidence"]}
    service.update_vendor(app, MISO, {"transport": copy.deepcopy(saved)}, MISO)
    assert _transport(app) == saved
    assert {e: app.registry.get(e, MISO).version for e in saved["offRoute"]["evidence"]} == versions


def test_changing_the_answer_replaces_the_terms_and_bumps_the_evidence():
    app = make_app(miso_visits=False)
    service.update_vendor(app, MISO, {"transport": {"offRoute": FEE_ONLY}}, MISO)
    first = copy.deepcopy(_transport(app)["offRoute"])
    assert "pickupsPerMonth" not in first and first["pickupsEvidence"] == []
    service.update_vendor(app, MISO, {"transport": {"offRoute": PAID_VISIT}}, MISO)
    second = _transport(app)["offRoute"]
    assert second["pickupsPerMonth"] == 2
    # 같은 항목의 근거는 같은 ID로 판이 오른다(그 근거를 인용한 대화가 바뀐 것을 안다)
    assert second["feeEvidence"] == first["feeEvidence"]
    stance = second["evidence"][-1]
    assert app.registry.get(stance, MISO).version >= 2

    service.update_vendor(app, MISO, {"transport": {"offRoute": NO_PICKUP}}, MISO)
    gone = _transport(app)["offRoute"]
    assert (
        gone["status"] == "unavailable" and gone["feeEvidence"] == [] and len(gone["evidence"]) == 1
    )

    service.update_vendor(app, MISO, {"transport": {"offRoute": None}}, MISO)
    assert "offRoute" not in _transport(app)


def test_shared_evidence_stays_open_to_the_counterpart():
    # 대화 상대에게 이미 열린 근거를 고쳐 쓸 때 상대가 볼 수 없게 되지 않는다
    app = make_app(miso_visits=False)
    service.update_vendor(app, MISO, {"transport": {"offRoute": UNDECIDED}}, MISO)
    stance = _transport(app)["offRoute"]["evidence"][-1]
    from app.v3.demo.seed import share_evidence

    share_evidence(app.registry, stance, HANBIT)
    service.update_vendor(app, MISO, {"transport": {"offRoute": FREE_VISIT}}, MISO)
    assert app.registry.get(stance, HANBIT).excerpt.endswith(
        "따로 받지 않습니다(매입 단가에 포함)."
    )


def test_wrong_transport_is_a_422_and_saves_nothing():
    app = make_app(miso_visits=False)
    before = copy.deepcopy(_transport(app))
    with pytest.raises(service.ServiceError) as caught:
        service.update_vendor(
            app,
            MISO,
            {
                "transport": {
                    "offRoute": {"status": "visit", "basis": "separate", "feePerPickupKrw": -1}
                }
            },
            MISO,
        )
    assert caught.value.status == 422 and caught.value.code == "INVALID_VALUE"
    assert _transport(app) == before


def test_only_the_vendor_edits_its_transport():
    app = make_app(miso_visits=False)
    with pytest.raises(service.ServiceError) as caught:
        service.update_vendor(app, MISO, {"transport": {"offRoute": NO_PICKUP}}, HANBIT)
    assert caught.value.status in (403, 404)
    # 다른 조직에는 운반 조건이 보이지 않는다(공개 프로필은 허가 품목과 모델뿐)
    assert "transport" not in service.get_vendor(app, MISO, HANBIT)


def test_purchase_criteria_upload_keeps_the_off_route_terms():
    # 매입 기준표의 수거비는 노선 수거 조건만 바꾼다. 업체가 정한 노선 밖 조건은 지우지 않는다
    app = make_app(miso_visits=False)
    service.update_vendor(app, MISO, {"transport": {"offRoute": PAID_VISIT}}, MISO)
    off = copy.deepcopy(_transport(app)["offRoute"])
    service.confirm_criteria(
        app,
        MISO,
        "기준표.pdf",
        [
            {
                "material": "PP",
                "grade": "B",
                "unitPriceKrwPerKg": 200,
                "collectionFee": "separate",
                "collectionFeePerPickupKrw": 50000,
            }
        ],
        MISO,
    )
    saved = _transport(app)
    assert saved["paidBy"] == "emitter" and saved["feePerPickupKrw"] == 50000
    assert saved["offRoute"] == off
