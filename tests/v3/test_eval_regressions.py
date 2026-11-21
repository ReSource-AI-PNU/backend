"""사례 평가(8단계)에서 찾아 고친 문제의 회귀 테스트. 회사와 수치는 모두 가상이다."""

from app.v3 import service
from app.v3.a2a.message import Message
from app.v3.eval import runner
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.validator import find_numbers, name_tokens, validate_message
from tests.v3.judge_fixtures import registry, run, stream

TODAY = "2026-11-17"


def world(streams, vendors, **more):
    return {
        "id": "T",
        "world": {"streams": streams, "vendors": vendors},
        "expect": {},
        "forbidden": [],
        **more,
    }


def pp(**over):
    base = {
        "id": "st_a",
        "name": "PP 압축품",
        "material": "PP",
        "origin": "PIR",
        "form": "압축",
        "monthlyKg": 500,
        "measure": {"contaminationPct": 1.0, "moisturePct": 0.3},
    }
    return {**base, **over}


def buyer(**over):
    base = {
        "orgId": "org_v1",
        "permits": [{"wasteItem": "폐플라스틱", "materials": ["PP", "PE"]}],
        "criteria": [
            {
                "id": "pc_pp_b",
                "material": "PP",
                "maxContaminationPct": 8,
                "maxMoisturePct": 2,
                "unitPriceKrwPerKg": 200,
                "minMonthlyKg": 300,
            }
        ],
    }
    return {**base, **over}


def played(case):
    session = runner._Session(case, {})
    session.run()
    return session


# ── 이름에 든 숫자 ───────────────────────────────────────────────────


def test_names_with_digits_are_not_numbers_but_bare_numbers_still_are():
    names = ["(주)제2공장", "PP 2차 분쇄품", "320", "1,000", "(주)가상정밀"]
    assert name_tokens(names) == ("(주)제2공장", "PP 2차 분쇄품")  # 숫자만 있는 이름은 받지 않는다
    reg = InMemoryRegistry()
    hello = Message(
        thread_id="th_1",
        sender="org_a",
        recipient="org_b",
        type="inquiry",
        stage=4,
        text="(주)제2공장 담당자께, PP 2차 분쇄품을 문의드립니다.",
    )
    assert validate_message(hello, reg, today=TODAY).codes == ("NUMBER_OUTSIDE_PLACEHOLDER",)
    assert validate_message(hello, reg, today=TODAY, names=names).ok
    sneaky = Message(
        thread_id="th_1",
        sender="org_a",
        recipient="org_b",
        type="inquiry",
        stage=4,
        text="(주)제2공장 담당자께, 단가는 2차 협의에서 320원으로 하겠습니다.",
    )
    result = validate_message(sneaky, reg, today=TODAY, names=names)
    assert not result.ok and find_numbers(sneaky.text) == ["2", "2", "320"]
    assert result.rejections[0].detail["found"] == ["2", "320"]  # 이름 밖의 숫자는 그대로 거부


def test_a_vendor_or_stream_named_with_a_digit_can_still_negotiate():
    case = world([pp(name="PP 2차 분쇄품")], [buyer(name="(주)제3자원")], approve=True)
    session = played(case)
    thread = session.view["threads"][0]
    assert thread["status"] == "confirmed" and thread["messages"][0]["type"] == "inquiry"
    assert "(주)제3자원" in thread["messages"][0]["text"]
    assert len(session.view["documents"]) == 3  # 요청서, 이행 계획서, 위탁 계약서 초안
    assert all(v["ok"] for v in session.validated.values())


# ── 측정 등급과 사진 추정 등급 ───────────────────────────────────────


def test_identify_compares_measured_and_photo_grade_without_an_asked_moisture():
    """측정 수분이 있으면 사진 판정을 위해 수분을 다시 묻지 않는다.

    2026-11-21 정책: 공통 등급 기준을 검증하지 못해 측정·사진 어느 쪽으로도 공통 등급을 내지 않는다(산정 보류).
    그래서 두 등급을 견주는 grade_disagreement도 없지만, 측정 수분이 있는데 사진만으로는 등급을 못 낸다며 수분을 묻는
    질문이 나가면 안 된다(이 시험이 막던 회귀)."""
    reg = registry()
    for photo in ({"contaminationPct": 6.0, "evidence": []}, {"contaminationPct": 0.8}):
        r = run(reg, "identify_stream", stream=stream(photo=photo))
        assert r["output"]["grade"] is None and r["output"]["photoGrade"] is None
        assert r["output"]["basis"] == "measured"
        assert not any(c["key"] in ("grade_disagreement", "moisture") for c in r["checks"])
        assert not any(c.get("ask") for c in r["checks"])


# ── 결과 한 장 ───────────────────────────────────────────────────────


def test_route_checks_and_sensitivity_survive_an_answer_given_mid_conversation():
    # 업체가 수분을 물어 담당자 질문이 대화 도중에 나가고, 그 답으로 흐름이 바뀐다
    session = played(world([pp(measure=None)], [buyer()]))
    result = session.view["result"]
    item = result["assignments"][0]
    assert [q["field"] for q in session.view["questions"]] == [
        "contaminationPct",
        "currentCost",
        "moisturePct",
    ]
    assert item["status"] == "conditional" and item["vendorOrgId"] == "org_v1"
    checks = {c["key"]: c["status"] for c in item["checks"]}
    assert checks["contaminationPct"] == checks["moisturePct"] == "conditional"
    # 현행 처리비는 담당자가 답한 값이다(평가 세계 기본 답). 가정값이 아니므로 가정값 민감도는 나오지 않는다(2026-11-21 정책)
    assert item["currentAssumed"] is False and "current_assumed" not in checks
    assert result["sensitivity"] == []


def test_a_stream_left_unassigned_because_of_unknown_values_is_not_shown_as_decided():
    case = world([pp(material=None)], [buyer()], answers={"st_a.material": "unknown"})
    session = played(case)
    item = session.view["result"]["assignments"][0]
    assert item["vendorOrgId"] is None and item["status"] == "needs_information"
    assert [c["key"] for c in item["checks"]] == ["unresolved"]
    assert "재질" in item["checks"][0]["text"]
    assert session.view["result"]["status"] == "needs_information"
    stages = {s["stage"]: s["status"] for s in session.view["stages"]}
    assert stages[6] == "needs_check"
    # 모르는 값이 없는데 받을 업체가 없으면 현행 유지로 확정한다
    wood = pp(wasteItem="폐목재", material="목재")
    kept = played(world([wood], [buyer()])).view["result"]["assignments"][0]
    assert kept["vendorOrgId"] is None and kept["status"] == "calculated"
    assert service.get_view  # 서비스 뷰를 거쳐 본다
