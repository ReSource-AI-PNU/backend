"""보관장 3D 스캔: 재구성 서버 호출(가짜 transport), 결과 확인, 더미별 부피·무게 근거, 정리안, 읽기 전용."""

import base64
import json
import struct

import pytest

from app.v3 import ids, service
from app.v3.demo.scope import demo_app
from app.v3.evidence.registry import EvidenceForbidden
from app.v3.scan import client as recon
from app.v3.scan import layout, result, views
from scripts.v3.scan_fake_recon import sample_points, sample_result
from tests.v3.runtime_fixtures import ADMIN, DAON, HANBIT, SAEGIL, make_app

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
FRAMES = [(f"f{i:03d}.jpg", JPEG + bytes([i])) for i in range(24)]


class FakeRecon:
    """재구성 서버를 흉내 내는 transport. GET /jobs/{id}를 받을 때마다 한 단계씩 나아간다."""

    def __init__(self, result_payload=None, points=None, fail_code=None, steps=2):
        self.result = result_payload if result_payload is not None else sample_result()
        self.points = points if points is not None else sample_points()
        self.result["pointsCount"] = struct.unpack_from("<4sIII", self.points)[2]
        self.fail_code, self.steps = fail_code, steps
        self.calls, self.polls, self.up = [], 0, True
        self.submitted = None

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if not self.up:
            raise OSError("connection refused")
        path = url.split("8090", 1)[1]
        if path == "/health":
            return 200, {}, b'{"ok": true}'
        if method == "POST" and path == "/jobs":
            self.submitted = (headers, body)
            return 202, {}, b'{"jobId": "job-1", "status": "queued"}'
        if path == "/jobs/job-1/points":
            return 200, {}, self.points
        if path == "/jobs/job-1":
            self.polls += 1
            if self.polls < self.steps:
                return (
                    200,
                    {},
                    json.dumps({"status": "running", "stage": "scale", "progress": 0.5}).encode(),
                )
            if self.fail_code:
                return (
                    200,
                    {},
                    json.dumps({"status": "failed", "error": {"code": self.fail_code}}).encode(),
                )
            return 200, {}, json.dumps({"status": "done", "result": self.result}).encode()
        return 404, {}, b'{"error": {"code": "NOT_FOUND", "message": "x"}}'


def scan_app(fake=None):
    fake = fake or FakeRecon()
    app = make_app(recon=recon.ReconClient("http://10.72.0.4:8090", fake))
    return app, fake


def finished(app, org=HANBIT):
    view = views.create_scan(app, org, FRAMES, "video", 32.0, org)
    for _ in range(5):
        view = views.get_scan(app, org, view["id"], org)
        if view["status"] != "processing":
            break
    assert view["status"] == "done", view
    return view


def evidence_ok(app, claim, org=HANBIT):
    assert claim["evidence"], claim
    for eid in claim["evidence"]:
        assert app.registry.get(eid, org) is not None


# ── 정리안 ──────────────────────────────────────────────────────────
def _piles(spec):
    return [
        {
            "id": pid,
            "footprint": layout.rectangle(c, size, yaw),
            "sizeM": list(size),
            "yawDeg": yaw,
            "volumeM3": size[0] * size[1],
        }
        for pid, c, size, yaw in spec
    ]


FLOOR = [(-4, -3), (4, -3), (4, 3), (-4, 3)]
SPEC = [
    ("p1", (-1.0, 0.5), (1.0, 1.0), 0),
    ("p2", (0.2, 0.6), (1.0, 1.0), 5),
    ("p3", (1.5, -1), (1.2, 0.8), 30),
    ("p4", (-2.5, -1.5), (0.6, 0.6), 0),
]


