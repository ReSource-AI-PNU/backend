"""사람이 읽는 이름: claim의 label·role, calc 근거 이름, 기준 노드 이름, 가정값 목록, 처리비 문구."""

import pytest

from app.v3 import names
from app.v3.a2a.message import PLACEHOLDER, Claim
from app.v3.agents import questions
from app.v3.agents.pool import ClaimPool
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.render import render_text
from app.v3.evidence.validator import validate_text
from tests.v3.runtime_fixtures import (
    HANBIT,
    NAMHAE,
    SAEGIL,
    TODAY,
    approve_all,
    make_app,
    thread_of,
    to_approval,
)


def claims_in(node, found=None):
    """뷰 어디에 있든 claim 객체를 모은다."""
    found = [] if found is None else found
    if isinstance(node, dict):
        if {"id", "kind", "value", "evidence"} <= set(node):
            found.append(node)
        for value in node.values():
            claims_in(value, found)
    elif isinstance(node, list):
        for value in node:
            claims_in(value, found)
    return found


@pytest.fixture(scope="module")
def demo():
    app = make_app()
    waiting = to_approval(app)
    return app, waiting, approve_all(app, waiting)


# ── 이름 규칙 ────────────────────────────────────────────────────────
def test_describe_names_claims_by_their_key():
    assert names.describe("st_sprue_unitPrice", 320) == ("매입 단가", "unitPrice")
    assert names.describe("route_purge_unitPrice", -80) == ("처리비", "treatmentFee")
    assert names.describe("st_sprue_monthlyKg", 600) == ("월 발생량", "monthlyKg")
    assert names.describe("s0Difference", -1) == ("현행 대비 차이", "difference")
    assert names.describe("total_difference") == ("현행 대비 차이", "difference")
    assert names.describe("totalDifference") == ("현행 대비 차이 합계", "difference")
    assert names.describe("st_film_carbon") == ("탄소 변화", "carbon")
    assert names.describe("carbonDifference") == ("탄소 변화", "carbon")
    assert names.describe("pc_pp_a_maxMoisturePct") == ("수분 상한", "threshold")
    assert names.describe("load_a_minMonthlyKg") == ("최소 물량", "threshold")
    assert names.describe("capacityM3") == ("차량 적재 부피", "capacity")  # 숫자로 끝나는 키
    assert names.describe("distanceKm0") == ("공장에서 업체까지 거리", "distance")
    assert names.describe("st_a_sensitivityB") == ("현행 가정이 다를 때의 차이", "sensitivity")
    assert names.describe("m3_route_sprue_unitPrice_2", 320)[1] == "unitPrice"  # 풀이 붙인 번호
    assert names.describe("clauseA", "원문", "clause") == ("조항 인용", "clause")
    assert names.describe("무엇인지모름", 1) == (None, None)
    assert set(names.ROLES) >= {"unitPrice", "monthlyKg", "difference", "carbon", "threshold"}
    assert not any(names.has_digit(label) for label in names.LABELS.values())


def test_calc_and_criterion_labels():
    assert names.calc_label("s0Difference", "스프루·런너") == "스프루·런너 현행 대비 차이 계산"
    assert names.calc_label("unitPrice", None, -80) == "처리비 계산"
    assert names.calc_label("알수없는키") == "알수없는키"
    criterion = {"id": "pc_pp_a", "material": "PP", "grade": "A", "form": "분쇄"}
    assert names.criterion_label(criterion) == "PP · A등급 · 분쇄"
    assert names.criterion_label({"material": "PP+ABS", "grade": "등외"}) == "PP+ABS · 등외"
    assert names.criterion_label(None, "매입 기준") == "매입 기준"


def test_trusted_drops_labels_the_code_did_not_make():
    forged = {
        "id": "route_sprue_unitPrice",
        "kind": "number",
        "value": -80,
        "unit": "원/kg",
        "evidence": ["doc:x"],
        "label": "매입 단가 9,999원",
        "role": "unitPrice",
    }
    assert names.trusted(forged) == {**forged, "label": "처리비", "role": "treatmentFee"}
    unknown = {"id": "x", "kind": "number", "value": 1, "evidence": [], "label": "특가 1원"}
    assert names.trusted(unknown) == {"id": "x", "kind": "number", "value": 1, "evidence": []}
    kept = names.trusted({**unknown, "role": "carbon", "label": "아무 말"})
    assert (kept["label"], kept["role"]) == ("탄소 변화", "carbon")


