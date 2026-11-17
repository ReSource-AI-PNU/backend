"""평가 사례 세트의 형식. 사례의 회사, 업체, 수치는 모두 가상이다."""

import json
import re

import pytest

from app.v3.eval import loader
from app.v3.eval.loader import CASE_DIR, check_case, check_cases, load_cases

CASES = load_cases()
PP_CASES = [f"C{n:02d}" for n in range(1, 11)]
# 지시가 요구한 주제. 사례의 tags에 하나라도 있어야 한다
REQUIRED_TOPICS = {
    "재질": ["PP", "PE", "LDPE", "PP+ABS", "금속", "목재"],
    "출처": ["PIR", "PCR"],
    "측정값 유무": ["측정값 있음", "측정값 없음"],
    "수분·물량 모름": ["수분 모름", "물량 모름"],
    "혼입": ["혼입"],
    "허가 불일치": ["허가 불일치"],
    "최소 물량": ["최소 물량 미달", "합산 통과"],
    "적재 부피 초과": ["적재 부피 초과"],
    "현행 처리 유무": ["현행 있음", "현행 없음"],
    "근거 만료": ["근거 만료"],
    "다른 조직 근거": ["다른 조직 근거"],
    "가드레일": ["가드레일"],
    "주입 문서": ["주입"],
}


def test_there_are_fifty_cases_with_unique_ids():
    ids = [c["id"] for c in CASES]
    assert len(ids) == 50 and len(set(ids)) == 50
    assert ids == [f"C{n:02d}" for n in range(1, 51)]
    assert all(re.fullmatch(r"C\d{2}", i) for i in ids)


def test_every_case_matches_the_schema():
    assert check_cases(CASES) == []
    for case in CASES:
        assert case["expect"] and case["forbidden"], case["id"]
        assert all(item["why"] for item in case["forbidden"]), case["id"]


def test_draft_marks_follow_the_review_state():
    by_id = {c["id"]: c for c in CASES}
    # C01~C10은 원문의 상태(제품 정책 초안)를 따르고 원문 위치를 적는다
    for case_id in PP_CASES:
        case = by_id[case_id]
        assert case["review"] == loader.REVIEW_POLICY_DRAFT
        assert case["source"] == f"docs/c-track/pp-validation-cases.md#{case_id}"
    # 새로 만든 40건은 전문가 검토 전(초안)이다
    new = [c for c in CASES if c["id"] not in PP_CASES]
    assert len(new) == 40 and {c["review"] for c in new} == {loader.REVIEW_DRAFT}
    assert all("초안" in c["review"] for c in CASES)  # 검토를 마친 사례는 아직 없다


def test_all_numbers_are_marked_synthetic():
    assert all(c["synthetic"] is True for c in CASES)
    for path in sorted(CASE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["synthetic"] is True and "가상" in data["note"], path.name
    # 실제 시연 조직이나 실제 업체 이름을 쓰지 않는다
    dumped = json.dumps(CASES, ensure_ascii=False)
    assert "org_hanbit" not in dumped and "한빛정밀" not in dumped


def test_the_pp_cases_keep_the_original_ten_titles_in_order():
    source = (CASE_DIR.parents[3] / "docs/c-track/pp-validation-cases.md").read_text(
        encoding="utf-8"
    )
    headings = re.findall(r"^### (C\d{2}) ", source, flags=re.MULTILINE)
    assert headings == PP_CASES
    assert [c["id"] for c in CASES[:10]] == PP_CASES


def test_required_topics_are_covered():
    tags = {tag for case in CASES for tag in case["tags"]}
    for topic, wanted in REQUIRED_TOPICS.items():
        missing = [w for w in wanted if w not in tags]
        assert not missing, (topic, missing)


def test_open_cases_explain_why():
    for case in CASES:
        if case.get("status") == "open":
            assert len(case["openNote"]) > 30, case["id"]


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"review": "확정"}, "review"),
        ({"synthetic": False}, "synthetic"),
        ({"forbidden": []}, "forbidden"),
        ({"expect": {}}, "expect"),
        ({"expect": {"result": {"status": "done"}}}, "판정 상태"),
        ({"forbidden": [{"kind": "anything", "why": "x"}]}, "금지 종류"),
        ({"forbidden": [{"kind": "money", "stream": "st_a"}]}, "이유"),
        ({"status": "open"}, "openNote"),
        ({"extra": 1}, "모르는 키"),
    ],
)
def test_check_case_rejects_malformed_cases(patch, needle):
    broken = {**{k: v for k, v in CASES[3].items() if k != "file"}, **patch}
    problems = check_case(broken)
    assert problems and any(needle in p for p in problems), problems


def test_duplicate_ids_are_reported():
    assert any("겹친다" in p for p in check_cases([CASES[0], CASES[0]]))
