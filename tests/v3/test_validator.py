import pytest

from app.v3.a2a.message import Claim, Message
from app.v3.evidence import validator as v
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry

TODAY = "2026-11-17"
PRICE = "doc:org_saegil:매입기준표:PP-A-단가"
SURVEY = "ref:가격조사:PP-펠렛-경남@2026-08"
LAW = "law:시험용법:제1조@2026-01-01"
CALC = "calc:cost_carbon:0123456789ab:월손익"
OTHER = "doc:org_other:위탁계약서:단가"
OLD = "assumption:운송단가@v0"
NOTE = "human:org_hanbit:st_film-형태"
# 시험용 문장이다. 실제 법령 원문이 아니다.
LAW_TEXT = "배출자는 폐기물을 위탁할 때 수탁자의 허가 품목을 확인하여야 한다. 다만, 재투입은 그러하지 아니하다."


@pytest.fixture
def registry():
    def ev(eid, **kw):
        base = {"excerpt": "시험", "source": "시험 자료", "as_of": "2026-11-01"}
        return Evidence(id=eid, kind=eid.split(":")[0], **{**base, **kw})

    return InMemoryRegistry(
        [
            ev(PRICE, value=320, unit="원/kg", access=("org_saegil", "org_hanbit")),
            ev(SURVEY, value=850000, unit="원/t", valid_until="2026-12-31"),
            ev(LAW, excerpt=LAW_TEXT),
            ev(CALC, value=510000, unit="원/월", meta={"inputEvidence": [PRICE]}),
            ev(OTHER, value=250, unit="원/kg", access=("org_other",)),
            ev(OLD, value=1200, unit="원/회", valid_until="2026-10-31"),
            ev(NOTE, value="필름"),
        ]
    )


def num(cid, value, unit, *evidence):
    return Claim(cid, "number", value, evidence, unit)


def check(registry, text, claims, org="org_hanbit", today=TODAY):
    return v.validate_text(text, claims, registry, org_id=org, today=today)


def test_valid_message_passes(registry):
    msg = Message(
        "th_1", "org_saegil", "org_hanbit", "offer", 5,
        "st_sprue 흐름은 kg당 {{price}}에 매입할 수 있습니다. 2026-08 조사가는 {{survey}}입니다. "
        "근거 조항: {{clause}}",
        claims=(
            num("price", 320, "원/kg", PRICE),
            num("survey", 850, "원/kg", SURVEY),
            Claim("clause", "clause", "수탁자의 허가 품목을 확인하여야 한다", (LAW,)),
        ),
    )  # fmt: skip
    result = v.validate_message(msg, registry, today=TODAY)
    assert result.ok, result.to_dict()
    assert result.to_dict() == {"ok": True, "rejections": []}


def test_all_codes_are_declared():
    assert len(v.CODES) == len(set(v.CODES)) == 10


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("단가는 320원입니다.", ["320"]),
        ("월 1,000kg 이상이면 3.5% 할인합니다.", ["1,000", "3.5%"]),
        ("오염도 10 % 이하", ["10 %"]),
        ("손익은 -250원/kg입니다.", ["-250"]),
        ("전각 ３００원도 숫자다.", ["300"]),
        ("최대 12턴, 3개월 뒤 재검토", ["12", "3"]),
        ("7일 안에 회신 바랍니다.", ["7"]),
        ("용량은 5m3입니다.", ["5", "3"]),
        ("1.5배 비쌉니다.", ["1.5"]),
    ],
)
def test_number_outside_placeholder(registry, text, found):
    result = check(registry, text, [])
    assert result.codes == (v.NUMBER_OUTSIDE_PLACEHOLDER,)
    assert result.rejections[0].detail["found"] == found
    assert found[0] in result.rejections[0].message