# ── Claim ────────────────────────────────────────────────────────────
def test_claim_carries_optional_label_and_role():
    plain = Claim("unitPrice", "number", 320, ("doc:x",), "원/kg")
    assert "label" not in plain.to_dict() and "role" not in plain.to_dict()
    named = Claim(
        "unitPrice", "number", 320, ("doc:x",), "원/kg", label="매입 단가", role="unitPrice"
    )
    assert named.to_dict()["label"] == "매입 단가" and named.to_dict()["role"] == "unitPrice"
    assert Claim.from_dict(named.to_dict()) == named
    assert Claim.from_dict(named.to_dict()).label == "매입 단가"
    assert plain == named  # 이름만 다른 claim은 같은 claim이다


def test_validator_ignores_label_and_role():
    registry = InMemoryRegistry()
    registry.put(
        Evidence(
            id="doc:org_a:기준표",
            kind="doc",
            label="매입 기준표(가상)",
            value=320,
            unit="원/kg",
            excerpt="단가 320원/kg",
            source="가상",
            as_of=TODAY,
            access=("org_a",),
        )
    )
    base = {"id": "p", "kind": "number", "value": 320, "unit": "원/kg"}
    base["evidence"] = ["doc:org_a:기준표"]
    text = "단가는 {{p}}입니다."
    for extra in ({}, {"label": "매입 단가", "role": "unitPrice"}, {"label": "9,999원 특가"}):
        result = validate_text(text, [{**base, **extra}], registry, org_id="org_a", today=TODAY)
        assert result.ok, result.to_dict()
    # 값이 틀리면 이름이 무엇이든 거부한다
    wrong = {**base, "value": 330, "label": "매입 단가", "role": "unitPrice"}
    assert validate_text(text, [wrong], registry, org_id="org_a", today=TODAY).codes == (
        "VALUE_MISMATCH",
    )


def test_pool_names_claims_from_fields_not_from_the_sender():
    app = make_app()
    pool = ClaimPool(app.registry, HANBIT)
    facts = pool.add_stream(app.store.get_stream("st_sprue"), private=True)
    kg = pool.get(facts["monthlyKg"]).claim
    assert (kg.label, kg.role) == ("월 발생량", "monthlyKg")
    cost = pool.get(facts["currentUnitCost"]).claim
    assert (cost.label, cost.role) == ("현행 처리 단가", "currentUnitCost")
    message = {
        "seq": 3,
        "from": SAEGIL,
        "claims": [{**kg.to_dict(), "id": "st_sprue_monthlyKg", "label": "월 9톤", "role": "x"}],
    }
    added = pool.add_message(message)
    again = pool.get(added["st_sprue_monthlyKg"]).claim
    assert (again.label, again.role) == ("월 발생량", "monthlyKg")


def test_render_writes_a_treatment_fee_as_a_size():
    fee = {"id": "p", "kind": "number", "value": -80, "unit": "원/kg", "evidence": ["doc:x"]}
    assert render_text("단가 {{p}}", [fee]) == "단가 -80원/kg"
    assert render_text("처리비 {{p}}", [{**fee, "role": "treatmentFee"}]) == "처리비 80원/kg"


# ── 시연 뷰 ──────────────────────────────────────────────────────────
def test_every_named_claim_in_the_view_follows_the_rules(demo):
    _, waiting, done = demo
    for view in (waiting, done):
        found = claims_in(view)
        named = [c for c in found if c.get("role")]
        assert len(named) > 40
        for claim in named:
            assert claim["role"] in names.ROLES and claim["label"], claim
            assert not names.has_digit(claim["label"]), claim  # 이름으로 숫자를 흘리지 않는다
        prices = [c for c in named if c["id"].endswith("unitPrice")]
        assert {c["role"] for c in prices} == {"unitPrice", "treatmentFee"}
        assert all((c["value"] < 0) == (c["role"] == "treatmentFee") for c in prices)
        assert all(c["label"] == names.LABELS[c["role"]] for c in prices)
    result = waiting["result"]
    sprue = next(a for a in result["assignments"] if a["streamId"] == "st_sprue")
    assert sprue["unitPrice"]["label"] == "매입 단가"
    assert sprue["difference"]["label"] == "현행 대비 차이"
    assert sprue["carbon"]["role"] == "carbon"
    purge = next(a for a in result["assignments"] if a["streamId"] == "st_purge")
    assert purge["unitPrice"]["role"] == "treatmentFee" and purge["unitPrice"]["value"] < 0
    assert result["totals"]["difference"]["label"] == "현행 대비 차이 합계"
    kg = next(s for s in waiting["streams"] if s["id"] == "st_sprue")["monthlyKg"]
    assert (kg["label"], kg["role"]) == ("월 발생량", "monthlyKg")


