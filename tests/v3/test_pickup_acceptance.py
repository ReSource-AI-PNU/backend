"""완료 시험 A~C(2026-11-19 평가 대응): 공장까지 실제로 수거할 수 있는지와 운반비를 반영한 한 달 손익이 결과표·합계·
승인 상세에서 같은 조건으로 보이는지 본다. 회사와 금액은 모두 가상이다.

재현(수정 전 운영 체험하기): 불량 사출품 400kg → (주)미소환경 줄에 "매입 kg당 220원 · 운반비 포함"만 보이고, 펼쳐야
"업체가 밝힌 수거 노선에 이 공장 지역이 없습니다"가 나왔다. 승인·서류에서는 같은 업체가 "조건이 맞음"으로만 보였다.

업체의 답은 업체 정보 수정(PUT /vendors의 transport.offRoute)으로 들어오고, 합의 뒤에 바뀌면 승인이 막히고 앞선 승인이
지워진 뒤 담당자가 다시 진행하면 새 조건으로 계산하는 기존 재확인 흐름을 탄다.
"""

from __future__ import annotations

import copy

import pytest

from app.v3 import service
from tests.v3.runtime_fixtures import HANBIT, MISO, SAEGIL, make_app, thread_of, to_approval

DEFECT = "st_defect"
PAID = {"status": "visit", "basis": "separate", "feePerPickupKrw": 30000, "pickupsPerMonth": 2}
FEE_ONLY = {"status": "visit", "basis": "separate", "feePerPickupKrw": 30000}
FREE = {"status": "visit", "basis": "included"}
UNDECIDED = {"status": "visit", "basis": "undecided"}
NO_PICKUP = {"status": "unavailable"}


def _world(*, saegil_takes_metal: bool = True):
    """시연 세계에서 승인 대기까지. saegil_takes_metal이 거짓이면 새길자원이 금속 인서트를 받지 않는다
    (불량 사출품을 받을 곳이 미소환경뿐인 시험 조건: 다른 비용·다른 후보가 없다)."""
    app = make_app(negotiate=True)
    if not saegil_takes_metal:
        guard = service.get_vendor(app, SAEGIL, SAEGIL)["guardrails"]
        service.update_vendor(
            app,
            SAEGIL,
            {"guardrails": {"autoDecline": [*guard.get("autoDecline", []), "금속 인서트"]}},
            SAEGIL,
        )
    view = to_approval(app)
    assert view["session"]["status"] == "awaiting_approval"
    return app, view


def _row(view, sid=DEFECT):
    return next(a for a in view["result"]["assignments"] if a["streamId"] == sid)


def _value(claim):
    return None if claim is None else claim["value"]


def _formula(app, claim):
    """claim의 첫 계산 근거의 이름과 계산식(근거 패널에 보이는 글)."""
    found = app.registry.get(claim["evidence"][0], HANBIT)
    return f"{found.label} {(found.meta or {}).get('formula') or ''} {found.excerpt or ''}"


def _approval(view, vendor):
    thread = thread_of(view, vendor)
    found = next((a for a in view["approvals"] if a["threadId"] == thread["id"]), None)
    rows = [a for a in view["result"]["assignments"] if a.get("threadId") == thread["id"]]
    return found, rows


def _answer(app, view, off):
    """업체가 업체 정보에서 노선 밖 수거 조건을 답하고, 공장 담당자가 다시 진행한다."""
    sid = view["session"]["id"]
    service.update_vendor(app, MISO, {"transport": {"offRoute": off}}, MISO)
    blocked = service.get_view(app, sid, HANBIT)
    after = service.run_session(app, sid, HANBIT)
    for _ in range(3):
        if after["session"]["status"] != "awaiting_answers":
            break
        for question in after["questions"]:
            if not question["answered"]:
                service.answer_question(app, sid, question["id"], "unknown", HANBIT)
        after = service.run_session(app, sid, HANBIT)
    return blocked, after


# ── A. 노선 밖이고 업체가 아직 답하지 않음 ────────────────────────────