def test_layout_separates_different_waste_and_keeps_the_rules():
    piles = _piles(SPEC)
    zones = {"p1": "a", "p2": "b", "p3": "a", "p4": None}
    pickup = (0.0, -3.2)
    out = layout.propose(FLOOR, piles, zones, {"a": "A", "b": "B"}, pickup)
    assert out["unavailable"] is None
    assert out["current"]["mixedPairs"] == 2  # p2가 p1·p3에 붙어 있다
    after = out["proposal"]["metrics"]
    assert after["mixedPairs"] == 0 and after["minMixedGapM"] >= 1.0 - 1e-6
    assert after["weightedDistanceM"] < out["current"]["weightedDistanceM"]
    placed = {p["id"]: p for p in out["proposal"]["piles"]}
    assert placed["p4"]["zone"] is None and placed["p4"]["center"] == pytest.approx([-2.5, -1.5])
    for p in out["proposal"]["piles"]:
        for corner in p["footprint"]:  # 바닥 안, 벽에서 0.3m 안쪽
            assert -3.7 - 1e-6 <= corner[0] <= 3.7 + 1e-6 and -2.7 - 1e-6 <= corner[1] <= 2.7 + 1e-6
        assert layout.polygon_distance(p["footprint"], [pickup] * 3) >= 1.5 - 1e-6
    again = layout.propose(FLOOR, piles, zones, {"a": "A", "b": "B"}, pickup)
    assert again == out  # 같은 입력이면 같은 결과


def test_layout_needs_two_kinds_and_room():
    piles = _piles(SPEC)
    one = layout.propose(FLOOR, piles, {"p1": "a", "p2": "a"}, {"a": "A"}, (0, -3))
    assert one["proposal"] is None and "두 가지" in one["unavailable"]
    tiny = [(-1.2, -1), (1.2, -1), (1.2, 1), (-1.2, 1)]
    full = layout.propose(
        tiny, piles, {"p1": "a", "p2": "b", "p3": "a"}, {"a": "A", "b": "B"}, (0, -1)
    )
    assert full["proposal"] is None and "자리를 찾지 못했습니다" in full["unavailable"]


def test_polygon_distance_and_area():
    a = layout.rectangle((0, 0), (1, 1), 0)
    b = layout.rectangle((2, 0), (1, 1), 0)
    assert layout.polygon_distance(a, b) == pytest.approx(1.0)
    assert layout.polygon_distance(a, layout.rectangle((0.5, 0), (1, 1), 45)) == 0.0
    assert layout.polygon_area(a) == pytest.approx(1.0)


# ── 주소와 결과 모양 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url,ok",
    [
        ("http://10.72.0.4:8090", True),
        ("http://192.168.0.3:8090", True),
        ("https://recon.example.com", True),
        ("http://8.8.8.8:8090", False),
        ("http://recon.example.com", False),
        ("ftp://10.0.0.1", False),
    ],
)
def test_recon_url_must_be_private_http_or_https(url, ok):
    if ok:
        assert recon.client_from_env({recon.ENV_URL: url}) is not None
    else:
        with pytest.raises(ValueError):
            recon.client_from_env({recon.ENV_URL: url})
    assert recon.client_from_env({}) is None


def test_multipart_is_deterministic_and_keeps_every_frame():
    first = recon.multipart(FRAMES[:3])
    assert first == recon.multipart(FRAMES[:3])
    assert first[1].count(b'name="frames"') == 3


def test_result_shape_is_checked():
    good = sample_result()
    assert result.read_result(good)["piles"][0]["id"] == "p1"
    bad = sample_result()
    bad["piles"][0]["volumeM3"] = 99.0  # 범위 밖
    with pytest.raises(result.ResultError):
        result.read_result(bad)
    bad = sample_result()
    bad["piles"][0]["thumbnail"] = "javascript:alert(1)"
    with pytest.raises(result.ResultError):
        result.read_result(bad)
    bad = sample_result()
    bad["piles"][1]["id"] = "p1"
    with pytest.raises(result.ResultError):
        result.read_result(bad)
    points = sample_points()
    assert result.read_points(points, 5) > 1000
    with pytest.raises(result.ResultError):
        result.read_points(points[:-3], 5)
    with pytest.raises(result.ResultError):
        result.read_points(points, 2)  # 더미 번호가 더미 수보다 크다


