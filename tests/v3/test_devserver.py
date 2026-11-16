"""로컬 서버를 임의 포트로 띄워 urllib로 부른다."""

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from app.v3 import devserver
from tests.v3.runtime_fixtures import DAON, HANBIT, SAEGIL, TODAY


@pytest.fixture(scope="module")
def base():
    app = devserver.build_app(today=TODAY)
    server = devserver.make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(base, method, path, body=None, org=HANBIT, headers=None, raw=None):
    headers = dict(headers or {})
    if org:
        headers["X-Org-Id"] = org
    data = raw
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def api(base, method, path, body=None, org=HANBIT, expect=200):
    status, _, raw = call(base, method, path, body, org)
    assert status == expect, raw.decode("utf-8")
    return json.loads(raw.decode("utf-8"))


def multipart(fields, filename, content):
    boundary = "----resource-test-boundary"
    lines = []
    for name, value in fields.items():
        lines += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value.encode("utf-8"),
        ]
    lines += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        b"Content-Type: application/octet-stream",
        b"",
        content,
        f"--{boundary}--".encode(),
        b"",
    ]
    return b"\r\n".join(lines), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def test_full_flow_over_http(base):
    seeded = api(base, "POST", "/demo/reset")
    assert seeded["emitterOrgId"] == HANBIT and seeded["synthetic"] is True
    assert [o["id"] for o in api(base, "GET", "/orgs")][:2] == [DAON, HANBIT]
    assert len(api(base, "GET", f"/orgs/{HANBIT}/streams")) == 4

    view = api(base, "POST", "/sessions", {"emitterOrgId": HANBIT})
    sid = view["session"]["id"]
    assert view["session"]["status"] == "idle"
    view = api(base, "POST", f"/sessions/{sid}/run")
    assert view["session"]["status"] == "awaiting_answers" and len(view["questions"]) == 3
    for question in view["questions"]:
        value = 2.0 if question["streamId"] == "st_film" else "unknown"
        api(
            base, "POST", f"/sessions/{sid}/answers", {"questionId": question["id"], "value": value}
        )
    view = api(base, "POST", f"/sessions/{sid}/run")
    assert view["session"]["status"] == "awaiting_approval"
    assert api(base, "GET", f"/sessions/{sid}") == view
    assert {t["vendorModel"]["name"] for t in view["threads"]} == {"규칙 정책(모델 미연결)"}

    # 근거 하나와 계산 사슬
    evidence_id = view["result"]["totals"]["difference"]["evidence"][0]
    found = api(base, "GET", "/evidence/" + quote(evidence_id, safe=""))
    assert found["evidence"]["id"] == evidence_id and found["evidence"]["kindLabel"] == "계산"
    law = api(base, "GET", "/evidence?q=" + quote("폐기물 위탁") + "&kind=law")
    assert law and law[0]["evidence"]["kind"] == "law"
    api(base, "GET", "/evidence/" + quote(evidence_id, safe=""), org=DAON, expect=404)

    vendors = {t["id"]: t["vendorOrgId"] for t in view["threads"]}
    for item in view["approvals"]:
        mine = api(base, "POST", f"/sessions/{sid}/approvals", {"threadId": item["threadId"]})
        assert mine["session"]["status"] == "awaiting_approval"
        api(
            base,
            "POST",
            f"/sessions/{sid}/approvals",
            {"threadId": item["threadId"]},
            org=vendors[item["threadId"]],
        )
    view = api(base, "GET", f"/sessions/{sid}")
    assert view["session"]["status"] == "confirmed"
    assert len(view["documents"]) == 6 and all(d["validation"]["ok"] for d in view["documents"])
    confirms = [t["messages"][-1] for t in view["threads"] if t["status"] == "confirmed"]
    assert len(confirms) == 3 and all(m["type"] == "confirm" for m in confirms)
    # 업체가 보는 뷰와 다른 배출 기업
    theirs = api(base, "GET", f"/sessions/{sid}", org=SAEGIL)
    assert [t["vendorOrgId"] for t in theirs["threads"]] == [SAEGIL]
    api(base, "GET", f"/sessions/{sid}", org=DAON, expect=404)
    report = api(base, "POST", "/admin/eval")
    assert report["validatorPassRate"] == 1.0 and report["model"]["provider"] == "rule"