@pytest.mark.parametrize(
    "text",
    [
        "기준일은 2026-11-16입니다.",
        "기준일은 2026.11.16 또는 2026. 11. 16. 입니다.",
        "2026년 11월 16일에 측정했고 11월 말에 다시 잽니다.",
        "2026/11/16 접수, 2026-08 가격조사 기준, 2026년 조사",
        "지금은 ③ 규정 검토이고 4단계로 넘어갑니다.",
        "흐름 st_sprue_2, 조직 org_hanbit, 스레드 th_12, 문서 doc_3, 세션 ses_1, msg_7, q_2",
        "확인할 것:\n1. 수분\n2) 형태\n 3. 포장",
        "탄소는 tCO2e와 kgCO2e로, 부피는 m3로 적습니다. CO2, CO2e도 같습니다.",
        "재질은 PA6, PA66, PA12이고 먼지는 PM10, PM2.5 기준입니다.",
        "A2A 대화에서 bge-m3 임베딩을 씁니다.",
        "삼백 원이라고 한글로 쓴 수사는 대상이 아닙니다.",
        "숫자가 없는 문장입니다.",
    ],
)
def test_allowed_exceptions(registry, text):
    assert v.find_numbers(text) == []
    assert check(registry, text, []).ok


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("16일까지 회신", ["16"]),
        ("7단계는 없다", ["7"]),
        ("13월은 없다", ["13"]),
        ("1000-20은 날짜가 아니다", ["1000", "-20"]),
        ("XPA66Y는 재질 이름이 아니다", ["66"]),
        ("mst_1은 식별자가 아니다", ["1"]),
        ("문장 가운데 1. 은 목록 번호가 아니다", ["1"]),
    ],
)
def test_exceptions_are_narrow(text, found):
    assert v.find_numbers(text) == found


def test_extra_allowed_tokens(registry):
    text = "모델은 gpt-5-mini입니다."
    assert not check(registry, text, []).ok
    result = v.validate_text(
        text, [], registry, org_id="org_hanbit", today=TODAY,
        allowed_tokens=(*v.ALLOWED_TOKENS, "gpt-5-mini"),
    )  # fmt: skip
    assert result.ok


def test_unknown_placeholder_and_unused_claim(registry):
    result = check(
        registry, "단가는 {{price}}, 운임은 {{freight}}", [num("p", 320, "원/kg", PRICE)]
    )
    assert result.codes == (v.UNKNOWN_PLACEHOLDER, v.UNKNOWN_PLACEHOLDER, v.UNUSED_CLAIM)
    assert [r.claim_id for r in result.rejections] == ["price", "freight", "p"]


def test_claim_without_evidence(registry):
    result = check(registry, "단가는 {{p}}", [num("p", 320, "원/kg")])
    assert result.codes == (v.CLAIM_WITHOUT_EVIDENCE,)


def test_evidence_not_found(registry):
    result = check(registry, "단가는 {{p}}", [num("p", 320, "원/kg", "doc:org_saegil:없음:x")])
    assert result.codes == (v.EVIDENCE_NOT_FOUND,)
    assert result.rejections[0].detail["evidenceId"] == "doc:org_saegil:없음:x"


def test_other_org_evidence_is_forbidden(registry):
    claims = [num("p", 250, "원/kg", OTHER)]
    assert check(registry, "현행 단가는 {{p}}", claims).codes == (v.EVIDENCE_FORBIDDEN,)
    assert check(registry, "현행 단가는 {{p}}", claims, org="org_other").ok
    # 접근 목록에 없는 업체는 배출 기업과 나눈 단가 근거도 쓸 수 없다
    assert check(
        registry, "단가는 {{p}}", [num("p", 320, "원/kg", PRICE)], org="org_namhae"
    ).codes == (v.EVIDENCE_FORBIDDEN,)


def test_expired_evidence(registry):
    claims = [num("f", 1200, "원/회", OLD)]
    result = check(registry, "운임은 {{f}}", claims)
    assert result.codes == (v.EVIDENCE_EXPIRED,)
    assert check(registry, "운임은 {{f}}", claims, today="2026-10-31").ok
    survey = [num("s", 850000, "원/t", SURVEY)]
    assert check(registry, "조사가는 {{s}}", survey, today="2026-12-31").ok
    assert check(registry, "조사가는 {{s}}", survey, today="2027-01-01").codes == (
        v.EVIDENCE_EXPIRED,
    )


def test_value_mismatch(registry):
    result = check(registry, "단가는 {{p}}", [num("p", 330, "원/kg", PRICE)])
    assert result.codes == (v.VALUE_MISMATCH,)
    assert result.rejections[0].detail["evidenceValue"] == 320
    assert "320 원/kg" in result.rejections[0].message


def test_unit_conversion_matches(registry):
    assert check(registry, "조사가는 {{s}}", [num("s", 850, "원/kg", SURVEY)]).ok
    assert check(registry, "조사가는 {{s}}", [num("s", 0.85, "원/kg", SURVEY)]).codes == (
        v.VALUE_MISMATCH,
    )