# ── 올리기에서 무게까지 ──────────────────────────────────────────────
def test_upload_progress_done_and_volume_evidence():
    app, fake = scan_app(FakeRecon(steps=3))
    view = views.create_scan(app, HANBIT, FRAMES, "video", 32.0, HANBIT)
    assert view["status"] == "processing" and view["summary"] is None
    assert fake.submitted[1].count(b'name="frames"') == 24
    view = views.get_scan(app, HANBIT, view["id"], HANBIT)
    assert view["status"] == "processing" and view["progress"] == {"stage": "scale", "ratio": 0.5}
    for _ in range(4):
        view = views.get_scan(app, HANBIT, view["id"], HANBIT)
    assert view["status"] == "done"
    assert view["summary"]["pileCount"] == 5 and view["summary"]["weightPending"] == 5
    assert view["summary"]["weight"] is None  # 무게 기준이 없으면 산정 보류
    evidence_ok(app, view["summary"]["volume"])
    for pile in view["piles"]:
        evidence_ok(app, pile["volume"])
        model = app.registry.get(pile["volume"]["evidence"][0], HANBIT)
        assert model.kind == "model" and model.access == (HANBIT,)
        with pytest.raises(EvidenceForbidden):
            app.registry.get(pile["volume"]["evidence"][0], DAON)
        assert "fake" not in model.excerpt and "VGGT" not in model.excerpt
    assert view["piles"][2]["note"]  # 덜 찍힌 더미에는 안내가 붙는다
    assert views.scan_points(app, HANBIT, view["id"], HANBIT) == fake.points
    assert view["layout"]["proposal"] is None and view["layout"]["unavailable"]


def test_weight_from_stream_density_film_reference_and_weighing():
    app, _ = scan_app()
    view = finished(app)
    sid = view["id"]
    service.update_stream(app, HANBIT, "st_film", {"bulkDensityKgPerM3": None}, HANBIT)
    view = views.update_pile(app, HANBIT, sid, "p1", {"streamId": "st_sprue"}, HANBIT)
    view = views.update_pile(app, HANBIT, sid, "p2", {"streamId": "st_sprue"}, HANBIT)
    view = views.update_pile(app, HANBIT, sid, "p5", {"streamId": "st_film"}, HANBIT)
    piles = {p["id"]: p for p in view["piles"]}
    lo, hi = piles["p1"]["volumeRangeM3"]
    assert piles["p1"]["density"]["basis"] == "stream" and piles["p1"]["density"]["lowKgM3"] == 250
    assert piles["p1"]["weight"]["low"]["value"] == round(lo * 250)
    assert piles["p1"]["weight"]["high"]["value"] == round(hi * 250)
    evidence_ok(app, piles["p1"]["weight"]["low"])
    calc = app.registry.get(piles["p1"]["weight"]["low"]["evidence"][0], HANBIT)
    assert calc.kind == "calc" and views._volume_id({"id": sid}, "p1") in calc.meta["inputEvidence"]
    film = piles["p5"]["density"]
    assert film["basis"] == "ref" and film["lowKgM3"] == pytest.approx(20.8, abs=0.05)
    assert film["highKgM3"] == pytest.approx(89.0, abs=0.05)
    assert all(app.registry.get(e, DAON).kind == "ref" for e in film["evidence"])  # 문헌값은 공개
    assert view["summary"]["weightPending"] == 2 and view["summary"]["weight"]["low"]["value"] > 0
    # 계량 보정: 같은 폐기물의 다른 더미에 같은 밀도를 쓴다
    view = views.update_pile(app, HANBIT, sid, "p1", {"weighedKg": 400}, HANBIT)
    piles = {p["id"]: p for p in view["piles"]}
    density = 400 / piles["p1"]["volume"]["value"]
    assert piles["p2"]["density"]["basis"] == "weighed"
    assert piles["p2"]["density"]["lowKgM3"] == pytest.approx(round(density, 1))
    assert piles["p1"]["weight"]["low"]["value"] == 400 and piles["p1"]["weighedKg"]["value"] == 400
    human = app.registry.get(piles["p1"]["weighedKg"]["evidence"][0], HANBIT)
    assert human.kind == "human" and human.value == 400
    # 폐기물을 지우면 계량값도 지운다
    view = views.update_pile(app, HANBIT, sid, "p1", {"streamId": None}, HANBIT)
    piles = {p["id"]: p for p in view["piles"]}
    assert piles["p1"]["streamId"] is None and piles["p1"]["weighedKg"] is None
    assert piles["p2"]["density"]["basis"] == "stream"


