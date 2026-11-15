from app.agents.pp_evidence import CATALOG, compare_pp_evidence
from app.agents.pp_questions import InputField, ReviewState


def value(text):
    return InputField(status="value", value=text)


def test_unprocessed_pp_differs_from_all_evidence_and_preserves_input():
    state = ReviewState(material=value("PP"), form=value("가공 전"), region=value("부산"))
    before = state.model_dump()
    result = compare_pp_evidence(state)
    assert result.status == "compared"
    assert all(
        c.form.status == "different" and c.region.status == "match" for c in result.comparisons
    )
    assert state.model_dump() == before
    assert result.input_summary["form"]["value"] == "가공 전"


def test_same_form_does_not_imply_quality_or_trade_stage():
    result = compare_pp_evidence(
        ReviewState(
            material=value("PP"),
            form=value("세척·파쇄"),
            region=value("경상남도"),
            color=value("잡색"),
            trade_stage=value("제품 제조업체에 판매"),
        )
    )
    compressed, flake, pellet = result.comparisons
    assert [c.form.status for c in (compressed, flake, pellet)] == [
        "different",
        "match",
        "different",
    ]
    assert flake.quality.status == flake.trade_stage.status == "unconfirmed"
    assert flake.evidence.criteria.pdf_page == 27
    assert flake.evidence.price_table.printed_page == 24


def test_unknown_conflict_and_ambiguous_values_remain_unconfirmed():
    result = compare_pp_evidence(
        ReviewState(
            material=InputField(status="unknown"),
            form=InputField(status="conflict"),
            region=value("부산 근처일 수도 있음"),
        )
    )
    assert result.status == "insufficient_information"
    assert result.input_summary["material"]["status"] == "unknown"
    assert result.input_summary["form"]["status"] == "conflict"
    assert all(
        c.material.status == c.form.status == c.region.status == "unconfirmed"
        for c in result.comparisons
    )
    assert (
        compare_pp_evidence(ReviewState(material=value("PP 또는 PE"))).status
        == "insufficient_information"
    )
    assert compare_pp_evidence(ReviewState()).comparisons[0].form.status == "unconfirmed"


def test_explicit_other_material_is_unsupported():
    result = compare_pp_evidence(ReviewState(material=value("PE")))
    assert result.status == "unsupported"
    assert not result.comparisons


def test_catalog_sources_and_conditions():
    assert [e.criteria.pdf_page for e in CATALOG] == [21, 27, 43]
    assert [e.price_table.pdf_page for e in CATALOG] == [22, 28, 44]
    assert all(
        e.reference_month == "2026-09" and e.vat == "포함" and e.logistics == "제외"
        for e in CATALOG
    )
    assert all(
        e.criteria.url and e.faq.url and e.general_criteria.printed_page == 3 for e in CATALOG
    )
    result = compare_pp_evidence(ReviewState(material=value("PP"), region=value("서울")))
    assert all(c.region.status == "different" for c in result.comparisons)
    assert result.limitations and result.follow_up
