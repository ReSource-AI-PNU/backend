"""두 서버가 같이 쓰는 요청 읽기와 요청 오류 코드(계약 8장)."""

import json

import pytest

from app.v3 import httpio
from app.v3.service import ServiceError


def error_of(call, *args):
    with pytest.raises(ServiceError) as caught:
        call(*args)
    return caught.value.code, caught.value.status


def test_parse_object():
    assert httpio.parse_object(b"") == {} and httpio.parse_object(b"  \n") == {}
    assert httpio.parse_object('{"a": "가"}'.encode()) == {"a": "가"}
    assert error_of(httpio.parse_object, b"{not json") == ("INVALID_JSON", 400)
    assert error_of(httpio.parse_object, b"\xff\xfe") == ("INVALID_JSON", 400)
    for raw in (b"[1]", b'"x"', b"3", b"null"):
        assert error_of(httpio.parse_object, raw) == ("INVALID_BODY", 422)


def test_required_and_answer_body():
    assert httpio.required({"threadId": "th_1"}, "threadId") == "th_1"
    for body in ({}, {"threadId": None}, {"threadId": ""}):
        assert error_of(httpio.required, body, "threadId") == ("INVALID_BODY", 422)
    assert httpio.answer_body({"questionId": "q_1", "value": "unknown"}) == ("q_1", "unknown")
    assert httpio.answer_body({"questionId": "q_1", "value": 0}) == ("q_1", 0)
    for body in ({"value": 1}, {"questionId": "q_1"}, {"questionId": "q_1", "value": None}):
        assert error_of(httpio.answer_body, body) == ("INVALID_BODY", 422)


def test_check_upload(monkeypatch):
    assert httpio.check_upload(b"x") == b"x"
    assert error_of(httpio.check_upload, b"") == ("FILE_REQUIRED", 422)
    assert error_of(httpio.check_upload, None) == ("FILE_REQUIRED", 422)
    monkeypatch.setattr(httpio, "MAX_BODY_BYTES", 3)
    assert error_of(httpio.check_upload, b"abcd") == ("TOO_LARGE", 413)


def test_codes_and_statuses_are_listed():
    made = [httpio.invalid_json(), httpio.invalid_body("x"), httpio.file_required()]
    made.append(httpio.too_large())
    assert {e.code: e.status for e in made} == httpio.REQUEST_ERRORS
    assert all(e.message for e in made)


# ── 요청한 조직: header 모드와 token 모드 ────────────────────────────
# 아래 토큰은 테스트용으로 지어낸 글이다(실제 값이 아니다)
HANBIT_TOKEN = "test-hanbit-" + "a" * 12
SAEGIL_TOKEN = "test-saegil-" + "b" * 12
SAEGIL_NEXT = "test-saegil-" + "c" * 12
TOKENS = {"org_hanbit": HANBIT_TOKEN, "org_saegil": [SAEGIL_TOKEN, SAEGIL_NEXT]}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_org_tokens_reads_json_and_objects():
    expected = (
        ("org_hanbit", HANBIT_TOKEN),
        ("org_saegil", SAEGIL_TOKEN),
        ("org_saegil", SAEGIL_NEXT),
    )
    assert httpio.org_tokens(TOKENS) == expected
    assert httpio.org_tokens(json.dumps(TOKENS)) == expected
    assert httpio.org_tokens(None) == () and httpio.org_tokens("") == ()
    assert httpio.org_tokens({}) == ()


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1, 2]",
        '"just a string"',
        {"org_hanbit": "short"},  # 16자보다 짧다
        {"org_hanbit": 12345678901234567890},
        {"org_hanbit": ""},
        {"org_hanbit": []},
        {"": HANBIT_TOKEN},
        {"org_hanbit": HANBIT_TOKEN, "org_daon": HANBIT_TOKEN},  # 두 조직이 같은 토큰
    ],
)
def test_org_tokens_refuses_bad_settings_without_echoing_the_token(raw):
    with pytest.raises(ValueError) as caught:
        httpio.org_tokens(raw)
    assert HANBIT_TOKEN not in str(caught.value)


def test_match_token_uses_constant_time_comparison(monkeypatch):
    tokens = httpio.org_tokens(TOKENS)
    assert httpio.match_token(HANBIT_TOKEN, tokens) == "org_hanbit"
    assert httpio.match_token(SAEGIL_NEXT, tokens) == "org_saegil"
    assert httpio.match_token(HANBIT_TOKEN[:-1], tokens) is None
    assert httpio.match_token(HANBIT_TOKEN + "x", tokens) is None
    assert httpio.match_token("", tokens) is None
    assert httpio.match_token("한글 토큰", tokens) is None
    calls = []
    real = httpio.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(httpio.hmac, "compare_digest", spy)
    assert httpio.match_token(HANBIT_TOKEN, tokens) == "org_hanbit"
    # 맞는 것을 찾은 뒤에도 끝까지 견준다(몇 번째에서 맞았는지가 시간으로 드러나지 않게)
    assert len(calls) == len(tokens)


def test_header_mode_trusts_the_org_header():
    auth = httpio.make_auth(None)
    assert auth.mode == "header" and auth.scoped is False and auth.tokens == ()
    assert auth.viewer({"X-Org-Id": " org_hanbit "}) == "org_hanbit"
    assert auth.viewer({"x-org-id": "org_saegil"}) == "org_saegil"
    assert auth.viewer({}) == "" and auth.viewer(None) == ""
    assert error_of(lambda: auth.viewer({}, required=True)) == ("ORG_REQUIRED", 401)
    # header 모드에서는 토큰을 보지 않는다(토큰 설정도 읽지 않는다)
    assert auth.viewer({**bearer(HANBIT_TOKEN), "X-Org-Id": "org_daon"}) == "org_daon"
    assert httpio.make_auth("header", "{not json").tokens == ()
    assert "시연 전용" in httpio.HEADER_MODE_WARNING and "조직 경계" in httpio.HEADER_MODE_WARNING


def test_token_mode_maps_the_bearer_token_and_ignores_the_org_header():
    auth = httpio.make_auth("token", TOKENS)
    assert auth.mode == "token" and auth.scoped is True
    assert auth.viewer(bearer(HANBIT_TOKEN)) == "org_hanbit"
    assert auth.viewer({"authorization": f"bearer {SAEGIL_NEXT}"}) == "org_saegil"
    # X-Org-Id를 바꿔도 다른 조직이 되지 않는다
    assert auth.viewer({**bearer(HANBIT_TOKEN), "X-Org-Id": "org_admin"}) == "org_hanbit"
    assert auth.viewer({"X-Org-Id": "org_admin"}) == ""
    assert error_of(lambda: auth.viewer({"X-Org-Id": "org_admin"}, required=True)) == (
        "TOKEN_REQUIRED",
        401,
    )
    for headers in (bearer("wrong-token-000000000000"), bearer(HANBIT_TOKEN.upper())):
        assert error_of(lambda h=headers: auth.viewer(h)) == ("INVALID_TOKEN", 401)
    for raw in ("Basic abc", "Bearer", "Bearer   ", HANBIT_TOKEN):
        assert error_of(lambda r=raw: auth.viewer({"Authorization": r})) == ("TOKEN_REQUIRED", 401)


def test_make_auth_refuses_unknown_modes_and_token_mode_without_tokens():
    for mode, raw in (("entra", TOKENS), ("token", None), ("token", "{}"), ("token", "")):
        with pytest.raises(ValueError):
            httpio.make_auth(mode, raw)
    assert httpio.make_auth(" TOKEN ", TOKENS).mode == "token"
    with pytest.raises(ValueError):
        httpio.Auth("token")