def test_layout_appears_after_two_kinds_and_pickup_can_move():
    app, _ = scan_app()
    sid = finished(app)["id"]
    for pid, stream in (
        ("p1", "st_sprue"),
        ("p2", "st_sprue"),
        ("p5", "st_film"),
        ("p3", "st_defect"),
    ):
        view = views.update_pile(app, HANBIT, sid, pid, {"streamId": stream}, HANBIT)
    plan = view["layout"]
    assert plan["unavailable"] is None and plan["pickup"]["source"] == "start"
    assert plan["current"]["mixedPairs"] >= 1 and plan["proposal"]["metrics"]["mixedPairs"] == 0
    assert {z["streamId"] for z in plan["proposal"]["zones"]} == {
        "st_sprue",
        "st_film",
        "st_defect",
    }
    assert all(app.registry.get(e, HANBIT).kind == "assumption" for e in plan["rules"]["evidence"])
    moved = views.update_layout(app, HANBIT, sid, {"pickup": {"x": 3.0, "y": 2.0}}, HANBIT)
    assert moved["layout"]["pickup"] == {"x": 3.0, "y": 2.0, "source": "manual"}
    assert moved["layout"]["proposal"] != plan["proposal"]
    back = views.update_layout(app, HANBIT, sid, {"pickup": None}, HANBIT)
    assert back["layout"]["pickup"]["source"] == "start"
    with pytest.raises(service.ServiceError) as far:
        views.update_layout(app, HANBIT, sid, {"pickup": {"x": 500, "y": 0}}, HANBIT)
    assert far.value.status == 422


def test_stream_change_recomputes_on_read():
    app, _ = scan_app()
    sid = finished(app)["id"]
    views.update_pile(app, HANBIT, sid, "p1", {"streamId": "st_sprue"}, HANBIT)
    service.update_stream(app, HANBIT, "st_sprue", {"bulkDensityKgPerM3": 500}, HANBIT)
    view = views.get_scan(app, HANBIT, sid, HANBIT)
    pile = view["piles"][0]
    assert pile["density"]["lowKgM3"] == 500
    assert pile["weight"]["low"]["value"] == round(pile["volumeRangeM3"][0] * 500)


# ── 막는 것 ─────────────────────────────────────────────────────────
def _code(fn, *args):
    with pytest.raises(service.ServiceError) as error:
        fn(*args)
    return error.value.code, error.value.status


def test_upload_rules():
    app, fake = scan_app()
    assert _code(
        views.create_scan, app, HANBIT, [("a.txt", b"hello")] * 20, "video", None, HANBIT
    ) == ("UNSUPPORTED_MEDIA", 415)
    assert _code(views.create_scan, app, HANBIT, FRAMES[:5], "video", None, HANBIT) == (
        "TOO_FEW_FRAMES",
        422,
    )
    assert _code(views.create_scan, app, HANBIT, FRAMES * 5, "video", None, HANBIT) == (
        "TOO_LARGE",
        413,
    )
    assert _code(views.create_scan, app, DAON, FRAMES, "video", None, HANBIT)[1] == 403
    assert _code(views.create_scan, app, SAEGIL, FRAMES, "video", None, SAEGIL) == (
        "WRONG_ROLE",
        403,
    )
    first = views.create_scan(app, HANBIT, FRAMES, "video", None, HANBIT)
    busy = _code(views.create_scan, app, HANBIT, FRAMES, "video", None, HANBIT)
    assert busy == ("SCAN_IN_PROGRESS", 409)
    for _ in range(3):
        views.get_scan(app, HANBIT, first["id"], HANBIT)
    fake.up = False
    assert _code(views.create_scan, app, HANBIT, FRAMES, "video", None, HANBIT) == (
        "SCAN_UNAVAILABLE",
        503,
    )
    nothing = make_app()
    assert _code(views.create_scan, nothing, HANBIT, FRAMES, "video", None, HANBIT) == (
        "SCAN_UNAVAILABLE",
        503,
    )
    assert views.list_scans(nothing, HANBIT, HANBIT) == {"canScan": False, "scans": []}  # fmt: skip


