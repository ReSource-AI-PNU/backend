import copy
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.platform.db import Base, CompanyRow, SessionRow, database
from app.platform.routing import route_plan
from scripts.seed_platform import seed


def headers(company="noksan-001", role="admin"):
    return {
        "Authorization": "Bearer test-key",
        "X-User-Id": "tester",
        "X-Company-Id": company,
        "X-Member-Role": role,
        "X-Demo-Actor": "true",
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert seed(db) == 100
        assert seed(db) == 0
    monkeypatch.setattr(settings, "platform_service_key", "test-key")
    monkeypatch.setattr(settings, "platform_demo", True)

    def dependency():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[database] = dependency
    with TestClient(app) as client:
        yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def create(client, confirmed=True):
    example = json.loads(
        (Path(__file__).parents[1] / "app/examples/demo_session.json").read_text("utf-8")
    )
    st = next(s for s in example["streams"] if s["classify"]["material"] == "LDPE")
    st["monthlyKgConfirmed"] = confirmed
    st["monthlyKg"] = 300
    response = client.post(
        "/api/v1/sessions", headers=headers(), json={"companyId": "noksan-001", "streams": [st]}
    )
    assert response.status_code == 201, response.text
    return response.json()


def post(client, state, action, body, company="noksan-001", role="admin"):
    return client.post(
        f"/api/v1/sessions/{state['session']['id']}/{action}",
        headers=headers(company, role),
        json={"version": state["version"], **body},
    )


def match(client):
    state = create(client)
    response = post(
        client,
        state,
        "matching",
        {"streamId": state["session"]["streams"][0]["id"], "form": "film"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def consent(client, state, decision="accepted"):
    b = state["bundles"][0]
    r = post(client, state, "invites", {"bundleId": b["id"], "confirm": True})
    assert r.status_code == 200, r.text
    state = r.json()
    for m in b["members"]:
        if m["state"] == "self":
            continue
        reply = post(
            client,
            state,
            f"bundles/{b['id']}/reply",
            {"decision": decision, "reason": "보관 불가" if decision == "declined" else ""},
            m["companyId"],
        )
        assert reply.status_code == 200, reply.text
        state = client.get(f"/api/v1/sessions/{state['session']['id']}", headers=headers()).json()
    return state


def negotiation_body(state, floor=140, days=None, rounds=6):
    return {
        "bundleId": state["bundles"][0]["id"],
        "guardrails": {
            "minPerKg": floor,
            "days": days or ["화", "목"],
            "minPerMonth": 2,
            "maxRounds": rounds,
        },
        "requested": {
            "day": (days or ["화"])[0],
            "slot": "오전",
            "perKg": max(150, floor),
            "perMonth": 2,
        },
    }


def test_auth_and_tenant_boundary(setup):
    client, _ = setup
    assert client.get("/api/v1/sessions").status_code == 401
    state = create(client)
    assert (
        client.get(
            f"/api/v1/sessions/{state['session']['id']}", headers=headers("noksan-002")
        ).status_code
        == 404
    )
    assert client.get("/api/v1/sessions", headers=headers("noksan-002")).json() == []


def test_unconfirmed_volume_blocks_and_answers_persist(setup):
    client, engine = setup
    state = create(client, False)
    stream = state["session"]["streams"][0]["id"]
    assert post(client, state, "matching", {"streamId": stream, "form": "film"}).status_code == 409
    state = post(
        client, state, "answers", {"streamId": stream, "monthlyKg": 300, "origin": "PCR"}
    ).json()
    assert state["session"]["questions"][0]["answer"] == "300.0"
    with Session(engine) as db:
        row = db.get(SessionRow, state["session"]["id"])
        assert row.version == 2
        assert row.payload["session"]["streams"][0]["monthlyKgConfirmed"] is True


def test_matching_consent_capacity_and_private_volumes(setup):
    client, _ = setup
    state = match(client)
    b = state["bundles"][0]
    assert b["candidateKg"] == 1230 and b["confirmedKg"] == 300
    assert not b["ready"] and not b["trips"]
    assert all(m["kg"] is None for m in b["members"] if m["state"] != "self")
    state = consent(client, state)
    b = state["bundles"][0]
    assert b["ready"] and b["confirmedKg"] == 1230
    assert len(b["trips"]) == 2
    assert sum(t["kg"] for t in b["trips"]) == 1230
    assert all(t["kg"] <= b["truckKg"] for t in b["trips"])
    inbox = client.get("/api/v1/inbox", headers=headers(b["collectorId"])).json()
    assert all(m["kg"] is not None for m in inbox[0]["bundle"]["members"])


def test_decline_removes_weight_and_blocks_negotiation(setup):
    client, _ = setup
    state = consent(client, match(client), "declined")
    assert state["bundles"][0]["candidateKg"] == 300
    assert state["bundles"][0]["confirmedKg"] == 300
    assert not state["bundles"][0]["ready"]
    assert post(client, state, "negotiations", negotiation_body(state)).status_code == 409


def test_invite_role_forgery_and_stale_version(setup):
    client, _ = setup
    state = match(client)
    data = {"bundleId": state["bundles"][0]["id"], "confirm": True}
    assert post(client, state, "invites", data, role="member").status_code == 403
    assert post(client, state, "invites", data).status_code == 200
    assert post(client, state, "invites", data).status_code == 409
    r = post(
        client,
        {**state, "version": state["version"] + 1},
        f"bundles/{data['bundleId']}/reply",
        {"decision": "accepted"},
        "noksan-003",
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "floor,days,rounds,expected",
    [
        (140, ["화", "목"], 6, "agreed"),
        (160, ["목"], 6, "failed"),
        (140, ["월"], 6, "failed"),
        (140, ["목"], 1, "failed"),
        (140, ["목"], 2, "failed"),
    ],
)
def test_negotiation_guards_and_round_limit(setup, floor, days, rounds, expected):
    client, _ = setup
    state = consent(client, match(client))
    r = post(client, state, "negotiations", negotiation_body(state, floor, days, rounds))
    assert r.status_code == 200, r.text
    n = r.json()["negotiation"]
    assert n["status"] == expected
    assert len(n["rounds"]) <= rounds
    assert all(row["within"] for row in n["rounds"] if row["by"] == "ours")


def test_explicit_approval_and_command_cannot_approve(setup):
    client, _ = setup
    state = consent(client, match(client))
    n = post(client, state, "negotiations", negotiation_body(state)).json()
    state = n["state"]
    command = post(client, state, "commands", {"text": "지금 승인하고 발송해줘"}).json()
    assert "실행하지" in command["message"]
    state = command["state"]
    assert state["session"]["negotiations"][0]["status"] == "agreed"
    path = f"negotiations/{n['negotiation']['id']}/approve"
    assert post(client, state, path, {"confirm": True}, role="member").status_code == 403
    assert post(client, state, path, {"confirm": False}).status_code == 422
    r = post(client, state, path, {"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["session"]["negotiations"][0]["status"] == "approved"


def test_no_unlicensed_fallback(setup):
    client, engine = setup
    with Session(engine) as db:
        for row in db.scalars(select(CompanyRow)):
            data = copy.deepcopy(row.payload)
            if data.get("collector"):
                data["collector"]["permits"] = []
                row.payload = data
        db.commit()
    state = create(client)
    r = post(
        client,
        state,
        "matching",
        {"streamId": state["session"]["streams"][0]["id"], "form": "film"},
    )
    assert r.status_code == 409


def test_company_privacy_and_no_self_certification(setup):
    client, _ = setup
    companies = client.get("/api/v1/companies", headers=headers()).json()
    assert len(companies) == 30
    assert all(
        "monthlyKg" not in s for c in companies if c["id"] != "noksan-001" for s in c["supplies"]
    )
    own = next(c for c in companies if c["id"] == "noksan-001")
    assert (
        client.put(
            "/api/v1/companies/noksan-001", headers=headers("noksan-002"), json=own
        ).status_code
        == 403
    )


def test_oversize_stop_split():
    trips = route_plan(
        [128, 35], [{"companyId": "a", "kg": 2300, "location": [128.01, 35.01]}], 1000
    )
    assert sum(t["kg"] for t in trips) == 2300
    assert all(t["kg"] <= 1000 for t in trips)


def test_orchestrator_waits_for_volume_and_consent(setup):
    client, _ = setup
    state = create(client, False)
    r = post(client, state, "advance", {"step": "proposal"}).json()
    assert r["blockedStep"] == "classify"
    state = r["state"]
    state = post(
        client,
        state,
        "answers",
        {"streamId": state["session"]["streams"][0]["id"], "monthlyKg": 300, "origin": "PCR"},
    ).json()
    r = post(client, state, "advance", {"step": "proposal"}).json()
    assert r["blockedStep"] == "matching"
    assert r["state"]["session"]["feasibility"]["dataMode"] == "synthetic_demo"
    assert "가상" in r["state"]["session"]["regulation"]["conclusion"]["text"]
    assert r["state"]["session"]["approval"] is None


def test_route_command_invalidates_matching_before_invites(setup):
    client, _ = setup
    state = match(client)
    r = post(client, state, "commands", {"text": "필름은 매각으로 바꿔 줘"})
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert state["session"]["feasibility"]["decisions"][0]["route"] == "sell"
    assert not state["bundles"]


def test_public_import_provenance_validation(tmp_path):
    import csv

    from scripts.import_public_data import read_records

    path = tmp_path / "sources.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["source_url", "retrieved_at", "license", "record_id", "payload_json"])
        writer.writerow(
            ["https://data.go.kr/example", "2026-11-13", "확인 필요", "1", '{"value":1}']
        )
    rows = read_records(path, "prices")
    assert len(rows) == 1 and rows[0].payload["verified"] is False
    with pytest.raises(ValueError):
        read_records(path, "unknown")


def test_migrations_upgrade_downgrade(tmp_path, monkeypatch):
    from sqlalchemy import inspect

    import app.platform.db as db_module

    eng = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    monkeypatch.setattr(db_module, "engine", lambda: eng)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    assert "platform_sessions" in inspect(eng).get_table_names()
    command.downgrade(config, "base")
    assert "platform_sessions" not in inspect(eng).get_table_names()
    eng.dispose()


def user_headers(uid="owner", email="owner@example.com", company="", role="admin"):
    return {
        "Authorization": "Bearer test-key",
        "X-User-Id": uid,
        "X-User-Email": email,
        "X-Company-Id": company,
        "X-Member-Role": role,
    }


def register(client, uid="owner", email="owner@example.com", biz="1234567890"):
    r = client.post(
        "/api/v1/organizations",
        headers=user_headers(uid, email),
        json={
            "name": "테스트 회사",
            "bizNo": biz,
            "industry": "제조",
            "complexId": "noksan",
            "role": "배출",
            "wasteStreams": [],
            "location": [128.8, 35.1],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_real_identity_membership_invitation_and_forged_role(setup):
    client, _ = setup
    cid = register(client)
    admin = user_headers(company=cid)
    invited = client.post(
        f"/api/v1/organizations/{cid}/members",
        headers=admin,
        json={"email": "member@example.com", "role": "담당자"},
    ).json()
    token = invited["token"]
    assert (
        client.post(
            "/api/v1/membership/accept",
            headers=user_headers("bad", "bad@example.com"),
            json={"token": token},
        ).status_code
        == 404
    )
    member = user_headers("member", "member@example.com", cid, role="admin")
    assert (
        client.post("/api/v1/membership/accept", headers=member, json={"token": token}).status_code
        == 200
    )
    assert (
        client.post("/api/v1/membership/accept", headers=member, json={"token": token}).status_code
        == 404
    )
    profile = client.get("/api/v1/companies/me", headers=member).json()
    assert client.put(f"/api/v1/companies/{cid}", headers=member, json=profile).status_code == 403
    assert (
        client.post(
            f"/api/v1/organizations/{cid}/members",
            headers=member,
            json={"email": "new@example.com", "role": "관리자"},
        ).status_code
        == 403
    )
    mid = invited["member"]["id"]
    assert (
        client.patch(
            f"/api/v1/organizations/{cid}/members/{mid}", headers=admin, json={"role": "승인권자"}
        ).status_code
        == 200
    )
    assert client.put(f"/api/v1/companies/{cid}", headers=member, json=profile).status_code == 200
    assert (
        client.delete(f"/api/v1/organizations/{cid}/members/{mid}", headers=admin).status_code
        == 200
    )
    assert client.get("/api/v1/sessions", headers=member).status_code == 403
    owner = client.get(f"/api/v1/organizations/{cid}/members", headers=admin).json()[0]
    assert (
        client.delete(
            f"/api/v1/organizations/{cid}/members/{owner['id']}", headers=admin
        ).status_code
        == 409
    )


def test_duplicate_organization_and_real_profile_is_not_demo(setup):
    client, _ = setup
    cid = register(client)
    duplicate = client.post(
        "/api/v1/organizations",
        headers=user_headers("other", "other@example.com"),
        json={
            "name": "다른 회사",
            "bizNo": "1234567890",
            "industry": "제조",
            "complexId": "noksan",
            "role": "배출",
            "wasteStreams": [],
        },
    )
    assert duplicate.status_code == 409
    h = user_headers(company=cid)
    p = client.get("/api/v1/companies/me", headers=h).json()
    p["isDemo"] = True
    assert client.put(f"/api/v1/companies/{cid}", headers=h, json=p).json()["isDemo"] is False
    assert client.get("/api/v1/sessions", headers=headers(cid)).status_code == 403


def test_demo_and_operational_companies_never_mix(setup):
    client, _ = setup
    cid = register(client)
    demo = client.get("/api/v1/companies", headers=headers()).json()
    assert len(demo) == 30 and all(c["isDemo"] for c in demo)
    real = client.get("/api/v1/companies", headers=user_headers(company=cid)).json()
    assert len(real) == 1 and real[0]["id"] == cid and not real[0]["isDemo"]


def test_permit_verification_is_operator_only_and_policy_edits_revoke_it(setup):
    from scripts.verify_company_permit import verify

    client, engine = setup
    cid = register(client)
    h = user_headers(company=cid)
    p = client.get("/api/v1/companies/me", headers=h).json()
    p["role"] = "처리업체"
    p["collector"] = {
        "permits": ["폐플라스틱"],
        "materials": ["LDPE"],
        "forms": ["film"],
        "minKg": 1000,
        "truckKg": 1000,
        "days": ["목"],
        "maxPerKg": 140,
        "maxPerMonth": 2,
        "permitVerified": True,
        "evidence": "사용자 주장",
    }
    r = client.put(f"/api/v1/companies/{cid}", headers=h, json=p)
    assert r.status_code == 200, r.text
    assert not r.json()["collector"]["permitVerified"]
    with Session(engine) as db:
        with pytest.raises(ValueError):
            verify(db, cid, "operator", "확인한 증빙", False)
        verify(db, cid, "operator", "확인한 증빙", True)
    p = client.get("/api/v1/companies/me", headers=h).json()
    assert p["collector"]["permitVerified"]
    p["collector"]["permits"] = ["포장재"]
    r = client.put(f"/api/v1/companies/{cid}", headers=h, json=p)
    assert r.status_code == 200 and not r.json()["collector"]["permitVerified"]


def completed(client):
    state = create(client)
    r = post(client, state, "advance", {"step": "proposal"})
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    state = consent(client, state)
    r = post(client, state, "advance", {"step": "proposal"})
    assert r.status_code == 200, r.text
    assert r.json()["blockedStep"] is None, r.text
    return r.json()["state"]


def test_full_stages_approval_freezes_every_mutation(setup):
    client, _ = setup
    state = completed(client)
    assert len(state["session"]["proposal"]["documents"]) == 6
    assert state["session"]["costCarbon"]["routes"][0]["carbonT"] is None
    assert (
        post(
            client, state, "approval", {"confirm": True, "routeId": "A"}, role="member"
        ).status_code
        == 403
    )
    assert post(client, state, "approval", {"confirm": True, "routeId": "D"}).status_code == 422
    r = post(client, state, "approval", {"confirm": True, "routeId": "A"})
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["session"]["status"] == "approved"
    stream = state["session"]["streams"][0]["id"]
    for action, body in [
        ("answers", {"streamId": stream, "monthlyKg": 400, "origin": "PCR"}),
        ("matching", {"streamId": stream, "form": "film"}),
        ("advance", {"step": "proposal"}),
        ("commands", {"text": "규정 다시 검토해 줘"}),
        ("invites", {"bundleId": state["bundles"][0]["id"], "confirm": True}),
    ]:
        assert post(client, state, action, body).status_code == 409


def test_operational_stage_missing_is_blocked_and_foreign_stream_is_rejected(setup, monkeypatch):
    from app.platform.models import PlatformState
    from app.platform.orchestrator import advance
    from app.platform.stages import handler

    client, _ = setup
    state = PlatformState.model_validate(create(client))
    blocked, _ = advance(state, "proposal", {"feasibility": handler("feasibility", state, False)})
    assert blocked == "feasibility" and state.session.feasibility is None
    monkeypatch.setattr(
        settings, "platform_stage_endpoints", {"feasibility": "https://example.invalid/b"}
    )
    from app.platform import stages

    monkeypatch.setattr(
        stages,
        "stage",
        lambda *args: {
            "result": {"decisions": [], "totalNow": 0, "totalAfter": 0, "totalGain": 0},
            "questions": [],
        },
    )
    blocked, _ = advance(state, "proposal", {"feasibility": handler("feasibility", state)})
    assert blocked == "feasibility" and state.session.feasibility is None


def test_classifier_preserves_human_answers(setup, monkeypatch):
    from app.platform import stages

    client, _ = setup
    state = create(client)
    state = post(
        client,
        state,
        "answers",
        {"streamId": state["session"]["streams"][0]["id"], "monthlyKg": 300, "origin": "PCR"},
    ).json()
    st = copy.deepcopy(state["session"]["streams"][0])
    st.update(
        {"monthlyKg": 9999, "monthlyKgConfirmed": True, "origin": "PIR", "originSource": "ai"}
    )
    monkeypatch.setattr(stages, "stage", lambda *args: {"streams": [st], "questions": []})
    r = post(client, state, "classify", {})
    assert r.status_code == 200, r.text
    actual = r.json()["session"]["streams"][0]
    assert actual["monthlyKg"] == 300 and actual["origin"] == "PCR"


def test_replacement_adds_new_candidates_without_consent(setup):
    client, engine = setup
    state = consent(client, match(client), "declined")
    old = state["bundles"][0]
    with Session(engine) as db:
        row = db.get(CompanyRow, "noksan-019")
        data = copy.deepcopy(row.payload)
        data.update(
            {
                "role": "배출",
                "location": [128.81, 35.10],
                "supplies": [
                    {
                        "item": "폐플라스틱",
                        "material": "LDPE",
                        "form": "film",
                        "origin": "PCR",
                        "monthlyKg": 1000,
                    }
                ],
            }
        )
        row.payload = data
        row.longitude, row.latitude = data["location"]
        db.commit()
    r = post(client, state, f"bundles/{old['id']}/replace", {})
    assert r.status_code == 200, r.text
    b = r.json()["bundles"][0]
    assert b["confirmedKg"] == 300 and not b["ready"]
    assert any(m["companyId"] == "noksan-019" and m["state"] == "not-sent" for m in b["members"])
    assert all(
        m["state"] == "declined"
        for m in b["members"]
        if m["companyId"] in {"noksan-017", "noksan-018"}
    )


def test_per_pickup_minimum_and_time_windows(setup):
    client, engine = setup
    state = consent(client, match(client))
    with Session(engine) as db:
        row = db.get(SessionRow, state["session"]["id"])
        data = copy.deepcopy(row.payload)
        data["bundles"][0]["minimumBasis"] = "pickup"
        row.payload = data
        collector = db.get(CompanyRow, data["bundles"][0]["collectorId"])
        profile = copy.deepcopy(collector.payload)
        profile["collector"]["minimumBasis"] = "pickup"
        collector.payload = profile
        db.commit()
    r = post(client, state, "negotiations", negotiation_body(state))
    assert r.status_code == 200, r.text
    assert r.json()["negotiation"]["status"] == "failed"
    with pytest.raises(ValueError):
        route_plan(
            [128, 35],
            [{"companyId": "far", "kg": 100, "location": [129, 36], "window": [480, 481]}],
            1000,
            shift=(480, 500),
        )


@pytest.mark.parametrize(
    "accepted,price,status", [(True, 140, "agreed"), (True, 100, "failed"), (False, 140, "failed")]
)
def test_a2a_real_wire_contract_and_guardrails(setup, accepted, price, status):
    import httpx

    from app.platform.adapters import a2a_negotiate
    from app.platform.models import Bundle, NegotiationRequest

    client, _ = setup
    state = consent(client, match(client))
    calls = []

    def peer(request):
        data = json.loads(request.content)
        calls.append(data)
        assert data["method"] == "message/send"
        assert data["params"]["message"]["parts"][0]["data"]["binding"] is False
        terms = {"day": "목", "slot": "오전", "perKg": price, "perMonth": 2}
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": data["id"],
                "result": {
                    "kind": "message",
                    "role": "agent",
                    "messageId": "peer",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "schema": "resource-pickup-v1",
                                "terms": terms,
                                "accepted": accepted,
                            },
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        result = a2a_negotiate(
            Bundle.model_validate(state["bundles"][0]),
            NegotiationRequest.model_validate(
                {"version": state["version"], **negotiation_body(state)}
            ),
            {"url": "https://peer.example/a2a"},
            "unique",
            http,
        )
    assert result.status == status and len(result.rounds) <= 6
    assert all(r.within for r in result.rounds if r.by == "ours")
    assert 1 <= len(calls) <= 3


def test_adapter_rejects_bad_response_without_secrets():
    import httpx

    from app.platform.adapters import AdapterError, call

    for response in [
        httpx.Response(302, headers={"location": "https://other.example"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
    ]:
        with (
            httpx.Client(
                transport=httpx.MockTransport(lambda req, result=response: result)
            ) as http,
            pytest.raises(AdapterError),
        ):
            call("https://api.example/test", {}, "secret", client=http)


def test_azure_maps_wire_and_unreachable_cells(monkeypatch):
    import httpx

    from app.platform.adapters import AdapterError
    from app.platform.road import matrix

    monkeypatch.setattr(settings, "azure_maps_key", "test-maps-key")
    monkeypatch.setattr(settings, "platform_road_matrix_url", "")

    def maps(request):
        assert request.headers["subscription-key"] == "test-maps-key"
        body = json.loads(request.content)
        assert body["features"][0]["properties"]["pointType"] == "origins"
        return httpx.Response(
            200,
            json={
                "properties": {
                    "matrix": [
                        {
                            "originIndex": i,
                            "destinationIndex": j,
                            "statusCode": 200,
                            "distanceInMeters": 0 if i == j else 999,
                            "durationInSeconds": 0 if i == j else 125,
                        }
                        for i in range(2)
                        for j in range(2)
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(maps)) as http:
        distance, minutes, basis = matrix([[128, 35], [128.1, 35.1], [128, 35]], http)
    assert distance[0][1] == 999 and distance[0][2] == 0 and minutes[0][1] == 3 and "Azure" in basis
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"properties": {"matrix": []}})
            )
        ) as http,
        pytest.raises(AdapterError),
    ):
        matrix([[128, 35]], http)


def test_public_source_pagination_atomicity_and_xml(monkeypatch):
    import httpx

    from scripts.collect_public_data import SourceConfig, collect, records

    monkeypatch.setenv("TEST_SOURCE_URL", "https://source.example/api")
    cfg = SourceConfig(
        kind="prices",
        source_url="https://source.example/catalog",
        endpoint_env="TEST_SOURCE_URL",
        license="공공누리 확인",
        page_param="page",
        page_size=1,
        fields={"material": "resin", "price": "price", "unit": "unit", "effective_date": "date"},
    )

    def source(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "r1",
                        "resin": "LDPE",
                        "price": "140",
                        "unit": "KRW/kg",
                        "date": "2026-11-13",
                    }
                ]
                if page == 1
                else []
            },
        )

    with httpx.Client(transport=httpx.MockTransport(source)) as http:
        rows = collect(cfg, http)
    assert (
        len(rows) == 1
        and rows[0].payload["record"]["price"] == 140
        and not rows[0].payload["verified"]
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "same",
                                "resin": "LDPE",
                                "price": 140,
                                "unit": "KRW/kg",
                                "date": "2026-11-13",
                            }
                        ]
                    },
                )
            )
        ) as http,
        pytest.raises(ValueError, match="중복"),
    ):
        collect(cfg, http)
    xml = cfg.model_copy(update={"format": "xml", "records_path": ".//item"})
    assert records(b"<root><item><id>1</id></item></root>", xml) == [{"id": "1"}]
    with pytest.raises(ValueError, match="DTD"):
        records(b'<!DOCTYPE x [<!ENTITY a "x">]><root/>', xml)


