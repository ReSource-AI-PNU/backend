"""두 서버가 같이 쓰는 요청 읽기와 요청 오류 코드(계약 8장)."""

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