def test_other_company_cannot_see_or_change_a_scan():
    app, _ = scan_app()
    sid = finished(app)["id"]
    assert _code(views.get_scan, app, HANBIT, sid, DAON)[1] == 403
    assert _code(views.get_scan, app, DAON, sid, DAON) == ("SCAN_NOT_FOUND", 404)
    assert _code(views.scan_points, app, DAON, sid, DAON) == ("SCAN_NOT_FOUND", 404)
    assert _code(views.update_pile, app, HANBIT, sid, "p1", {"streamId": "st_daon_pp"}, HANBIT) == (
        "STREAM_NOT_FOUND",
        404,
    )
    assert _code(views.update_pile, app, HANBIT, sid, "p9", {"streamId": "st_sprue"}, HANBIT) == (
        "PILE_NOT_FOUND",
        404,
    )
    assert _code(views.update_pile, app, HANBIT, sid, "p1", {"weighedKg": 10}, HANBIT) == (
        "STREAM_REQUIRED",
        422,
    )
    assert _code(views.update_pile, app, HANBIT, sid, "p1", {"weighedKg": -1}, HANBIT)[1] == 422
    assert views.list_scans(app, DAON, DAON)["scans"] == []


def test_failures_become_user_sentences():
    app, _ = scan_app(FakeRecon(fail_code="NO_FLOOR"))
    view = views.create_scan(app, HANBIT, FRAMES, "video", None, HANBIT)
    for _ in range(4):
        view = views.get_scan(app, HANBIT, view["id"], HANBIT)
    assert view["status"] == "failed" and "바닥" in view["failure"]["message"]
    payload = sample_result()
    payload["piles"] = []
    app, _ = scan_app(FakeRecon(result_payload=payload, points=sample_points(())))
    view = views.create_scan(app, HANBIT, FRAMES, "video", None, HANBIT)
    for _ in range(4):
        view = views.get_scan(app, HANBIT, view["id"], HANBIT)
    assert view["status"] == "failed" and "더미" in view["failure"]["message"]
    assert _code(views.scan_points, app, HANBIT, view["id"], HANBIT) == ("POINTS_NOT_READY", 404)


def test_stale_scan_fails_when_the_server_never_answers():
    now = ["2026-11-17T01:00:00+00:00"]
    fake = FakeRecon(steps=99)
    app = make_app(recon=recon.ReconClient("http://10.72.0.4:8090", fake))
    app.store._clock = lambda: now[0]
    view = views.create_scan(app, HANBIT, FRAMES, "video", None, HANBIT)
    fake.up = False
    assert views.get_scan(app, HANBIT, view["id"], HANBIT)["status"] == "processing"
    now[0] = "2026-11-17T01:30:00+00:00"
    view = views.get_scan(app, HANBIT, view["id"], HANBIT)
    assert view["status"] == "failed" and view["failure"]["message"]


def test_demo_view_is_read_only_and_shows_only_demo_scans():
    app, _ = scan_app()
    sid = finished(app)["id"]
    views.update_pile(app, HANBIT, sid, "p1", {"streamId": "st_sprue"}, HANBIT)
    app.store.put_org({**app.store.get_org(HANBIT), "demo": True})
    demo = demo_app(app)
    shown = views.get_scan(demo, HANBIT, sid, HANBIT)
    assert shown["piles"][0]["weight"] is not None
    assert views.list_scans(demo, HANBIT, HANBIT)["canScan"] is False
    with pytest.raises(service.ServiceError) as error:
        views.update_pile(demo, HANBIT, sid, "p2", {"streamId": "st_sprue"}, HANBIT)
    assert error.value.code == "DEMO_READ_ONLY"
    # 실제 조직의 스캔은 체험하기 앱에서 보이지 않는다
    other_app, _ = scan_app()
    other = finished(other_app, DAON)
    other_app.store.put_org({**other_app.store.get_org(DAON), "demo": False})  # 실제 조직으로
    hidden = demo_app(other_app)
    assert hidden.store.list_scans(DAON) == []
    with pytest.raises(KeyError):
        hidden.store.get_scan(other["id"])
    with pytest.raises(KeyError):
        hidden.store.get_scan_points(other["id"])
    assert _code(views.list_scans, hidden, DAON, DAON)[1] in (401, 403, 404)
    assert _code(views.get_scan, hidden, DAON, other["id"], DAON)[1] in (401, 403, 404)