def test_a_off_route_shows_pickup_check_in_result_and_approval():
    _app, view = _world()
    row = _row(view)
    assert row["vendorOrgId"] == MISO
    assert _value(row["unitPrice"]) == 220  # 협상 단가(매입 kg당 220원)
    assert _value(row["monthlyNow"]) == -100000  # 현행 처리비 kg당 250원 × 400kg
    pickup = row["pickup"]
    assert pickup["status"] == "check" and pickup["label"] == "별도 수거 확인 필요"
    # 노선 수거 조건은 단가에 포함이지만, 노선 밖이라 그 조건을 그대로 쓰지 않는다(운반비 확정 아님)
    assert pickup["routeBasis"] == "included"
    assert row["status"] == "conditional"
    check = next(c for c in row["checks"] if c["key"] == "pickup_route")
    assert check["status"] == "conditional" and "노선에 이 공장 지역이 없습니다" in check["text"]
    # 승인 카드가 그리는 줄은 결과표와 같은 항목이다(같은 수거 상태)
    approval, rows = _approval(view, MISO)
    assert approval is not None and [r["streamId"] for r in rows] == [DEFECT]
    assert rows[0]["pickup"] == pickup


def test_a_unconfirmed_transport_is_not_fixed_at_zero():
    app, view = _world()
    row = _row(view)
    # 운반비를 정하지 못했으니 0원으로 확정하지 않고 월 손익을 "별도 운반비 반영 전"으로 둔다
    assert row["transportPending"] is True
    assert row["transport"] is None or _value(row["transport"]) != 0
    assert _value(row["monthlyAfter"]) == 88000
    # 계산 근거(근거 패널에 열리는 식)에 "별도 운반비 반영 전"을 밝힌다. 합계도 그렇다
    assert "반영 전" in _formula(app, row["monthlyAfter"])
    assert DEFECT in view["result"]["transportPending"]
    assert "반영 전" in _formula(app, view["result"]["totals"]["difference"])


# ── B. 업체가 별도 수거를 확인하고 추가 운반비 회당 30,000원 × 월 2회(공장 부담) ──


def test_b_paid_visit_numbers_match_in_result_and_approval():
    app, view = _world(saegil_takes_metal=False)
    sid = view["session"]["id"]
    thread = thread_of(view, MISO)
    assert _row(view)["vendorOrgId"] == MISO
    service.approve(app, sid, thread["id"], HANBIT)  # 공장이 이전 조건으로 먼저 승인해 둔 상태

    blocked, after = _answer(app, view, PAID)
    # 이전 조건으로 한 승인은 기존 재확인 흐름을 탄다: 막히고 앞선 승인이 지워진다
    stale, _ = _approval(blocked, MISO)
    assert stale["blocked"] and "PICKUP_TERMS_CHANGED" in stale["blockedCodes"]
    assert not stale["emitter"] and not stale["vendor"]

    row = _row(after)
    assert row["vendorOrgId"] == MISO and row["transportPending"] is False
    # 수거·운반 답은 단가 합의를 흔들지 않는다: 보이는 단가와 계산에 쓴 단가가 같다
    assert _value(row["unitPrice"]) == 220
    assert _value(row["revenue"]) == 88000  # 매각 대금 220원 × 400kg
    assert (
        _value(row["transport"]) == 60000
    )  # 월 운반비 30,000원 × 2회(이 업체로 가는 폐기물이 하나뿐)
    assert _value(row["monthlyAfter"]) == 28000
    assert _value(row["monthlyNow"]) == -100000
    assert _value(row["difference"]) == 128000
    pickup = row["pickup"]
    assert (
        pickup["status"] == "visit"
        and pickup["basis"] == "separate"
        and pickup["paidBy"] == "emitter"
    )
    assert (_value(pickup["fee"]), _value(pickup["pickups"]), _value(pickup["monthly"])) == (
        30000,
        2,
        60000,
    )
    # 누가 어떤 조건을 확인했는지: 금액 근거는 업체 담당자 입력이다
    fee_sources = [app.registry.get(e, HANBIT) for e in pickup["fee"]["evidence"]]
    assert any(
        e.kind == "human" and e.source.startswith("(주)미소환경 담당자 입력") for e in fee_sources
    ) or any("human:" in e.id for e in fee_sources)
    # 승인 상세는 같은 항목을 그린다(기준이 같다)
    approval, rows = _approval(after, MISO)
    assert approval is not None and not approval["blocked"]
    assert [(r["streamId"], _value(r["monthlyAfter"]), _value(r["transport"])) for r in rows] == [
        (DEFECT, 28000, 60000)
    ]
    # 합계는 폐기물별 값의 합이다
    total = sum(_value(a["difference"]) or 0 for a in after["result"]["assignments"])
    assert _value(after["result"]["totals"]["difference"]) == pytest.approx(total)