def test_unit_mismatch(registry):
    assert check(registry, "단가는 {{p}}", [num("p", 320, "원/월", PRICE)]).codes == (
        v.UNIT_MISMATCH,
    )
    assert check(registry, "단가는 {{p}}", [num("p", 320, None, PRICE)]).codes == (v.UNIT_MISMATCH,)
    assert check(registry, "단가는 {{p}}", [num("p", 320, "달러", PRICE)]).codes == (
        v.UNIT_MISMATCH,
    )


def test_calc_value_mismatch(registry):
    assert check(registry, "월 손익은 {{m}}", [num("m", 510000, "원/월", CALC)]).ok
    result = check(registry, "월 손익은 {{m}}", [num("m", 520000, "원/월", CALC)])
    assert result.codes == (v.VALUE_MISMATCH,)


def test_number_claim_needs_numeric_values(registry):
    assert check(
        registry, "단가는 {{p}}", [Claim("p", "number", "320", (PRICE,), "원/kg")]
    ).codes == (v.VALUE_MISMATCH,)
    assert check(registry, "형태는 {{f}}", [num("f", 1, None, NOTE)]).codes == (v.VALUE_MISMATCH,)
    assert check(registry, "조항 {{c}}", [num("c", 1, None, LAW)]).codes == (v.VALUE_MISMATCH,)


def test_secondary_evidence_checked_for_existence_and_access_only(registry):
    ok = [num("m", 510000, "원/월", CALC, PRICE, SURVEY)]
    assert check(registry, "월 손익은 {{m}}", ok).ok
    bad = [num("m", 510000, "원/월", CALC, OTHER, "ref:없음:x@2026-01")]
    assert check(registry, "월 손익은 {{m}}", bad).codes == (
        v.EVIDENCE_FORBIDDEN,
        v.EVIDENCE_NOT_FOUND,
    )


@pytest.mark.parametrize(
    "quote",
    [
        "수탁자의 허가 품목을 확인해야 한다",  # 글자가 다르다
        "수탁자의 허가품목을 확인하여야 한다",  # 공백이 빠졌다
        "수탁자의  허가 품목을 확인하여야 한다",  # 공백이 하나 더 있다
        "수탁자의 허가 품목을 확인하여야 한다 ",  # 끝에 공백
        "확인하여야 한다. 재투입은",  # 이어지지 않는 부분을 붙였다
        "",
    ],
)
def test_quote_mismatch(registry, quote):
    result = check(registry, "근거: {{c}}", [Claim("c", "clause", quote, (LAW,))])
    assert result.codes == (v.QUOTE_MISMATCH,)


def test_quote_must_be_string_and_exact_substring(registry):
    assert check(registry, "근거: {{c}}", [Claim("c", "clause", LAW_TEXT, (LAW,))]).ok
    assert check(registry, "근거: {{c}}", [Claim("c", "clause", "다만, 재투입은", (LAW,))]).ok
    assert check(registry, "근거: {{c}}", [Claim("c", "clause", 1, (LAW,))]).codes == (
        v.QUOTE_MISMATCH,
    )


def test_claims_may_be_dicts_and_messages_are_korean(registry):
    claims = [{"id": "p", "kind": "number", "value": 999, "unit": "원/kg", "evidence": [PRICE]}]
    result = check(registry, "단가는 {{ p }}이고 참고로 5입니다.", claims)
    assert result.codes == (v.NUMBER_OUTSIDE_PLACEHOLDER, v.VALUE_MISMATCH)
    data = result.to_dict()
    assert data["ok"] is False
    assert data["rejections"][1]["claimId"] == "p"
    assert all(r["message"].endswith(("세요.", "니다.")) for r in data["rejections"])


def test_system_caller_sees_everything(registry):
    assert check(registry, "현행 단가는 {{p}}", [num("p", 250, "원/kg", OTHER)], org=None).ok


def test_message_uses_sender_org(registry):
    msg = Message(
        "th_1", "org_hanbit", "org_saegil", "inquiry", 4, "다른 회사는 {{p}}에 처리합니다.",
        claims=(num("p", 250, "원/kg", OTHER),),
    )  # fmt: skip
    assert v.validate_message(msg, registry, today=TODAY).codes == (v.EVIDENCE_FORBIDDEN,)
