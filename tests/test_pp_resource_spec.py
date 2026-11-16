import pytest

from app.agents.pp_evidence import compare_pp_evidence
from app.agents.pp_questions import ReviewState
from app.agents.pp_resource_spec import build_resource_spec_draft


def test_pp_draft_preserves_user_values_quantity_and_evidence():
    state = ReviewState.model_validate(
        {
            "material": {"status": "value", "value": "PP"},
            "materialBasis": {"status": "value", "value": "원자재 표기"},
            "form": {"status": "value", "value": "압축"},
            "contamination": {"status": "value", "value": "present", "description": "금속"},
            "region": {"status": "value", "value": "부산"},
            "quantity": {
                "status": "value",
                "value": {"amount": 1234567.89, "unit": "kg", "period": "월", "basis": "estimated"},
            },
        }
    )
    before = state.model_dump()
    draft = build_resource_spec_draft(state)
    assert draft.startswith("# 자원 명세 초안\n")
    assert "사용자가 제공한 진술" in draft and "품질 인증, 견적 또는 법적 판정이 아닙니다" in draft
    assert "재질: PP" in draft and "재질 판단 근거: 원자재 표기" in draft
    assert "혼입: 있음 (설명: 금속)" in draft
    assert "1234567.89 kg / 월 (기준: 추정)" in draft
    assert "품질 미확인" in draft and "거래 단계 미확인" in draft
    for comparison in compare_pp_evidence(state).comparisons:
        source = comparison.evidence.criteria
        assert source.document in draft
        assert f"PDF {source.pdf_page}쪽 / 인쇄 {source.printed_page}쪽" in draft
        assert source.url in draft
    assert state.model_dump() == before


def test_unanswered_unknown_conflict_remain_distinct_and_conflicting_value_is_not_confirmed():
    state = ReviewState.model_validate(
        {
            "material": {"status": "value", "value": "PP"},
            "materialBasis": {"status": "unknown"},
            "form": {"status": "unanswered"},
            "region": {"status": "conflict", "value": "부산"},
            "contamination": {"status": "unknown", "description": "성분 확인 필요"},
        }
    )
    before = state.model_dump()
    draft = build_resource_spec_draft(state)
    assert "재질 판단 근거: 모름" in draft
    assert "형태: 미응답" in draft
    assert "지역: 충돌 (제공된 값: 부산)" in draft
    assert "혼입: 모름 (설명: 성분 확인 필요)" in draft
    assert "지역 미확인" in draft
    assert state.model_dump() == before


@pytest.mark.parametrize(
    "material", [{"status": "value", "value": "PC"}, {"status": "unknown"}]
)
def test_unconfirmed_or_non_pp_has_no_pp_source(material):
    state = ReviewState.model_validate({"material": material})
    before = state.model_dump()
    draft = build_resource_spec_draft(state)
    assert "입력 재질에 적용할 PP 공식 자료를 제시하지 않습니다" in draft
    assert "한국환경공단" not in draft and "[원문]" not in draft
    assert state.model_dump() == before


def test_user_text_cannot_inject_markdown_or_html_structure():
    state = ReviewState.model_validate(
        {"material": {"status": "value", "value": "PP"}, "process": {
            "status": "value", "value": "제조\n# 인증 완료 <script>"
        }}
    )
    draft = build_resource_spec_draft(state)
    assert "<br>\\# 인증 완료 &lt;script&gt;" in draft
    assert "\n# 인증 완료" not in draft and "<script>" not in draft
