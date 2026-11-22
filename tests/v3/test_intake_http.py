"""intake HTTP 호출: https만 부르고, 내부 서비스(V3_INTERNAL_HTTP_HOSTS의 한 단어 이름, 사설 IPv4 주소)만 http로 부른다."""

import pytest

from app.v3.intake import _http


def test_https_is_always_allowed_with_the_short_timeout():
    assert _http.timeout_for("https://endpoint.example/score", {}) == _http.TIMEOUT


def test_plain_http_is_refused_unless_the_host_is_listed():
    with pytest.raises(ValueError):
        _http.timeout_for("http://detector:8000/score", {})
    with pytest.raises(ValueError):
        _http.timeout_for("http://other:8000/score", {"V3_INTERNAL_HTTP_HOSTS": "detector"})
    with pytest.raises(ValueError):
        _http.timeout_for("ftp://detector/score", {"V3_INTERNAL_HTTP_HOSTS": "detector"})


def test_listed_internal_service_gets_http_and_the_long_timeout():
    env = {"V3_INTERNAL_HTTP_HOSTS": " Detector , grade "}
    assert _http.timeout_for("http://detector:8000/score", env) == _http.INTERNAL_TIMEOUT
    assert _http.timeout_for("http://grade/score", env) == _http.INTERNAL_TIMEOUT


def test_public_names_cannot_be_listed():
    env = {"V3_INTERNAL_HTTP_HOSTS": "endpoint.example,detector"}
    assert _http.internal_hosts(env) == frozenset({"detector"})
    with pytest.raises(ValueError):
        _http.timeout_for("http://endpoint.example/score", env)


def test_default_transport_refuses_before_opening_a_connection(monkeypatch):
    monkeypatch.delenv("V3_INTERNAL_HTTP_HOSTS", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("연결을 열면 안 된다")

    monkeypatch.setattr(_http.urllib.request, "urlopen", boom)
    with pytest.raises(ValueError):
        _http.default_transport("POST", "http://detector:8000/score", {}, b"{}")


def test_private_ipv4_gets_http_and_the_long_timeout():
    """H100 계산 인스턴스의 탐지 서버는 VNet 피어링으로 사설 주소에서만 닿는다."""
    assert _http.timeout_for("http://10.72.0.4:8095/score", {}) == _http.INTERNAL_TIMEOUT
    assert _http.timeout_for("http://192.168.0.2/score", {}) == _http.INTERNAL_TIMEOUT


def test_metadata_and_public_addresses_are_not_internal():
    for url in (
        "http://169.254.169.254/metadata",
        "http://0.0.0.0:8095/score",
        "http://20.9.38.56:8095/score",
    ):
        with pytest.raises(ValueError):
            _http.timeout_for(url, {})
