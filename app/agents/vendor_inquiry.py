"""사용자 진술을 바탕으로 발송하지 않는 업체 문의 초안을 생성한다."""

from app.agents.pp_questions import ReviewState
from app.agents.pp_resource_spec import _field

_FIELDS = (
    ("material", "재질"),
    ("material_basis", "재질 판단 근거"),
    ("form", "형태"),
    ("contamination", "혼입"),
    ("process", "발생 과정"),
    ("region", "지역"),
    ("quantity", "물량"),
)


def build_vendor_inquiry_draft(state: ReviewState) -> str:
    """입력 상태를 변경하지 않고 검토용 Markdown 문의 초안을 반환한다."""
    lines = [
        "# 자원 처리·매입 조건 문의 초안",
        "",
        "이 문서는 사용자 제공 정보를 바탕으로 작성한 검토용 초안입니다.",
        "",
        "## 자원 정보 요약",
    ]
    lines.extend(f"- {label}: {_field(getattr(state, name))}" for name, label in _FIELDS)
    lines += [
        "",
        "## 업체에 확인할 질문",
        "- 위 재질·형태·혼입 상태의 자원을 취급할 수 있나요?",
        "- 필요한 분리·전처리 및 반입 조건은 무엇인가요?",
        "- 최소 물량과 수거 가능 조건은 무엇인가요?",
        "- 매입 또는 처리비의 단위는 무엇이며 VAT와 물류비가 포함되나요?",
        "- 검토에 필요한 추가 정보는 무엇인가요?",
        "",
        "이 초안은 검토·수정 후 사용해야 하며 실제로 발송되지 않았습니다.",
    ]
    return "\n".join(lines) + "\n"