def test_calc_evidence_has_korean_labels(demo):
    app, waiting, _ = demo
    calc = [
        app.registry.get(evidence_id, None)
        for claim in claims_in(waiting["result"])
        for evidence_id in claim["evidence"][:1]
        if evidence_id.startswith("calc:")
    ]
    assert len(calc) > 10
    for evidence in calc:
        assert evidence.label.endswith(" 계산"), evidence.label
        assert evidence.label.isascii() is False
    labels = {e.label for e in calc}
    assert "스프루·런너 현행 대비 차이 계산" in labels
    assert not labels & {"unitPrice", "s0Difference", "monthlyAfter", "totalNow"}


def test_graph_criterion_nodes_are_named_by_material_grade_form(demo):
    app, waiting, _ = demo
    nodes = [n for n in waiting["graph"]["nodes"] if n["type"] == "criterion"]
    assert nodes
    criteria = {
        c["id"]: c for p in app.store.list_vendor_profiles() for c in p.get("criteria") or []
    }
    for node in nodes:
        assert node["label"] == names.criterion_label(criteria[node["id"]])
        assert node["id"] not in node["label"] and " · " in node["label"]
    film = next(n for n in nodes if n["id"] == "pc_saegil_film_b")
    assert film["label"] == "LDPE · B등급 · 필름"


def test_assumptions_are_objects_with_names_and_values(demo):
    app, waiting, _ = demo
    assumed = waiting["result"]["assumptions"]
    assert assumed and all(set(a) == {"id", "label", "value", "unit"} for a in assumed)
    for item in assumed:
        evidence = app.registry.get(item["id"], HANBIT)
        assert evidence.kind == "assumption" and item["label"] == evidence.label
        if item["value"] is None:
            assert item["unit"] is None  # 값 하나로 적을 수 없는 가정(등급 기준표)
        else:
            assert item["value"] == evidence.value and item["unit"] == evidence.unit
    theirs = make_view_for(app, waiting, SAEGIL)
    assert theirs["result"]["assumptions"] == []  # 업체가 보는 뷰에는 나가지 않는다


def make_view_for(app, view, org):
    from app.v3 import service

    return service.get_view(app, view["session"]["id"], org)


def test_fee_messages_say_fee_and_drop_the_sign_note(demo):
    _, waiting, done = demo
    texts = [m["text"] for view in (waiting, done) for t in view["threads"] for m in t["messages"]]
    assert texts and not any("음수" in text for text in texts)
    namhae = thread_of(waiting, NAMHAE)
    by_id = {c["id"]: c for m in namhae["messages"] for c in m["claims"]}
    fee_lines = [
        line
        for m in namhae["messages"]
        if m["type"] in ("offer", "conditional")
        for line in m["text"].splitlines()
        if "처리비는 {{" in line
    ]
    assert fee_lines
    for line in fee_lines:
        cited = [by_id[x.group(1)] for x in PLACEHOLDER.finditer(line)]
        assert cited[0]["role"] == "treatmentFee" and cited[0]["value"] < 0  # 값은 음수 그대로
        assert "매입 단가는" not in line
    saegil = thread_of(waiting, SAEGIL)
    assert any("매입 단가는 {{" in m["text"] for m in saegil["messages"])
    assert not any("처리비는 {{" in m["text"] for m in saegil["messages"])
    # 확정 뒤 문서도 처리비를 크기로 적는다
    request = next(
        d for d in done["documents"] if d["threadId"] == namhae["id"] and d["kind"] == "request"
    )
    assert "합의 처리비 80원/kg" in request["rendered"] and "-80" not in request["rendered"]
    confirm = next(m for m in thread_of(done, NAMHAE)["messages"] if m["type"] == "confirm")
    assert "처리비 {{" in confirm["text"]


def test_asks_fields_are_a_closed_list():
    assert questions.ASK_FIELDS == (
        "material",
        "origin",
        "form",
        "contaminationPct",
        "moisturePct",
        "lotSame",
        "monthlyKg",
        "terms",
    )
    cleaned = questions.clean_asks(
        [
            {"streamId": "st_a", "field": "moisturePct", "extra": "x"},
            {"streamId": "st_a", "field": "moisturePct"},
            {"streamId": "st_a", "field": "가격 320원"},
            "moisturePct",
        ]
    )
    assert cleaned == [{"streamId": "st_a", "field": "moisturePct"}]
