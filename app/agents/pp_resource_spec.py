"""사용자 진술과 공식 자료를 구분하는 저장 없는 자원 명세 초안."""

import html
import re

from app.agents.pp_evidence import compare_pp_evidence
from app.agents.pp_questions import (
    ContaminationField,
    InputField,
    Quantity,
    QuantityField,
    ReviewState,
)

_FIELDS = (
    ("material", "재질"),
    ("material_basis", "재질 판단 근거"),
    ("form", "형태"),
    ("contamination", "혼입"),
    ("separation", "분리·보관"),
    ("process", "발생 과정"),
    ("sorting", "선별"),
    ("color", "색상"),
    ("trade_stage", "거래 단계"),
    ("region", "지역"),
    ("quantity", "물량"),
)
_STATUS = {"unanswered": "미응답", "unknown": "모름", "conflict": "충돌"}
_BASIS = {"measured": "측정", "estimated": "추정", "unknown": "모름"}
_FINDING = {"match": "일치", "different": "차이 있음", "unconfirmed": "미확인"}


def _text(value: str) -> str:
    """사용자 입력이 Markdown 구조나 HTML로 해석되지 않도록 표시한다."""
    escaped = html.escape(value, quote=False)
    escaped = re.sub(r"([\\`*_\[\]{}()#+.!|>~-])", r"\\\1", escaped)
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _quantity(value: Quantity) -> str:
    return f"{value.amount} {_text(value.unit)} / {_text(value.period)} (기준: {_BASIS[value.basis]})"


def _field(field: InputField | ContaminationField | QuantityField) -> str:
    result = _STATUS.get(field.status, "")
    if field.value is not None:
        if isinstance(field.value, Quantity):
            value = _quantity(field.value)
        elif isinstance(field, ContaminationField):
            value = {"present": "있음", "absent": "없음으로 파악"}[field.value]
        else:
            value = _text(field.value)
        result = f"{result} (제공된 값: {value})" if result else value
    if isinstance(field, ContaminationField) and field.description is not None:
        result += f" (설명: {_text(field.description)})"
    return result


def build_resource_spec_draft(state: ReviewState) -> str:
    """ReviewState를 수정하지 않고 근거가 포함된 Markdown 초안을 반환한다."""
    result = compare_pp_evidence(state)
    lines = [
        "# 자원 명세 초안",
        "",
        "이 문서의 입력 정보는 사용자가 제공한 진술이며 품질 인증, 견적 또는 법적 판정이 아닙니다.",
        "",
        "## 사용자 제공 정보",
    ]
    for name, label in _FIELDS:
        lines.append(f"- {label}: {_field(getattr(state, name))}")

    lines += ["", "## 미확인·충돌 사항"]
    unresolved = [(label, getattr(state, name)) for name, label in _FIELDS]
    unresolved = [(label, field) for label, field in unresolved if field.status != "value"]
    lines.extend(f"- {label}: {_field(field)}" for label, field in unresolved)
    if not unresolved:
        lines.append("- 입력 상태에 미응답·모름·충돌 항목이 없습니다.")

    lines += ["", "## 관련 공식 자료와 적용 한계"]
    if not result.comparisons:
        lines.append("- 입력 재질에 적용할 PP 공식 자료를 제시하지 않습니다.")
        lines.append("- 입력 재질과 해당 재질의 자료·적용 조건을 별도로 확인해야 합니다.")
    else:
        lines.append("아래는 공식 자료의 조사 조건이며, 사용자 입력에 대한 인증이나 견적이 아닙니다.")
        for comparison in result.comparisons:
            evidence = comparison.evidence
            source = evidence.criteria
            lines.append(
                f"- {_text(evidence.form)}: {_text(source.document)} "
                f"(기준월 {_text(evidence.reference_month)}, "
                f"PDF {source.pdf_page}쪽 / 인쇄 {source.printed_page}쪽). "
                f"[원문]({source.url})"
            )
            lines.append(
                f"  - 조사 조건: {_text(evidence.processing)}, {_text(evidence.quality)}, "
                f"{_text(evidence.trade_stage)}, {_text(evidence.region)}"
            )
            lines.append(
                f"  - 입력과 대조: 형태 {_FINDING[comparison.form.status]}, "
                f"지역 {_FINDING[comparison.region.status]}, "
                f"품질 {_FINDING[comparison.quality.status]}, "
                f"거래 단계 {_FINDING[comparison.trade_stage.status]}"
            )
        lines.extend(f"- 적용 한계: {_text(limit)}" for limit in result.limitations)
    return "\n".join(lines) + "\n"
