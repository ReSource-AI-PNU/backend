import json

import pytest

from app.v3.agents.gateway import GatewayError, HttpVendorGateway, LocalVendorGateway


def test_local_gateway_calls_handler():
    calls = []

    def handler(org, message, context):
        calls.append((org, message, context))
        return {"type": "answer"}

    gateway = LocalVendorGateway(handler)
    assert gateway.deliver("org_saegil", {"seq": 1}, {"sessionId": "ses_1"}) == {"type": "answer"}
    assert calls == [("org_saegil", {"seq": 1}, {"sessionId": "ses_1"})]
    with pytest.raises(GatewayError):
        LocalVendorGateway(lambda *_: None).deliver("org_saegil", {}, {})


def test_http_gateway_posts_message_with_bearer_key():
    seen = {}

    def transport(method, url, headers, body, timeout):
        seen.update(method=method, url=url, headers=headers, body=json.loads(body), timeout=timeout)
        return 200, {}, json.dumps({"message": {"type": "offer", "text": "네"}}).encode()

    gateway = HttpVendorGateway(
        {"org_saegil": "https://vendor.internal/"}, "service-key-for-test", transport, timeout=5
    )
    reply = gateway.deliver("org_saegil", {"seq": 1, "text": "문의"}, {"sessionId": "ses_1"})
    assert reply == {"type": "offer", "text": "네"}
    assert seen["method"] == "POST" and seen["url"] == "https://vendor.internal/a2a/messages"
    assert seen["headers"]["Authorization"] == "Bearer service-key-for-test"
    assert seen["body"]["message"] == {"seq": 1, "text": "문의"}
    assert seen["body"]["context"] == {"sessionId": "ses_1"}
    assert seen["body"]["vendorOrgId"] == "org_saegil" and seen["timeout"] == 5
    assert "service-key-for-test" not in repr(gateway)


def test_http_gateway_failures_do_not_leak_the_key():
    def boom(*_args):
        raise OSError("connection refused with key service-key-for-test")

    for transport, expected in (
        (lambda *_: (500, {}, b"{}"), "500"),
        (lambda *_: (200, {}, b"not json"), "JSON"),
        (lambda *_: (200, {}, b'{"nope": 1}'), "message"),
        (boom, "OSError"),
    ):
        gateway = HttpVendorGateway("https://vendor.internal", "service-key-for-test", transport)
        with pytest.raises(GatewayError) as error:
            gateway.deliver("org_saegil", {}, {})
        assert expected in str(error.value) and "service-key-for-test" not in str(error.value)
    with pytest.raises(GatewayError):
        HttpVendorGateway({}, "k", lambda *_: (200, {}, b"{}")).deliver("org_saegil", {}, {})