def test_b_in_the_demo_world_a_better_route_takes_the_stream():
    # 새길자원(노선이 녹산을 지나고 운반비 포함)도 받을 수 있으면, 운반비를 넣은 미소환경(+28,000원)보다
    # 새길자원(+72,000원)이 낫다. 다시 진행하면 옮겨 가고 미소환경은 다른 선택지로 남는다
    app, view = _world()
    _, after = _answer(app, view, PAID)
    row = _row(after)
    assert row["vendorOrgId"] == SAEGIL and row["pickup"]["status"] == "route"
    assert _value(row["monthlyAfter"]) == 72000
    miso = next(x for x in row["alternatives"] if x["vendorOrgId"] == MISO)
    assert _value(miso["monthlyAfter"]) == 28000
    # 대안 줄의 단가와 한 달 값의 기준이 같다(220원 × 400kg − 운반비 60,000원)
    assert _value(miso["unitPrice"]) * 400 - 60000 == _value(miso["monthlyAfter"])


# ── C. 회당 금액만, 월 횟수 모름 · 비용 미정 · 추가비 없음 · 수거 불가 ─────


def test_c_fee_without_count_stays_pending():
    app, view = _world(saegil_takes_metal=False)
    _, after = _answer(app, view, FEE_ONLY)
    row = _row(after)
    assert row["vendorOrgId"] == MISO
    assert row["transportPending"] is True and row["pickup"]["pending"] is True
    assert _value(row["pickup"]["fee"]) == 30000 and row["pickup"]["pickups"] is None
    assert _value(row["monthlyAfter"]) == 88000  # 운반비를 넣기 전 값
    assert "반영 전" in _formula(app, row["monthlyAfter"])
    assert DEFECT in after["result"]["transportPending"]


@pytest.mark.parametrize(
    ("off", "pending", "after"), [(UNDECIDED, True, 88000), (FREE, False, 88000)]
)
def test_c_undecided_and_free_answers(off, pending, after):
    app, view = _world(saegil_takes_metal=False)
    _, done = _answer(app, view, off)
    row = _row(done)
    assert row["vendorOrgId"] == MISO and row["pickup"]["status"] == "visit"
    assert row["transportPending"] is pending
    assert _value(row["monthlyAfter"]) == after
    if not pending:
        assert _value(row["difference"]) == 188000


def test_c_no_pickup_leaves_recommendations_and_totals_with_a_reason():
    app, view = _world(saegil_takes_metal=False)
    _, after = _answer(app, view, NO_PICKUP)
    row = _row(after)
    assert row["vendorOrgId"] != MISO  # 진행 가능한 추천에서 빠진다
    _, rows = _approval(after, MISO)
    assert DEFECT not in [r["streamId"] for r in rows]
    # 사유가 보인다: 다른 선택지의 설명이나 확인 항목에 수거 불가가 적힌다
    texts = [x.get("note") or "" for x in row["alternatives"]] + [c["text"] for c in row["checks"]]
    assert any("수거" in t and ("불가" in t or "올 수 없" in t) for t in texts)
    # 합계에 미소환경 값이 섞이지 않는다
    total = sum(_value(a["difference"]) or 0 for a in after["result"]["assignments"])
    assert _value(after["result"]["totals"]["difference"]) == pytest.approx(total)


# ── 기존 데이터 ──────────────────────────────────────────────────────


def test_sessions_saved_before_the_pickup_fields_still_open():
    """수거 필드가 없는 예전 진단(결과 한 장, 합의 지문)도 열리고, 그것만으로 승인이 막히지 않는다."""
    app, view = _world()
    sid = view["session"]["id"]
    session = copy.deepcopy(app.store.get_session(sid))
    for item in session["work"]["result"]["assignments"]:
        for key in ("pickup", "transportPending"):
            item.pop(key, None)
        for alt in item.get("alternatives") or []:
            alt.pop("transportPending", None)
            alt.pop("pickupStatus", None)
    session["work"]["result"].pop("transportPending", None)
    session["work"]["result"].pop("scope", None)
    for terms in (session["work"].get("agreedTerms") or {}).values():
        terms.pop("pickup", None)
        terms.pop("pickupShown", None)
    session["work"].pop("pickups", None)
    app.store.put_session(session)
    reopened = service.get_view(app, sid, HANBIT)
    assert reopened["result"]["assignments"]
    assert not any(a["blocked"] for a in reopened["approvals"])