def test_errors_use_the_contract_shape(base):
    for method, path, body, org, status, code in (
        ("GET", "/nope", None, HANBIT, 404, "NOT_FOUND"),
        ("GET", "/sessions/ses_999", None, HANBIT, 404, "SESSION_NOT_FOUND"),
        ("GET", f"/orgs/{HANBIT}/streams", None, None, 401, "ORG_REQUIRED"),
        ("GET", f"/orgs/{HANBIT}/streams", None, "org_ghost", 401, "UNKNOWN_ORG"),
        ("GET", f"/orgs/{HANBIT}/streams", None, DAON, 403, "FORBIDDEN"),
        ("POST", "/sessions", {"emitterOrgId": SAEGIL}, SAEGIL, 403, "WRONG_ROLE"),
        ("PUT", f"/vendors/{SAEGIL}", {"model": {"provider": "x"}}, SAEGIL, 422, "INVALID_VALUE"),
        ("POST", "/sessions/ses_1/answers", {"value": 1}, HANBIT, 422, "INVALID_BODY"),
    ):
        error = api(base, method, path, body, org, expect=status)["error"]
        assert error["code"] == code and error["message"]
    status, _, raw = call(
        base, "POST", "/sessions", raw=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert status == 400 and json.loads(raw)["error"]["code"] == "INVALID_JSON"
    status, _, raw = call(
        base,
        "POST",
        "/sessions",
        raw=b'["org_hanbit"]',
        headers={"Content-Type": "application/json"},
    )
    assert status == 422 and json.loads(raw)["error"]["code"] == "INVALID_BODY"
    for path, body in (
        ("/sessions/ses_1/approvals", {}),
        ("/sessions/ses_1/answers", {"questionId": "q_1", "value": None}),
        ("/intake/documents/confirm", {"fields": "x"}),
    ):
        error = api(base, "POST", path, body, HANBIT, expect=422)["error"]
        assert error["code"] == "INVALID_BODY", path


def test_cors_allows_only_the_local_frontend(base):
    for origin in devserver.ALLOWED_ORIGINS:
        status, headers, body = call(
            base,
            "OPTIONS",
            "/sessions",
            org=None,
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert status == 204 and body == b""
        assert headers["Access-Control-Allow-Origin"] == origin
        assert headers["Access-Control-Allow-Headers"] == "X-Org-Id, Content-Type"
        assert "PUT" in headers["Access-Control-Allow-Methods"]
        status, headers, _ = call(base, "GET", "/orgs", headers={"Origin": origin})
        assert status == 200 and headers["Access-Control-Allow-Origin"] == origin
    status, headers, _ = call(base, "GET", "/orgs", headers={"Origin": "http://evil.example"})
    assert status == 200 and "Access-Control-Allow-Origin" not in headers
    status, headers, _ = call(
        base, "OPTIONS", "/orgs", org=None, headers={"Origin": "http://evil.example"}
    )
    assert status == 204 and "Access-Control-Allow-Origin" not in headers


def test_uploads_and_profile_edits_over_http(base):
    api(base, "POST", "/demo/reset")
    status, headers, template = call(base, "GET", "/intake/csv/template")
    assert status == 200 and headers["Content-Type"].startswith("text/csv")
    assert template.startswith(b"\xef\xbb\xbf") and "흐름 이름" in template.decode("utf-8-sig")
    csv = "흐름 이름,공정,재질,오염도(%),수분(%),측정일\n포장 필름,포장,LDPE,3.5,1.2,2026-11-12\n"
    body, headers = multipart({"orgId": HANBIT}, "측정값 11월.csv", csv.encode("utf-8"))
    status, _, raw = call(base, "POST", "/intake/csv", raw=body, headers=headers)
    result = json.loads(raw)
    assert status == 200 and result["accepted"] == 1 and result["filename"] == "측정값 11월.csv"
    assert result["streamIds"] == ["st_film"] and result["errors"] == []
    film = next(s for s in api(base, "GET", f"/orgs/{HANBIT}/streams") if s["id"] == "st_film")
    assert film["measure"]["moisturePct"] == 1.2

    contract = "처리 방법: 혼합 소각\n위탁 단가 250원/kg\n".encode()
    body, headers = multipart({"kind": "contract"}, "계약서.txt", contract)
    status, _, raw = call(base, "POST", "/intake/documents", raw=body, headers=headers)
    result = json.loads(raw)
    assert status == 200 and result["shieldUnavailable"] is True and result["blocked"] is False
    assert result["extracted"]["unitCostKrwPerKg"] == 250
    status, _, raw = call(
        base, "POST", "/intake/documents", raw=b"", headers={"Content-Type": "text/plain"}
    )
    assert status == 422 and json.loads(raw)["error"]["code"] == "FILE_REQUIRED"

    updated = api(
        base,
        "PUT",
        f"/orgs/{HANBIT}/streams/st_film",
        {"currentTreatment": {"method": "위탁 재활용", "unitCostKrwPerKg": 90}},
    )
    assert updated["currentTreatment"]["evidence"] == ["human:org_hanbit:st_film-현행처리단가"]
    vendor = api(base, "PUT", f"/vendors/{SAEGIL}", {"guardrails": {"days": ["수"]}}, org=SAEGIL)
    assert vendor["guardrails"]["days"] == ["수"]
    assert "guardrails" not in api(base, "GET", f"/vendors/{SAEGIL}")
    status = api(base, "GET", "/admin/status")
    assert status["store"] == "InMemoryStore" and status["models"]["llmFactory"] is False
    assert api(base, "GET", "/health", org=None) == {"ok": True}


def test_multipart_parser_handles_quotes_and_encoded_names():
    body = (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="kind"\r\n\r\npermit\r\n'
        b"--b\r\n"
        b'Content-Disposition: form-data; name="file"; filename="a;b.pdf"; filename*=UTF-8\'\'%ED%97%88%EA%B0%80%EC%A6%9D.pdf\r\n'
        b"Content-Type: application/pdf\r\n\r\n\x00\x01--b-not-a-boundary\r\n\r\n"
        b"--b--\r\n"
    )
    fields, files = devserver.parse_multipart("multipart/form-data; boundary=b", body)
    assert fields == {"kind": "permit"}
    # filename*(UTF-8)가 있으면 그것을 쓴다. 내용 안의 경계 비슷한 글자는 자르지 않는다
    assert files["file"] == ("허가증.pdf", b"\x00\x01--b-not-a-boundary\r\n")
    plain = body.replace(b"; filename*=UTF-8''%ED%97%88%EA%B0%80%EC%A6%9D.pdf", b"")
    assert (
        devserver.parse_multipart("multipart/form-data; boundary=b", plain)[1]["file"][0]
        == "a;b.pdf"
    )
    fields, files = devserver.parse_multipart('multipart/form-data; boundary="b"', body)
    assert set(files) == {"file"}
    from app.v3.service import ServiceError

    with pytest.raises(ServiceError):
        devserver.parse_multipart("multipart/form-data", body)


def test_llm_option_reads_environment_only_for_foundry(tmp_path):
    env = {"FOUNDRY_ENDPOINT": "https://example.invalid"}
    app = devserver.build_app(llm="rule", today=TODAY, env=env)  # rule이면 환경 변수를 보지 않는다
    assert app.llm_factory is None and app.llm_for("company", HANBIT, None).provider == "rule"
    with pytest.raises(SystemExit) as error:
        devserver.build_app(llm="foundry", today=TODAY, env=env)
    assert "FOUNDRY_DEPLOYMENT" in str(error.value) and "FOUNDRY_API_KEY" in str(error.value)
    full = {**env, "FOUNDRY_DEPLOYMENT": "chat", "FOUNDRY_API_KEY": "not-a-real-key"}
    path = tmp_path / "store.json"
    app = devserver.build_app(llm="foundry", today=TODAY, env=full, store_path=str(path))
    company = app.llm_for("company", HANBIT, None)
    assert (company.provider, company.model) == (
        "foundry",
        "chat",
    ) and "not-a-real-key" not in repr(company)
    assert (
        app.llm_for("vendor", SAEGIL, {"provider": "foundry", "name": "gpt-5-mini"}).provider
        == "foundry"
    )
    # 업체가 ollama를 골랐는데 주소가 없으면 규칙 정책으로 돌고, 그렇게 표시된다
    assert (
        app.llm_for("vendor", "org_namhae", {"provider": "ollama", "name": "qwen2.5:7b"}).provider
        == "rule"
    )
    with_ollama = devserver.build_app(
        llm="foundry", today=TODAY, env={**full, "OLLAMA_BASE_URL": "http://10.0.0.4:11434"}
    )
    local = with_ollama.llm_for(
        "vendor", "org_namhae", {"provider": "ollama", "name": "qwen2.5:7b"}
    )
    assert (local.provider, local.model) == ("ollama", "qwen2.5:7b")
    assert path.exists() and json.loads(path.read_text("utf-8"))["orgs"]
