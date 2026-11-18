"""두 번째 실제 브라우저 QA(2026-11-18)에서 본 것. 회사와 수치는 모두 가상이다.

- 업체 매입 기준의 형태가 "분쇄품"이고 폐기물의 형태가 "분쇄"이면 같은 형태다. "폐기물은 분쇄 형태이고
  매입 기준은 분쇄품 형태입니다"가 조건으로 올라가던 것을 충족으로 본다.
"""

from __future__ import annotations

from app.v3.judge.common import ambiguous_forms, criterion_checks, quality

STREAM = {"id": "st_a", "orgId": "org_e", "material": "PP", "form": "분쇄"}


def _form(stream: dict, criterion: dict) -> tuple[str, str]:
    checks = criterion_checks(stream, criterion, quality(stream))
    return next((status, text) for key, status, text, _ in checks if key == "form")


def test_the_same_form_written_differently_is_met_not_a_condition():
    assert _form(STREAM, {"form": "분쇄품"}) == ("met", "형태가 매입 기준과 같습니다.")
    assert _form({**STREAM, "form": "펠릿"}, {"form": "펠렛"})[0] == "met"
    assert _form({**STREAM, "form": "압축"}, {"form": "압축품"})[0] == "met"
    # 정말 다른 형태는 그대로 조건부다
    status, text = _form({**STREAM, "form": "가공 전"}, {"form": "분쇄품"})
    assert status == "conditional" and "가공 전" in text


def test_forms_written_differently_do_not_make_criteria_ambiguous():
    vendor = {
        "criteria": [
            {"id": "pc_1", "material": "PP", "form": "분쇄", "unitPriceKrwPerKg": 300},
            {"id": "pc_2", "material": "PP", "form": "분쇄품", "unitPriceKrwPerKg": 280},
        ]
    }
    unknown = {**STREAM, "form": None}
    assert ambiguous_forms(unknown, vendor, quality(unknown)) == []