def test_points_round_trip_through_the_store_as_base64():
    app, fake = scan_app()
    sid = finished(app)["id"]
    stored = app.store.get_scan_points(sid)
    assert base64.b64decode(stored["data"]) == fake.points and stored["orgId"] == HANBIT
    app.store.purge_orgs([HANBIT])
    assert app.store.list_scans(HANBIT) == []


def test_evidence_ids_follow_the_registry_rules():
    app, _ = scan_app()
    sid = finished(app)["id"]
    assert ids.model_id(views.MODEL_NAME, views.MODEL_VERSION, f"{sid}-p1-부피").startswith(
        "model:"
    )


# ── HTTP 경로 (FastAPI와 로컬 서버) ─────────────────────────────────
def test_http_routes_upload_view_points_and_edit(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.v3 import api

    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "v3_llm", "rule")
    app, fake = scan_app()
    api.set_state(app)
    try:
        with TestClient(fastapi_app) as client:
            head = {"X-Org-Id": HANBIT}
            files = [("frames", (name, data, "image/jpeg")) for name, data in FRAMES]
            started = client.post(
                "/api/v3/intake/scan",
                headers=head,
                data={"orgId": HANBIT, "source": "video", "durationSec": "31.5"},
                files=files,
            )
            assert started.status_code == 202, started.text
            sid = started.json()["id"]
            assert fake.submitted[1].count(b'name="frames"') == 24
            for _ in range(4):
                view = client.get(f"/api/v3/orgs/{HANBIT}/scans/{sid}", headers=head).json()
            assert view["status"] == "done"
            listed = client.get(f"/api/v3/orgs/{HANBIT}/scans", headers=head).json()
            assert listed["canScan"] is True and listed["scans"][0]["id"] == sid
            points = client.get(f"/api/v3/orgs/{HANBIT}/scans/{sid}/points", headers=head)
            assert points.status_code == 200 and points.content == fake.points
            edited = client.put(
                f"/api/v3/orgs/{HANBIT}/scans/{sid}/piles/p1",
                headers=head,
                json={"streamId": "st_sprue"},
            )
            assert (
                edited.status_code == 200
                and edited.json()["piles"][0]["streamName"] == "스프루·런너"
            )
            moved = client.put(
                f"/api/v3/orgs/{HANBIT}/scans/{sid}/layout",
                headers=head,
                json={"pickup": {"x": 1, "y": 1}},
            )
            assert moved.json()["layout"]["pickup"]["source"] == "manual"
            hidden = client.get(f"/api/v3/orgs/{HANBIT}/scans/{sid}", headers={"X-Org-Id": DAON})
            assert hidden.status_code == 403
            bad = client.post("/api/v3/intake/scan", headers=head, json={"x": 1})
            assert bad.status_code == 400
    finally:
        api.set_state(None)


def test_devserver_reads_every_frame():
    from app.v3 import devserver

    app, _ = scan_app()
    content_type, body = recon.multipart(FRAMES)
    request = devserver.Request(
        "POST",
        "/api/v3/intake/scan",
        {},
        {"Content-Type": content_type, "X-Org-Id": HANBIT},
        body,
    )
    view = devserver.route(app, request)
    assert view["status"] == "processing" and view["frameCount"] == 24


