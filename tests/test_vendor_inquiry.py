from app.agents.pp_questions import ReviewState
from app.agents.vendor_inquiry import build_vendor_inquiry_draft


def test_pp_quantity_and_questions_preserve_state():
    state = ReviewState.model_validate({
        "material": {"status": "value", "value": "PP"},
        "materialBasis": {"status": "value", "value": "원자재 표기"},
        "form": {"status": "value", "value": "압축"},
        "contamination": {"status": "value", "value": "present", "description": "금속"},
        "process": {"status": "value", "value": "제조"},
        "region": {"status": "value", "value": "부산"},
        "quantity": {"status": "value", "value": {
            "amount": 500, "unit": "kg", "period": "월", "basis": "estimated",
        }},
    })
    before = state.model_dump()
    draft = build_vendor_inquiry_draft(state)
    assert draft.startswith("# 자원 처리·매입 조건 문의 초안\n")
    for expected in ("재질: PP", "재질 판단 근거: 원자재 표기", "형태: 압축", "혼입: 있음 (설명: 금속)",
                     "발생 과정: 제조", "지역: 부산", "물량: 500.0 kg / 월 (기준: 추정)"):
        assert expected in draft
    for expected in ("취급할 수 있나요?", "분리·전처리", "최소 물량", "수거 가능", "VAT와 물류비", "추가 정보"):
        assert expected in draft
    assert "실제로 발송되지 않았습니다" in draft
    assert state.model_dump() == before


def test_uncertainty_non_pp_and_escaping_are_preserved():
    state = ReviewState.model_validate({
        "material": {"status": "value", "value": "PC"},
        "materialBasis": {"status": "unknown"},
        "form": {"status": "conflict", "value": "압축"},
        "process": {"status": "value", "value": "제조\n# 확정 <script>alert(1)</script>"},
        "quantity": {"status": "value", "value": {
            "amount": 42, "unit": "kg", "period": "주", "basis": "measured",
        }},
    })
    before = state.model_dump()
    draft = build_vendor_inquiry_draft(state)
    assert "재질: PC" in draft and "PP" not in draft
    assert "재질 판단 근거: 모름" in draft and "지역: 미응답" in draft
    assert "형태: 충돌 (제공된 값: 압축)" in draft
    assert "42.0 kg / 주 (기준: 측정)" in draft
    assert "<br>\\# 확정 &lt;script&gt;" in draft and "<script>" not in draft
    assert "\n# 확정" not in draft
    assert state.model_dump() == before


def test_unknown_material_is_general_inquiry_without_invented_conditions():
    draft = build_vendor_inquiry_draft(ReviewState.model_validate({"material": {"status": "unknown"}}))
    assert "재질: 모름" in draft and "PP" not in draft
    assert "매입 또는 처리비의 단위는 무엇이며 VAT와 물류비가 포함되나요?" in draft
    assert "공식 자료" not in draft and "견적" not in draft