def test_delivery_explicit_confirmation_retry_uses_same_key(setup, monkeypatch):
    from app.platform import delivery
    from app.platform.adapters import AdapterError
    from app.platform.db import MemberRow, OrganizationRow

    client, engine = setup
    state = completed(client)
    with Session(engine) as db:
        row = db.get(SessionRow, state["session"]["id"])
        data = copy.deepcopy(row.payload)
        data["session"]["proposal"]["documents"][0]["external"] = True
        row.payload = data
        c = db.get(CompanyRow, "noksan-001")
        c.payload = {**c.payload, "isDemo": False}
        db.add(OrganizationRow(id=c.id, owner_id="tester", biz_no="1231231231", payload={}))
        db.flush()
        db.add(
            MemberRow(
                id="admin-test",
                company_id=c.id,
                user_id="tester",
                email="tester@example.com",
                role="관리자",
                payload={"status": "active"},
            )
        )
        db.commit()
    state = post(client, state, "approval", {"confirm": True, "routeId": "A"}).json()
    assert state["session"]["approval"]
    assert post(client, state, "deliveries", {"confirm": False}).status_code == 422
    monkeypatch.setattr(
        settings, "platform_stage_endpoints", {"dispatch": "https://dispatch.example"}
    )
    keys = []

    def send(url, payload, token, key):
        keys.append(key)
        if len(keys) == 1:
            raise AdapterError("timeout")
        return {"status": "sent", "receiptId": "receipt-1"}

    monkeypatch.setattr(delivery, "call", send)
    r = post(client, state, "deliveries", {"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()[0]["status"] == "uncertain"
    state = client.get(f"/api/v1/sessions/{state['session']['id']}", headers=headers()).json()
    r = post(client, state, "deliveries", {"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()[0]["status"] == "sent" and keys[0] == keys[1]
    state = client.get(f"/api/v1/sessions/{state['session']['id']}", headers=headers()).json()
    assert len(state["session"]["approval"]["sentTo"]) == 1
    assert post(client, state, "deliveries", {"confirm": True}).status_code == 200
    assert len(keys) == 2
