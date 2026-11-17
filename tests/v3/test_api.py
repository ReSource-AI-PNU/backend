"""v3 HTTP API: 시연 시나리오, 오류 응답 모양, 조직 시야. 규칙 정책과 메모리 저장으로 돈다."""

from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.main import app
from app.v3 import api
from app.v3.agents.llm import AnthropicChat, FoundryChat, OllamaChat

EMITTER, OTHER_EMITTER = "org_hanbit", "org_daon"
VENDORS = ("org_saegil", "org_namhae", "org_miso")
ADMIN = "org_admin"
HIDDEN = (403, 404)  # 볼 수 없는 것은 금지나 없음으로 답한다


def org(org_id):
    return {"X-Org-Id": org_id}


def assert_error(response, *statuses):
    assert response.status_code in statuses, response.text
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] and body["error"]["message"]
    return body["error"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_llm", "rule")
    api.set_state(None)
    with TestClient(app) as test_client:
        assert test_client.post("/api/v3/demo/reset").status_code == 200
        yield test_client
    api.set_state(None)


def run_to_approval(client, session_id):
    """멈춤 지점마다 질문에 "모름"으로 답하며 승인 대기까지 돌린다."""
    view = None
    for _ in range(6):
        response = client.post(f"/api/v3/sessions/{session_id}/run", headers=org(EMITTER))
        assert response.status_code == 200, response.text
        view = response.json()
        if view["session"]["status"] != "awaiting_answers":
            break
        open_questions = [q for q in view["questions"] if not q["answered"]]
        assert open_questions, "awaiting_answers인데 답할 질문이 없다"
        for question in open_questions:
            answered = client.post(
                f"/api/v3/sessions/{session_id}/answers",
                headers=org(EMITTER),
                json={"questionId": question["id"], "value": "unknown"},
            )
            assert answered.status_code == 200, answered.text
    return view


def test_orgs_and_streams_after_reset(client):
    orgs = client.get("/api/v3/orgs").json()
    assert {EMITTER, OTHER_EMITTER, *VENDORS} <= {o["id"] for o in orgs}
    streams = client.get(f"/api/v3/orgs/{EMITTER}/streams", headers=org(EMITTER))
    assert streams.status_code == 200, streams.text
    assert {"st_sprue", "st_defect", "st_purge", "st_film"} <= {s["id"] for s in streams.json()}
    assert all(s["orgId"] == EMITTER for s in streams.json())


def test_health_and_document_confirm(client):
    assert client.get("/api/v3/health").json() == {"ok": True}
    confirmed = client.post(
        "/api/v3/intake/documents/confirm",
        headers=org(EMITTER),
        json={"orgId": EMITTER, "documentName": "성적서.txt", "fields": []},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"evidence": []}
    wrong = client.post(
        "/api/v3/intake/documents/confirm", headers=org(EMITTER), json={"fields": "x"}
    )
    assert assert_error(wrong, 422)["code"] == "INVALID_BODY"


def test_media_upload_says_so_when_the_detector_is_not_connected(client):
    """탐지 엔드포인트 설정이 없으면 결과를 지어내지 않고 미연결이라고 답한다."""
    assert api.get_state().detector is None
    uploaded = client.post(
        "/api/v3/intake/media",
        params={"streamId": "st_film", "filename": "필름.jpg"},
        headers=org(EMITTER),
        content=b"jpeg-bytes",
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {
        "detectorUnavailable": True,
        "message": "탐지 모델이 연결되지 않았습니다",
        "streamId": "st_film",
        "evidence": [],
    }
    other = client.post(
        "/api/v3/intake/media",
        params={"orgId": EMITTER, "streamId": "st_film", "filename": "필름.jpg"},
        headers=org(OTHER_EMITTER),
        content=b"jpeg-bytes",
    )
    assert_error(other, *HIDDEN)
    empty = client.post(
        "/api/v3/intake/media", params={"streamId": "st_film"}, headers=org(EMITTER), content=b""
    )
    assert assert_error(empty, 422)["code"] == "FILE_REQUIRED"
    # 미연결이어도 지원하지 않는 형식은 415가 먼저다
    text = client.post(
        "/api/v3/intake/media",
        params={"streamId": "st_film", "filename": "메모.txt"},
        headers=org(EMITTER),
        content=b"hello",
    )
    assert assert_error(text, 415)["code"] == "UNSUPPORTED_MEDIA"


def test_admin_routes_and_reset_need_the_admin_org(client, monkeypatch):
    orgs = client.get("/api/v3/orgs").json()
    assert orgs[0]["id"] == EMITTER and orgs[-1]["id"] == ADMIN  # order 순
    for path in ("/api/v3/admin/status", "/api/v3/admin/retrain/export"):
        assert assert_error(client.get(path, headers=org(EMITTER)), 403)["code"] == "FORBIDDEN"
        assert client.get(path, headers=org(ADMIN)).status_code == 200
    # 조직이 이미 있는 저장소는 관리자만 되돌린다(V3_DEMO_OPEN 기본 false)
    assert assert_error(client.post("/api/v3/demo/reset"), 401)["code"] == "ORG_REQUIRED"
    denied = client.post("/api/v3/demo/reset", headers=org(EMITTER))
    assert assert_error(denied, 403)["code"] == "FORBIDDEN"
    assert client.post("/api/v3/demo/reset", headers=org(ADMIN)).status_code == 200
    monkeypatch.setattr(api.get_state(), "demo_open", True)
    assert client.post("/api/v3/demo/reset").status_code == 200


def test_error_shape(client):
    missing = assert_error(client.get(f"/api/v3/orgs/{EMITTER}/streams"), 401)
    assert missing["code"] == "ORG_REQUIRED"
    not_json = client.post(
        "/api/v3/sessions",
        headers={**org(EMITTER), "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert assert_error(not_json, 400)["code"] == "INVALID_JSON"
    not_object = client.post("/api/v3/sessions", headers=org(EMITTER), json=["org_hanbit"])
    assert assert_error(not_object, 422)["code"] == "INVALID_BODY"
    no_field = client.post(
        "/api/v3/sessions/ses_1/answers", headers=org(EMITTER), json={"value": 1}
    )
    assert assert_error(no_field, 422)["code"] == "INVALID_BODY"
    no_thread = client.post("/api/v3/sessions/ses_1/approvals", headers=org(EMITTER), json={})
    assert assert_error(no_thread, 422)["code"] == "INVALID_BODY"
    no_file = client.post("/api/v3/intake/csv", headers=org(EMITTER), content=b"")
    assert assert_error(no_file, 422)["code"] == "FILE_REQUIRED"
    assert_error(client.get("/api/v3/sessions/ses_없는것", headers=org(EMITTER)), *HIDDEN)
    assert_error(client.get("/api/v3/evidence/measure:없는:것", headers=org(EMITTER)), *HIDDEN)


def test_emitters_cannot_see_each_other(client):
    assert_error(client.get(f"/api/v3/orgs/{EMITTER}/streams", headers=org(OTHER_EMITTER)), *HIDDEN)
    changed = client.put(
        f"/api/v3/orgs/{EMITTER}/streams/st_sprue",
        headers=org(OTHER_EMITTER),
        json={"name": "남의 흐름"},
    )
    assert_error(changed, *HIDDEN)
    assert_error(
        client.post("/api/v3/sessions", headers=org(OTHER_EMITTER), json={"emitterOrgId": EMITTER}),
        *HIDDEN,
    )
    mine = client.get(f"/api/v3/orgs/{EMITTER}/streams", headers=org(EMITTER)).json()
    private = [
        evidence_id for s in mine for evidence_id in (s.get("measure") or {}).get("evidence") or []
    ]
    assert private, "시연 흐름에 측정값 근거가 있어야 한다"
    path = f"/api/v3/evidence/{quote(private[0], safe='')}"
    assert client.get(path, headers=org(EMITTER)).status_code == 200
    assert_error(client.get(path, headers=org(OTHER_EMITTER)), *HIDDEN)


def test_demo_scenario_to_confirmation(client):
    created = client.post("/api/v3/sessions", headers=org(EMITTER), json={"emitterOrgId": EMITTER})
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]
    assert created.json()["session"]["emitterOrgId"] == EMITTER
    assert created.json()["viewer"]["orgId"] == EMITTER

    view = run_to_approval(client, session_id)
    assert view["session"]["status"] == "awaiting_approval"
    assert [s["stage"] for s in view["stages"]] == [1, 2, 3, 4, 5, 6]
    for thread in view["threads"]:
        assert thread["turns"] <= thread["maxTurns"]
        for message in thread["messages"]:
            assert message["validation"]["ok"], message  # 검증을 통과한 메시지만 나간다
    batches = {}
    for question in view["questions"]:
        batches[question["batch"]] = batches.get(question["batch"], 0) + 1
    assert set(batches) <= {1, 2} and all(count <= 3 for count in batches.values())
    assert view["events"], "실행 기록이 있어야 한다"
    assert view["documents"] == []  # 승인 전에는 문서를 만들지 않는다

    blocked = {a["threadId"] for a in view["approvals"] if a["blocked"]}
    agreed = [t for t in view["threads"] if t["status"] == "agreed" and t["id"] not in blocked]
    assert agreed, "시연 시나리오에는 합의된 스레드가 하나 이상 있어야 한다"
    for thread in agreed:
        body = {"threadId": thread["id"]}
        first = client.post(
            f"/api/v3/sessions/{session_id}/approvals", headers=org(EMITTER), json=body
        )
        assert first.status_code == 200, first.text
        halfway = client.get(f"/api/v3/sessions/{session_id}", headers=org(EMITTER)).json()
        mine = next(t for t in halfway["threads"] if t["id"] == thread["id"])
        assert mine["status"] == "agreed"  # 한쪽만 승인하면 확정하지 않는다
        # 앞서 확정한 스레드의 문서는 이미 있으므로 이 스레드의 문서만 본다
        assert not [d for d in halfway["documents"] if d["threadId"] == thread["id"]]
        second = client.post(
            f"/api/v3/sessions/{session_id}/approvals",
            headers=org(thread["vendorOrgId"]),
            json=body,
        )
        assert second.status_code == 200, second.text

    final = client.get(f"/api/v3/sessions/{session_id}", headers=org(EMITTER)).json()
    statuses = {t["id"]: t["status"] for t in final["threads"]}
    assert all(statuses[t["id"]] == "confirmed" for t in agreed)
    assert final["documents"] and {d["kind"] for d in final["documents"]} <= {"request", "plan"}
    assert all(d["validation"]["ok"] for d in final["documents"])

    # 업체는 자기 스레드와 그 스레드의 흐름만 본다. 다른 배출 기업은 진단을 보지 못한다.
    vendor_id = agreed[0]["vendorOrgId"]
    vendor_view = client.get(f"/api/v3/sessions/{session_id}", headers=org(vendor_id)).json()
    assert vendor_view["viewer"]["orgId"] == vendor_id
    assert vendor_view["threads"]
    assert all(t["vendorOrgId"] == vendor_id for t in vendor_view["threads"])
    shared = {sid for t in vendor_view["threads"] for sid in t["streamIds"]}
    assert {s["id"] for s in vendor_view["streams"]} <= shared
    assert_error(client.get(f"/api/v3/sessions/{session_id}", headers=org(OTHER_EMITTER)), *HIDDEN)
    assert_error(
        client.post(f"/api/v3/sessions/{session_id}/run", headers=org(OTHER_EMITTER)), *HIDDEN
    )


def test_evidence_search_and_chain(client):
    found = client.get(
        "/api/v3/evidence", params={"q": "폐기물", "kind": "law"}, headers=org(EMITTER)
    )
    assert found.status_code == 200, found.text
    items = found.json()  # service.search_evidence는 [{"evidence", "score"}]를 돌려준다
    assert items and all(item["evidence"]["kind"] == "law" for item in items)
    first_id = items[0]["evidence"]["id"]
    one = client.get(f"/api/v3/evidence/{quote(first_id, safe='')}", headers=org(EMITTER))
    assert one.status_code == 200, one.text
    assert one.json()["evidence"]["id"] == first_id
    assert isinstance(one.json()["chain"], list)


def test_csv_template_and_admin_status(client):
    template = client.get("/api/v3/intake/csv/template")
    assert template.status_code == 200
    assert template.headers["content-type"].startswith("text/csv")
    assert "attachment" in template.headers["content-disposition"] and template.content
    status = client.get("/api/v3/admin/status", headers=org(ADMIN))
    assert status.status_code == 200 or assert_error(status, 403)


def test_feedback_routes(client):
    created = client.post("/api/v3/sessions", headers=org(EMITTER), json={"emitterOrgId": EMITTER})
    session_id = created.json()["session"]["id"]
    view = run_to_approval(client, session_id)
    body = {"streamId": "st_sprue", "field": "form", "before": "가공 전", "after": "분쇄"}
    path = f"/api/v3/sessions/{session_id}"
    fixed = client.post(f"{path}/corrections", headers=org(EMITTER), json=body)
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["correction"]["target"] == "detector" and fixed.json()["evidence"]
    assert_error(client.post(f"{path}/corrections", headers=org(VENDORS[0]), json=body), 403)
    assert_error(client.post(f"{path}/corrections", headers=org(OTHER_EMITTER), json=body), *HIDDEN)
    # 확정 전에는 실제 결과를 받지 않는다
    thread = next(t for t in view["threads"] if t["status"] == "agreed")
    outcome = {
        "threadId": thread["id"],
        "streamId": thread["assignedStreamIds"][0],
        "actualGrade": "A",
        "inspectedOn": view["session"]["today"],
    }
    early = client.post(f"{path}/outcomes", headers=org(thread["vendorOrgId"]), json=outcome)
    assert assert_error(early, 409)["code"] == "THREAD_NOT_CONFIRMED"
    assert_error(client.get("/api/v3/admin/retrain/export", headers=org(EMITTER)), 403)
    assert_error(client.post("/api/v3/admin/retrain/runs", headers=org(EMITTER), json={}), 403)
    status = client.get("/api/v3/admin/status", headers=org(ADMIN)).json()
    assert status["retrain"]["corrections"]["count"] == 1
    assert status["retrain"]["modelVersions"] == []


def test_llm_factory_never_swaps_models():
    assert api.make_llm_factory("rule")("company", EMITTER, None) is None
    with pytest.raises(ValueError, match="V3_LLM"):
        api.make_llm_factory("gpt")
    unset = api.make_llm_factory("foundry")
    assert unset("company", EMITTER, None) is None  # 설정이 없으면 규칙 정책
    factory = api.make_llm_factory(
        "foundry", endpoint="https://example.invalid", deployment="chat", api_key="test-key"
    )
    company = factory("company", EMITTER, None)
    assert isinstance(company, FoundryChat) and company.model == "chat"
    ollama = {"provider": "ollama", "name": "open-model"}
    assert (
        factory("vendor", "org_namhae", ollama) is None
    )  # 주소가 없으면 다른 모델로 바꾸지 않는다
    with_ollama = api.make_llm_factory(
        "foundry",
        endpoint="https://example.invalid",
        deployment="chat",
        api_key="test-key",
        ollama_base_url="http://ollama.invalid:11434",
    )
    chosen = with_ollama("vendor", "org_namhae", ollama)
    assert isinstance(chosen, OllamaChat) and chosen.model == "open-model"
    assert isinstance(with_ollama("vendor", "org_saegil", {"provider": "foundry"}), FoundryChat)


def test_llm_factory_anthropic_option_matches_the_dev_server(tmp_path):
    """배포 경로에서도 V3_LLM=anthropic을 고를 수 있다. 키가 없으면 규칙 정책으로 돈다."""
    unset = api.make_llm_factory("anthropic")
    assert unset("company", EMITTER, None) is None
    key_file = tmp_path / "key"
    key_file.write_text("not-a-real-key\n", encoding="utf-8")
    from_file = api.make_llm_factory("anthropic", anthropic_api_key_file=str(key_file))
    company = from_file("company", EMITTER, None)
    assert isinstance(company, AnthropicChat) and company.model == "claude-sonnet-5-5"
    assert "not-a-real-key" not in repr(company)
    ollama = {"provider": "ollama", "name": "open-model"}
    assert from_file("vendor", "org_namhae", ollama) is None  # 다른 모델로 바꾸지 않는다
    everyone = api.make_llm_factory(
        "anthropic",
        anthropic_api_key="not-a-real-key",
        anthropic_model="m-1",
        all_vendors=True,
    )
    swapped = everyone("vendor", "org_namhae", ollama)
    assert isinstance(swapped, AnthropicChat) and swapped.model == "m-1"


def test_build_app_reads_the_anthropic_settings(monkeypatch):
    """설정 V3_LLM=anthropic, ANTHROPIC_API_KEY로 만든 앱은 관리자 화면에 그 모델을 보여 준다."""
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_llm", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "not-a-real-key")
    monkeypatch.setattr(settings, "anthropic_api_key_file", "")
    monkeypatch.setattr(settings, "anthropic_model", "")
    monkeypatch.setattr(settings, "v3_llm_all_vendors", False)
    built = api.build_app()
    company = built.llm_for("company", EMITTER, None)
    assert (company.provider, company.model) == ("anthropic", "claude-sonnet-5-5")
    assert built.llm_for("vendor", "org_namhae", {"provider": "ollama", "name": "x"}).provider == (
        "rule"
    )
    fields = Settings(v3_llm="anthropic", anthropic_api_key_file="/run/secrets/key")
    assert fields.anthropic_api_key_file == "/run/secrets/key" and not fields.v3_llm_all_vendors


def test_build_app_picks_the_distance_provider_from_settings(monkeypatch):
    """V3_DISTANCE=kakao와 KAKAO_REST_API_KEY로 카카오 길찾기를, 비어 있으면 이전처럼 Azure Maps를 쓴다."""
    from app.v3.judge.distance import AzureMapsDistance, KakaoDistance

    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_llm", "rule")
    monkeypatch.setattr(settings, "azure_maps_key", "")
    monkeypatch.setattr(settings, "kakao_rest_api_key", "kakao-test")
    monkeypatch.setattr(settings, "v3_distance", "")
    assert api.build_app().distance_provider is None
    monkeypatch.setattr(settings, "v3_distance", "kakao")
    assert isinstance(api.build_app().distance_provider, KakaoDistance)
    monkeypatch.setattr(settings, "kakao_rest_api_key", "")
    assert api.build_app().distance_provider is None  # 키가 없으면 가정값 경로(서버는 뜬다)
    monkeypatch.setattr(settings, "v3_distance", "")
    monkeypatch.setattr(settings, "azure_maps_key", "maps-test")
    assert isinstance(api.build_app().distance_provider, AzureMapsDistance)


def test_database_url_uses_installed_driver():
    """Key Vault의 database-url(postgresql://)을 그대로 넣어도 psycopg 3로 붙는다."""
    plain = Settings(database_url="postgresql://user:pw@db.invalid:5432/app?sslmode=require")
    assert plain.database_url == (
        "postgresql+psycopg://user:pw@db.invalid:5432/app?sslmode=require"
    )
    kept = "postgresql+psycopg://user:pw@db.invalid:5432/app"
    assert Settings(database_url=kept).database_url == kept
    assert Settings(database_url="sqlite:///./x.db").database_url == "sqlite:///./x.db"


def test_remote_vendor_agents_need_a_service_key(monkeypatch):
    monkeypatch.setattr(settings, "vendor_org_id", "")
    monkeypatch.setattr(settings, "vendor_service_key", "")
    monkeypatch.setattr(settings, "v3_vendor_agents", {})
    assert api.make_vendor_gateway() is None  # 기본은 같은 프로세스
    monkeypatch.setattr(settings, "v3_vendor_agents", {"org_saegil": "https://vendor.invalid"})
    with pytest.raises(RuntimeError, match="VENDOR_SERVICE_KEY"):
        api.make_vendor_gateway()
    monkeypatch.setattr(settings, "vendor_service_key", "test-key")
    assert api.make_vendor_gateway() is not None
    monkeypatch.setattr(settings, "vendor_org_id", "org_saegil")
    assert api.make_vendor_gateway() is None  # 업체 측 서비스 자신은 다시 넘기지 않는다


def test_build_app_connects_azure_parts_only_when_configured(monkeypatch):
    """도로 거리, 검색 백엔드, 추적은 설정이 있을 때만 켜진다."""
    from app.v3 import tracing
    from app.v3.evidence import search
    from app.v3.judge.distance import AzureMapsDistance

    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "azure_maps_key", "")
    monkeypatch.setattr(settings, "azure_search_endpoint", "")
    monkeypatch.setattr(settings, "azure_search_api_key", "")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("V3_EMBEDDINGS", raising=False)
    monkeypatch.delenv("V3_AGENT_FRAMEWORK", raising=False)
    plain = api.build_app()
    assert plain.distance_provider is None and search.backend_name() == "local"
    assert not isinstance(plain.store, tracing.TracingStore)

    monkeypatch.setattr(settings, "azure_maps_key", "test-key")
    monkeypatch.setattr(settings, "azure_search_endpoint", "https://search.invalid")
    monkeypatch.setattr(settings, "azure_search_api_key", "test-key")
    connected = api.build_app()
    assert isinstance(connected.distance_provider, AzureMapsDistance)
    assert search.backend_name() == "azure"

    monkeypatch.setenv("V3_EMBEDDINGS", "ollama")  # 주소가 없으면 로컬 검색으로 돈다
    monkeypatch.setattr(settings, "ollama_base_url", "")
    api.build_app()
    assert search.backend_name() == "local"
    search.use_backend(None)


# ── 요청한 조직: token 모드 ──────────────────────────────────────────
# 아래 토큰은 테스트용으로 지어낸 글이다(실제 값이 아니다)
ORG_TOKENS = {
    EMITTER: "test-hanbit-" + "a" * 12,
    OTHER_EMITTER: "test-daon-" + "b" * 14,
    ADMIN: "test-admin-" + "d" * 13,
}


def bearer(org_id):
    return {"Authorization": f"Bearer {ORG_TOKENS[org_id]}"}


def test_token_mode_maps_the_bearer_token_and_ignores_the_org_header(client, monkeypatch):
    import json

    monkeypatch.setattr(settings, "v3_auth", "token")
    monkeypatch.setattr(settings, "v3_org_tokens", json.dumps(ORG_TOKENS))
    path = f"/api/v3/orgs/{EMITTER}/streams"
    assert assert_error(client.get(path, headers=org(EMITTER)), 401)["code"] == "TOKEN_REQUIRED"
    wrong = client.get(path, headers={"Authorization": "Bearer " + "z" * 24})
    assert assert_error(wrong, 401)["code"] == "INVALID_TOKEN"
    assert client.get(path, headers=bearer(EMITTER)).status_code == 200
    # 다른 조직의 토큰에 조직 헤더를 붙여도 그 조직이 되지 않는다
    spoofed = client.get(path, headers={**bearer(OTHER_EMITTER), **org(EMITTER)})
    assert assert_error(spoofed, 403)["code"] == "FORBIDDEN"
    admin = client.get("/api/v3/admin/status", headers={**bearer(EMITTER), **org(ADMIN)})
    assert assert_error(admin, 403)["code"] == "FORBIDDEN"
    assert client.get("/api/v3/admin/status", headers=bearer(ADMIN)).status_code == 200
    # 조직 목록은 자기 조직과 대화 상대만
    assert assert_error(client.get("/api/v3/orgs"), 401)["code"] == "TOKEN_REQUIRED"
    mine = client.get("/api/v3/orgs", headers=bearer(EMITTER)).json()
    assert [item["id"] for item in mine] == [EMITTER]
    everyone = client.get("/api/v3/orgs", headers=bearer(ADMIN)).json()
    assert {EMITTER, OTHER_EMITTER, ADMIN} <= {item["id"] for item in everyone}
    assert client.get("/api/v3/health").json() == {"ok": True}


def test_token_mode_without_tokens_is_a_configuration_error(monkeypatch):
    monkeypatch.setattr(settings, "v3_auth", "token")
    monkeypatch.setattr(settings, "v3_org_tokens", "")
    with pytest.raises(ValueError):
        api.current_auth()
    monkeypatch.setattr(settings, "v3_auth", "entra")
    with pytest.raises(ValueError):
        api.current_auth()
