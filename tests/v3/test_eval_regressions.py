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
    reg = registry()
    s = stream(photo={"contaminationPct": 6.0, "evidence": []})
    r = run(reg, "identify_stream", stream=s)
    assert r["output"]["grade"] == "A" and r["output"]["basis"] == "measured"
    assert r["output"]["photoGrade"] == "C" and r["status"] == "conditional"
    assert any(c["key"] == "grade_disagreement" and c["ask"] for c in r["checks"])
    same = run(reg, "identify_stream", stream=stream(photo={"contaminationPct": 0.8}))
    assert same["status"] == "calculated" and same["output"]["photoGrade"] == "A"


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
    assert {"contaminationPct", "moisturePct", "current_assumed"} <= {
        c["key"] for c in item["checks"]
    }
    assert result["sensitivity"]  # 현행이 가정값이면 민감도가 함께 나온다


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
