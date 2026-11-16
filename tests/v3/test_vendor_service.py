"""업체 측 에이전트 서비스: 서비스 키 확인, 받는 업체 확인, 본문과 답의 모양."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from app.core.config import settings
from app.v3 import api, service, vendor_service

VENDOR = "org_saegil"
KEY = {"Authorization": "Bearer test-key"}
MESSAGE = {
    "threadId": "th_1",
    "from": "org_hanbit",
    "to": VENDOR,
    "type": "inquiry",
    "stage": 4,
    "streamIds": ["st_sprue"],
    "text": "문의드립니다.",
    "claims": [],
}


@pytest.fixture
def calls():
    return []


@pytest.fixture
def client(monkeypatch, calls):
    state = object()  # 서비스 함수를 가짜로 바꾸므로 실제 앱 상태는 필요 없다

    def fake(app, vendor_org_id, message, context):
        calls.append((app is state, vendor_org_id, message, context))
        return {**message, "from": vendor_org_id, "to": message["from"], "type": "question"}

    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "vendor_org_id", VENDOR)
    monkeypatch.setattr(settings, "vendor_service_key", "test-key")
    monkeypatch.setattr(service, "handle_vendor_message", fake, raising=False)
    api.set_state(state)
    with TestClient(vendor_service.app) as test_client:
        yield test_client
    api.set_state(None)


def error_code(response, status):
    assert response.status_code == status, response.text
    assert set(response.json()["error"]) == {"code", "message"}
    return response.json()["error"]["code"]


def test_service_entry_point_exists():
    """service가 약속한 이름으로 진입점을 내는지 확인한다(가짜로 바꾸기 전의 것)."""
    assert callable(getattr(service, "handle_vendor_message", None))


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "vendorOrgId": VENDOR}


def test_message_is_passed_to_vendor_agent(client, calls):
    # HttpVendorGateway는 vendorOrgId도 함께 보낸다. 받는 쪽은 message.to로 확인한다.
    body = {"vendorOrgId": VENDOR, "message": MESSAGE, "context": {"sessionId": "ses_1"}}
    response = client.post("/a2a/messages", headers=KEY, json=body)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert set(reply) == {"message"}
    assert (reply["message"]["from"], reply["message"]["to"]) == (VENDOR, "org_hanbit")
    assert calls == [(True, VENDOR, MESSAGE, {"sessionId": "ses_1"})]


def test_service_key_is_required(client, calls, monkeypatch):
    body = {"message": MESSAGE, "context": {}}
    assert error_code(client.post("/a2a/messages", json=body), 401) == "UNAUTHORIZED"
    wrong = {"Authorization": "Bearer wrong"}
    assert error_code(client.post("/a2a/messages", headers=wrong, json=body), 401) == "UNAUTHORIZED"
    basic = {"Authorization": "Basic test-key"}
    assert error_code(client.post("/a2a/messages", headers=basic, json=body), 401) == "UNAUTHORIZED"
    monkeypatch.setattr(settings, "vendor_service_key", "")
    unset = client.post("/a2a/messages", headers=KEY, json=body)
    assert error_code(unset, 503) == "SERVICE_KEY_NOT_SET"
    assert calls == []


def test_rejects_wrong_recipient_and_bad_body(client, calls, monkeypatch):
    other = {"message": {**MESSAGE, "to": "org_namhae"}, "context": {}}
    assert error_code(client.post("/a2a/messages", headers=KEY, json=other), 403) == (
        "WRONG_RECIPIENT"
    )
    for body in ({}, {"message": "문의"}, {"message": MESSAGE, "context": []}):
        response = client.post("/a2a/messages", headers=KEY, json=body)
        assert error_code(response, 400) == "INVALID_REQUEST"
    not_json = client.post(
        "/a2a/messages", headers={**KEY, "Content-Type": "application/json"}, content=b"{"
    )
    assert error_code(not_json, 400) == "INVALID_REQUEST"
    monkeypatch.setattr(settings, "vendor_org_id", "")
    unset = client.post("/a2a/messages", headers=KEY, json={"message": MESSAGE, "context": {}})
    assert error_code(unset, 503) == "VENDOR_NOT_SET"
    assert calls == []
