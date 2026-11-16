"""선정된 공식 문서의 정적 사실과 ReviewState 기반의 순수 규칙 비교.

가격 적용이나 거래 적합성 판단을 제공하지 않는다.
"""

from dataclasses import dataclass
from typing import Literal

from app.agents.pp_questions import InputField, ReviewState


@dataclass(frozen=True)
class Source:
    document: str
    url: str
    pdf_page: int
    printed_page: int


@dataclass(frozen=True)
class Evidence:
    id: str
    reference_month: str
    material: str
    form: str
    quality: str
    processing: str
    trade_stage: str
    region: str
    vat: str
    logistics: str
    criteria: Source
    price_table: Source
    general_criteria: Source
    detailed_criteria: Source
    faq: Source


_URL = "https://brc.beco.or.kr/kor/view.do?idx=7544&no=1653&pageIndex=1&sv=&sw=&view=view"
_DOC = "한국환경공단 2026년 9월 재활용가능자원 가격조사"
_FAQ_URL = (
    "https://brc.beco.or.kr/board/downFile.do?siteId=kor&boardId=UXBID_00000000000265"
    "&nowUrl=%2Fkor%2Fview.do%3Fno%3D1653&pageIndex=1&idxArr=7544&fidx=1959"
)


def _source(pdf: int, printed: int) -> Source:
    return Source(_DOC, _URL, pdf, printed)


def _evidence(id: str, form: str, processing: str, stage: str, page: int) -> Evidence:
    return Evidence(
        id=id,
        reference_month="2026-09",
        material="PP",
        form=form,
        quality="잡색(생활계 혼합)",
        processing=processing,
        trade_stage=stage,
        region="경남 권역(부산·울산·경남)",
        vat="포함",
        logistics="제외",
        criteria=_source(page, page - 4),
        price_table=_source(page + 1, page - 3),
        general_criteria=_source(7, 3),
        detailed_criteria=_source(70, 66),
        faq=Source("재활용가능자원 가격조사 FAQ·세부 조사기준", _FAQ_URL, 1, 64),
    )


CATALOG = (
    _evidence(
        "keco-2026-09-pp-compressed",
        "압축",
        "선별 후 압축",
        "압축품을 후속 제품 제조업체에 판매",
        21,
    ),
    _evidence(
        "keco-2026-09-pp-flake",
        "세척·파쇄",
        "세척 및 파쇄",
        "세척 파쇄품을 후속 제품 제조업체에 판매",
        27,
    ),
    _evidence(
        "keco-2026-09-pp-pellet", "펠렛", "세척 후 용융·압출", "펠렛을 제품 제조업체에 판매", 43
    ),
)


@dataclass(frozen=True)
class Finding:
    status: Literal["match", "different", "unconfirmed"]
    reason: str


@dataclass(frozen=True)
class Comparison:
    evidence: Evidence
    material: Finding
    form: Finding
    region: Finding
    quality: Finding
    trade_stage: Finding


@dataclass(frozen=True)
class ComparisonResult:
    status: Literal["compared", "insufficient_information", "unsupported"]
    input_summary: dict
    comparisons: tuple[Comparison, ...]
    limitations: tuple[str, ...]
    follow_up: tuple[str, ...]


_PP = {"PP", "폴리프로필렌", "POLYPROPYLENE"}
_OTHER = {
    "PE",
    "PET",
    "PVC",
    "PS",
    "ABS",
    "HDPE",
    "LDPE",
    "PC",
    "POLYCARBONATE",
    "폴리카보네이트",
    "PA",
    "PA6",
    "PA66",
    "NYLON",
    "나일론",
    "폴리아미드",
    "POM",
    "PMMA",
    "PU",
    "EVA",
    "PBT",
    "PLA",
    "아크릴",
    "폴리우레탄",
    "폴리에틸렌",
    "폴리에스터",
    "폴리스티렌",
    "금속",
    "철",
    "알루미늄",
    "유리",
    "종이",
}
_GYEONGNAM = {"부산", "부산광역시", "울산", "울산광역시", "경남", "경상남도"}
_OTHER_REGIONS = {
    "서울",
    "서울특별시",
    "인천",
    "인천광역시",
    "경기",
    "경기도",
    "강원",
    "강원도",
    "강원특별자치도",
    "대전",
    "대전광역시",
    "세종",
    "세종특별자치시",
    "충북",
    "충청북도",
    "충남",
    "충청남도",
    "대구",
    "대구광역시",
    "경북",
    "경상북도",
    "광주",
    "광주광역시",
    "전북",
    "전라북도",
    "전북특별자치도",
    "전남",
    "전라남도",
    "제주",
    "제주특별자치도",
}


def _unconfirmed(field: InputField, label: str) -> Finding:
    return Finding(
        "unconfirmed", f"{label}: 입력 상태가 {field.status}이거나 값을 확실히 해석할 수 없습니다."
    )


def compare_pp_evidence(state: ReviewState) -> ComparisonResult:
    """입력을 변경하지 않고 문서 사실과 항목별 비교 판단을 반환한다."""
    state = ReviewState.model_validate(state.model_dump())
    summary = state.model_dump(exclude={"batches"})
    material = state.material
    material_value = material.value.strip().upper() if material.status == "value" else ""
    limits = (
        "2026년 9월 조사 자료이며 최신 가격이나 견적이 아닙니다. 조사값은 시점·품질 등에 따라 실제 거래가격과 다를 수 있습니다.",
        "조사 조건은 VAT 포함, 물류비 제외입니다. 지역 일치는 조사 권역만 뜻하며 개별 거래조건 일치가 아닙니다.",
        "선정 문서 기반 규칙 비교이며 단가 적용, 수익 또는 법적 적합성 판단이 아닙니다.",
    )
    follow_up = (
        "재질과 그 판단 근거, 가공 형태 및 발생 지역을 확인하세요.",
        "품질·선별·색상, 거래 단계와 거래 기준일 및 VAT·물류비 조건을 확인하세요.",
    )
    if material_value in _OTHER:
        return ComparisonResult("unsupported", summary, (), limits, follow_up)
    if material_value not in _PP:
        return ComparisonResult("insufficient_information", summary, (), limits, follow_up)
    material_finding = Finding("match", "입력 재질 PP가 문서의 PP와 같습니다.")
    comparisons = []
    for evidence in CATALOG:
        form = state.form
        if form.status == "value":
            same = form.value == evidence.form
            form_finding = Finding(
                "match" if same else "different",
                f"입력 형태 {form.value}와 문서 형태 {evidence.form}는 {'같습니다' if same else '다릅니다'}.",
            )
        else:
            form_finding = _unconfirmed(form, "형태")
        region = state.region
        region_value = region.value.strip() if region.status == "value" else ""
        if region_value in _GYEONGNAM:
            region_finding = Finding("match", "입력 지역은 문서의 경남 조사 권역에 속합니다.")
        elif region_value in _OTHER_REGIONS:
            region_finding = Finding("different", "입력 지역은 경남 조사 권역 밖입니다.")
        else:
            region_finding = _unconfirmed(region, "지역")
        comparisons.append(
            Comparison(
                evidence,
                material_finding,
                form_finding,
                region_finding,
                Finding(
                    "unconfirmed", "형태만으로 잡색·생활계 혼합 등 품질 조건을 확인할 수 없습니다."
                ),
                Finding(
                    "unconfirmed", "자유서술과 형태만으로 문서의 판매 단계를 확정할 수 없습니다."
                ),
            )
        )
    return ComparisonResult("compared", summary, tuple(comparisons), limits, follow_up)