def test_demo_seed_puts_a_finished_scan_when_assets_exist(tmp_path, monkeypatch):
    from app.v3.scan import demo as scan_demo

    payload = sample_result()
    points = sample_points()
    payload["pointsCount"] = struct.unpack_from("<4sIII", points)[2]
    (tmp_path / "hanbit-result.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "hanbit-points.bin").write_bytes(points)
    truth = {
        "assign": {"p1": "st_sprue", "p5": "st_film", "p9": "st_sprue"},
        "weighed": {"p1": 240},
    }
    (tmp_path / "hanbit-truth.json").write_text(json.dumps(truth), encoding="utf-8")
    monkeypatch.setattr(scan_demo, "ASSETS", tmp_path)
    app = make_app()
    view = views.get_scan(demo_app(app), HANBIT, scan_demo.SCAN_ID, HANBIT)
    assert view["status"] == "done"
    piles = {p["id"]: p for p in view["piles"]}
    assert piles["p1"]["streamId"] == "st_sprue" and piles["p5"]["streamId"] == "st_film"
    assert piles["p1"]["weighedKg"]["value"] == 240 and piles["p1"]["density"]["basis"] == "weighed"
    assert view["layout"]["proposal"] is not None
    for pile in view["piles"]:
        for claim in (pile["volume"], *(pile["weight"] or {}).values()):
            evidence_ok(app, claim)
    again = service.reset_demo(app, ADMIN)  # 되돌려도 같은 ID로 다시 들어간다
    assert again["scanId"] == scan_demo.SCAN_ID


def test_demo_seed_skips_without_assets(tmp_path, monkeypatch):
    from app.v3.scan import demo as scan_demo

    monkeypatch.setattr(scan_demo, "ASSETS", tmp_path)
    app = make_app()
    assert app.store.list_scans(HANBIT) == []


# ── 금붕어 검증에서 나온 것 ──────────────────────────────────────────
def test_odd_result_closes_the_scan_instead_of_500():
    payload = sample_result()
    payload["warnings"] = 5
    app, _ = scan_app(FakeRecon(result_payload=payload))
    view = views.create_scan(app, HANBIT, FRAMES, "video", None, HANBIT)
    for _ in range(3):
        view = views.get_scan(app, HANBIT, view["id"], HANBIT)
    assert view["status"] == "failed" and "잠시 뒤 다시 올려" in view["failure"]["message"]
    assert views.list_scans(app, HANBIT, HANBIT)["scans"][0]["status"] == "failed"
    bad = sample_result()
    bad["piles"][0]["id"] = "p 1"
    with pytest.raises(result.ResultError):
        result.read_result(bad)
    bad = sample_result()
    bad["piles"][0]["thumbnail"] = 'data:image/jpeg;base64,"><script>x</script>'
    with pytest.raises(result.ResultError):
        result.read_result(bad)


def test_calc_evidence_links_its_inputs_and_metrics_have_evidence():
    from app.v3.evidence import search

    app, _ = scan_app()
    sid = finished(app)["id"]
    views.update_pile(app, HANBIT, sid, "p1", {"streamId": "st_sprue"}, HANBIT)
    view = views.update_pile(app, HANBIT, sid, "p5", {"streamId": "st_film"}, HANBIT)
    weight = view["piles"][0]["weight"]["low"]["evidence"][0]
    chain = search.evidence_chain(app.registry, weight, HANBIT)
    assert {c["id"] for c in chain} >= {views._volume_id({"id": sid}, "p1")}
    total = view["summary"]["volume"]["evidence"][0]
    assert len(search.evidence_chain(app.registry, total, HANBIT)) == 5
    evidence_ok(app, view["summary"]["floorArea"])
    for side in (view["layout"]["current"], view["layout"]["proposal"]["metrics"]):
        for key in ("mixedPairs", "weightedDistanceM"):
            ev = app.registry.get(side["evidence"][key], HANBIT)
            assert ev.kind == "calc" and ev.value == side[key]
    for ev in app.registry.list(org_id=HANBIT):
        if ev.id.startswith(("model:resource-ai-scan3d", "calc:scan_")):
            assert sid not in ev.source and "models" not in ev.meta


def test_wall_margin_holds_on_slanted_walls():
    floor = [(0, 0), (10, 0), (10, 2), (6, 6), (0, 6)]
    spec = [
        ("p1", (2, 2), (1.2, 1.0), 0),
        ("p2", (3.5, 2), (1.2, 1.0), 0),
        ("p3", (2, 4), (1.0, 1.0), 0),
        ("p4", (4, 4), (1.4, 1.0), 0),
    ]
    piles = _piles(spec)
    zones = {"p1": "a", "p2": "b", "p3": "a", "p4": "b"}
    out = layout.propose(floor, piles, zones, {"a": "A", "b": "B"}, (8, 3))
    assert out["proposal"] is not None
    edges = [[floor[i], floor[(i + 1) % len(floor)]] for i in range(len(floor))]
    for p in out["proposal"]["piles"]:
        rect = [tuple(v) for v in p["footprint"]]
        for edge in edges:
            assert layout.polygon_distance(rect, edge + [edge[1]]) >= 0.3 - 1e-3, p


def test_inputs_that_used_to_slip_through():
    app, fake = scan_app()
    view = views.create_scan(app, HANBIT, FRAMES, "video", float("inf"), HANBIT)
    assert app.store.get_scan(view["id"])["durationSec"] is None
    json.dumps(app.store.get_scan(view["id"]), allow_nan=False)
    for _ in range(3):
        views.get_scan(app, HANBIT, view["id"], HANBIT)
    sid = view["id"]
    views.update_pile(app, HANBIT, sid, "p1", {"streamId": "st_sprue"}, HANBIT)
    assert _code(views.update_pile, app, HANBIT, sid, "p1", {"weighedKg": 90000}, HANBIT) == (
        "INVALID_VALUE",
        422,
    )
    for url in ("http://169.254.169.254", "http://0.0.0.0:8090", "http://[2002::1]:8090"):
        with pytest.raises(ValueError):
            recon.client_from_env({recon.ENV_URL: url})
    with pytest.raises(recon.ReconError):
        recon.ReconClient("http://10.72.0.4:8090", fake).job("../health")


def test_evidence_dates_are_korean_days():
    assert views._kst_day("2026-11-19T17:30:00+00:00") == "2026-11-20"
    assert views._shot({"createdAt": "2026-11-19T17:30:00+00:00"}) == "2026-11-20 촬영"
    assert views._kst_day("2026-11-19") == "2026-11-19"


def test_a_pile_with_no_room_stays_and_the_rest_still_move():
    floor = [(-4, -3), (4, -3), (4, 3), (-4, 3)]
    spec = [
        ("big", (0.0, 0.5), (9.0, 1.0), 0),  # 바닥보다 긴 더미: 옮길 자리가 없다
        ("p2", (-3.0, -2.0), (0.8, 0.8), 0),
        ("p3", (-2.0, -2.0), (0.8, 0.8), 0),
        ("p4", (3.0, -2.2), (0.8, 0.8), 0),
    ]
    piles = _piles(spec)
    zones = {"big": "a", "p2": "b", "p3": "a", "p4": "b"}
    out = layout.propose(floor, piles, zones, {"a": "A", "b": "B"}, (0, -3.2))
    proposal = out["proposal"]
    assert proposal is not None and proposal["kept"] == ["big"]
    placed = {p["id"]: p for p in proposal["piles"]}
    assert placed["big"]["zone"] == "a"
    assert placed["big"]["center"] == pytest.approx([0.0, 0.5])
    assert proposal["moved"] >= 1
    assert proposal["metrics"]["mixedPairs"] <= out["current"]["mixedPairs"]


def test_portrait_frames_get_a_landscape_note():
    payload = sample_result()
    payload["frames"].update(width=294, height=518)
    app, _ = scan_app(FakeRecon(result_payload=payload))
    view = finished(app)
    assert "가로" in view["quality"]["notes"][0]
    app, _ = scan_app()  # 표본 장면은 가로(518×294)
    assert not any("세로" in n for n in finished(app)["quality"]["notes"])
