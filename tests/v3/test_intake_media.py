"""사진·영상 경로: 탐지 엔드포인트 호출(가짜 transport), 추론 결과 계약 읽기, model 근거 등록."""

import base64
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from app.v3 import devserver, ids, service
from app.v3.evidence.registry import EvidenceForbidden
from app.v3.intake import detector
from tests.v3.runtime_fixtures import ADMIN, DAON, HANBIT, SAEGIL, TODAY, make_app, to_approval

ENDPOINT = "https://rsai-detector.example.inference.ml.azure.com/score"

# app/ml/track.py의 run() 결과 모양(가상 값). 사진 한 장
PHOTO = {
    "stage": "classify",
    "backend": "trained",
    "model_version": "v5",
    "mode": "photos",
    "primary_material": "plastic_film",
    "items": [
        {
            "material": "plastic_film",
            "label_ko": "폐플라스틱(합성수지)",
            "count_estimate": 3,
            "area_ratio": 0.8,
            "frame_coverage": 0.6,
            "confidence": 0.82,
            "contaminated_ratio": 0.333,
            "multi_material_ratio": 0.0,
        },
        {"material": "paper", "count_estimate": 1, "area_ratio": 0.2, "confidence": 0.7},
    ],
    "input_domain": "aihub137_field",
    "contamination": {
        "overall_ratio": 0.125,
        "multi_material_ratio": 0.0,
        "judged_objects": 3,
        "reliable": False,
        "note": "현장 사진: 사람 라벨 검증 없음. 사용자 확인 권장",
    },
    "detections": [
        {
            "id": "d0",
            "label": "plastic_film",
            "kind": "item",
            "confidence": 0.82,
            "box": [0.1, 0.1, 0.5, 0.5],
            "ratio": 0.25,
            "conds": ["contaminated"],
        },
        {
            "id": "c0",
            "label": "이물질",
            "kind": "contaminant",
            "confidence": 0.6,
            "box": [0.2, 0.2, 0.1, 0.1],
            "ratio": 0.04,
            "ratio_basis": "면적 기준 추정",
        },
    ],
    "rep_frame": 0,
    "frames_analyzed": 1,
    "tracks_total": 0,
    "needs_followup": False,
    "followup_questions": [],
    "latency_sec": 0.4,
}
# 영상: 같은 계약의 mode와 tracks_total(ByteTrack 트랙 수)
VIDEO = {**PHOTO, "mode": "video", "frames_analyzed": 48, "tracks_total": 7, "rep_frame": 12}


def fake(payload, status=200, calls=None, wrap=False):
    def transport(method, url, headers, body):
        if calls is not None:
            calls.append({"method": method, "url": url, "headers": headers, "body": body})
        text = json.dumps(payload, ensure_ascii=False)
        raw = json.dumps(text) if wrap else text
        return status, {}, raw.encode("utf-8")

    return transport


def client(payload=PHOTO, **options):
    calls = options.pop("calls", None)
    return detector.DetectorClient(ENDPOINT, "test-key", fake(payload, calls=calls, **options))


def fails(code, status, call, *args):
    with pytest.raises(service.ServiceError) as caught:
        call(*args)
    assert (caught.value.code, caught.value.status) == (code, status), caught.value.to_dict()
    return caught.value


# ── 클라이언트 ───────────────────────────────────────────────────────
def test_the_ml_contract_module_is_pure_python():
    """핵심 코드가 가져다 쓰는 app.ml.contract가 네이티브 확장을 끌고 오지 않는다(새 프로세스에서 본다)."""
    code = (
        "import sys, app.v3.intake.detector as d;"
        "heavy = ('numpy', 'torch', 'cv2', 'supervision', 'transformers', 'pydantic');"
        "print([n for n in heavy if n in sys.modules], d.ITEM_OF['plastic_film'])"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert done.stdout.strip() == "[] 포장재"
    assert detector.LABEL_KO["plastic_film"]


def test_client_sends_the_file_to_the_endpoint_and_reads_the_contract():
    calls = []
    found = client(calls=calls).detect(b"\xff\xd8jpeg-bytes", "필름 사진.JPG")
    assert len(calls) == 1 and calls[0]["method"] == "POST" and calls[0]["url"] == ENDPOINT
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    sent = json.loads(calls[0]["body"])["input_data"]
    assert sent["filename"] == "필름 사진.JPG" and sent["media_type"] == "image"
    assert base64.b64decode(sent["content_base64"]) == b"\xff\xd8jpeg-bytes"
    assert found["mode"] == "photos" and found["primary"] == "plastic_film"
    assert found["modelVersion"] == "v5" and found["tracksTotal"] == 0
    assert found["contamination"] == {
        "overallRatio": 0.125,
        "reliable": False,
        "note": "현장 사진: 사람 라벨 검증 없음. 사용자 확인 권장",
        "judgedObjects": 3,
    }
    assert [d["id"] for d in found["detections"]] == ["d0", "c0"]
    assert "conds" not in found["detections"][0]  # 계약의 Detection 항목만 내보낸다
    named = detector.DetectorClient(ENDPOINT, "k", fake(PHOTO, calls=calls), deployment="blue")
    named.detect(b"x", "a.png")
    assert calls[-1]["headers"]["azureml-model-deployment"] == "blue"
    # score.py가 JSON 문자열로 한 번 더 감싸 보내도 읽는다
    assert client(wrap=True).detect(b"x", "a.png")["primary"] == "plastic_film"


def test_video_follows_the_tracking_shape_of_the_contract():
    calls = []
    found = client(VIDEO, calls=calls).detect(b"mp4-bytes", "컨베이어.mp4")
    assert json.loads(calls[0]["body"])["input_data"]["media_type"] == "video"
    assert found["mode"] == "video" and found["framesAnalyzed"] == 48
    assert found["tracksTotal"] == 7 and found["repFrame"] == 12
    with pytest.raises(detector.DetectorError):  # 사진을 보냈는데 영상 결과가 오면 받지 않는다
        client(VIDEO).detect(b"x", "a.jpg")


def test_client_refuses_bad_answers_instead_of_filling_them_in():
    assert detector.media_type("a.JPEG") == "image" and detector.media_type("a.mov") == "video"
    assert detector.media_type("a.pdf") is None and detector.media_type("사진") is None
    unset = detector.DetectorClient("", "k", fake(PHOTO))
    assert unset.configured is False
    with pytest.raises(detector.DetectorError):
        unset.detect(b"x", "a.jpg")
    with pytest.raises(detector.DetectorError):
        client().detect(b"x", "a.pdf")
    with pytest.raises(detector.DetectorError):
        client(status=503).detect(b"x", "a.jpg")
    bad = (
        {},
        {**PHOTO, "items": "none"},
        {**PHOTO, "items": [{"material": "plastic", "area_ratio": 2, "confidence": 0.5}]},
        {**PHOTO, "primary_material": "metal"},
        {**PHOTO, "contamination": {"overall_ratio": "많음"}},
        {**PHOTO, "contamination": {"overall_ratio": 1.5}},
        {**PHOTO, "mode": "stream"},
        {**PHOTO, "detections": "none"},
    )
    for payload in bad:
        with pytest.raises(detector.DetectorError):
            client(payload).detect(b"x", "a.jpg")

    def broken(method, url, headers, body):
        raise OSError("connection reset")

    with pytest.raises(detector.DetectorError):
        detector.DetectorClient(ENDPOINT, "k", broken).detect(b"x", "a.jpg")
    assert detector.client_from_env({}) is None
    made = detector.client_from_env({"V3_DETECTOR_ENDPOINT": ENDPOINT, "V3_DETECTOR_API_KEY": "k"})
    assert made.configured and made.endpoint_url == ENDPOINT


# ── 서비스 ───────────────────────────────────────────────────────────
def test_without_an_endpoint_nothing_is_detected_or_registered():
    calls = []
    for unset in (None, detector.DetectorClient("", "k", fake(PHOTO, calls=calls))):
        app = make_app(detector=unset)
        before = (len(app.registry.list(org_id=None)), app.store.get_stream("st_film"))
        result = service.upload_media(app, HANBIT, "st_film", "필름.jpg", b"jpeg", HANBIT)
        assert result == {
            "detectorUnavailable": True,
            "message": "탐지 모델이 연결되지 않았습니다",
            "streamId": "st_film",
            "evidence": [],
        }
        assert (len(app.registry.list(org_id=None)), app.store.get_stream("st_film")) == before
        assert service.admin_status(app, ADMIN)["detector"] == {"configured": False}
    assert calls == []  # 엔드포인트를 부르지 않았다


def test_photo_registers_model_evidence_and_updates_the_stream_photo():
    app = make_app(detector=client())
    before = app.store.get_stream("st_film")
    result = service.upload_media(app, HANBIT, "st_film", "필름.jpg", b"jpeg", HANBIT)
    assert result["detectorUnavailable"] is False and result["mode"] == "photos"
    assert result["model"] == {"name": "resource-ai-detector", "version": "v5"}
    cont_id = ids.model_id("resource-ai-detector", "v5", "st_film-오염비율")
    mat_id = ids.model_id("resource-ai-detector", "v5", "st_film-재질")
    assert result["evidence"] == [mat_id, cont_id]
    contamination = app.registry.get(cont_id, HANBIT)
    assert contamination.kind == "model" and contamination.unit == "%"
    assert contamination.value == 12.5 and contamination.meta["overallRatio"] == 0.125
    assert contamination.meta["reliable"] is False and contamination.access == (HANBIT,)
    material = app.registry.get(mat_id, HANBIT)
    assert material.kind == "model" and material.value is None
    assert material.meta["class"] == "plastic_film" and "필름" in material.excerpt
    for other in (DAON, SAEGIL):  # 올린 조직만 본다
        with pytest.raises(EvidenceForbidden):
            app.registry.get(cont_id, other)
    claim = result["contamination"]["claim"]
    assert claim == {
        "id": "st_film_photoContaminationPct",
        "kind": "number",
        "value": 12.5,
        "unit": "%",
        "evidence": [cont_id],
        "label": "사진 추정 오염 비율",
        "role": "contamination",
    }
    assert result["contamination"]["reliable"] is False and result["contamination"]["note"]
    assert result["material"] == {
        "class": "plastic_film",
        "label": "필름",
        "wasteItem": "포장재",
        "confidence": 0.82,
        "evidence": [mat_id],
    }
    after = app.store.get_stream("st_film")
    assert after["photo"]["contaminationPct"] == 12.5 and after["photo"]["evidence"] == [cont_id]
    assert after["photo"]["materialEstimate"]["class"] == "plastic_film"
    assert after["material"] == before["material"] == "LDPE"  # 수지 종류는 사진으로 바꾸지 않는다
    assert result["stream"] == after
    # 진단은 이 사진 추정값을 쓴다
    view = to_approval(app)
    film = next(s for s in view["streams"] if s["id"] == "st_film")
    assert film["photoContamination"]["value"] == 12.5
    assert film["photoContamination"]["evidence"] == [cont_id]


def test_video_and_missing_condition_model():
    app = make_app(detector=client(VIDEO))
    result = service.upload_media(app, HANBIT, "st_sprue", "라인.mp4", b"mp4", HANBIT)
    assert result["mode"] == "video" and result["tracksTotal"] == 7
    assert result["framesAnalyzed"] == 48
    meta = app.registry.get(result["evidence"][0], HANBIT).meta
    assert meta["mode"] == "video" and meta["tracksTotal"] == 7 and meta["file"] == "라인.mp4"

    # 상태 모델이 돌지 않아 이물 비율이 없으면 값을 만들지 않는다
    bare = make_app(detector=client({**PHOTO, "contamination": None}))
    before = bare.store.get_stream("st_defect")["photo"]
    result = service.upload_media(bare, HANBIT, "st_defect", "불량.png", b"png", HANBIT)
    assert result["contamination"] is None and len(result["evidence"]) == 1
    after = bare.store.get_stream("st_defect")["photo"]
    assert after["contaminationPct"] == before["contaminationPct"]
    assert after["evidence"] == before["evidence"]

    # 아무것도 탐지하지 못하면 재질 추정도 없다
    empty = {**PHOTO, "items": [], "primary_material": None, "contamination": None}
    nothing = make_app(detector=client(empty))
    result = service.upload_media(nothing, HANBIT, "st_film", "빈 사진.jpg", b"jpeg", HANBIT)
    assert result["material"] is None and result["evidence"] == []


def test_upload_media_checks_who_and_what():
    app = make_app(detector=client())
    fails("FORBIDDEN", 403, service.upload_media, app, HANBIT, "st_film", "a.jpg", b"x", DAON)
    fails("WRONG_ROLE", 403, service.upload_media, app, SAEGIL, "st_film", "a.jpg", b"x", SAEGIL)
    fails(
        "STREAM_NOT_FOUND", 404, service.upload_media, app, HANBIT, "st_없음", "a.jpg", b"x", HANBIT
    )
    fails(
        "STREAM_NOT_FOUND",
        404,
        service.upload_media,
        app,
        HANBIT,
        "st_daon_pp",
        "a.jpg",
        b"x",
        HANBIT,
    )
    fails(
        "UNSUPPORTED_MEDIA",
        415,
        service.upload_media,
        app,
        HANBIT,
        "st_film",
        "a.pdf",
        b"x",
        HANBIT,
    )
    down = make_app(detector=client(status=500))
    error = fails(
        "DETECTOR_FAILED", 502, service.upload_media, down, HANBIT, "st_film", "a.jpg", b"x", HANBIT
    )
    assert "500" in error.message
    assert down.store.get_stream("st_film") == make_app().store.get_stream("st_film")
    wrong = make_app(detector=client({"items": []}))
    fails(
        "DETECTOR_FAILED",
        502,
        service.upload_media,
        wrong,
        HANBIT,
        "st_film",
        "a.jpg",
        b"x",
        HANBIT,
    )


# ── HTTP ─────────────────────────────────────────────────────────────
def multipart(fields, filename, content):
    boundary = "----resource-media-boundary"
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
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


def post_media(base, fields, filename, content, org=HANBIT):
    body, content_type = multipart(fields, filename, content)
    headers = {"Content-Type": content_type, "X-Org-Id": org}
    request = urllib.request.Request(base + "/intake/media", data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_media_upload_over_http():
    plain = devserver.build_app(today=TODAY, env={})
    assert plain.detector is None  # 환경 변수가 없으면 미연결
    wired = devserver.build_app(today=TODAY, env={"V3_DETECTOR_ENDPOINT": ENDPOINT})
    assert wired.detector.configured
    calls = []
    wired.detector.transport = fake(PHOTO, calls=calls)
    results = {}
    for name, app in (("plain", plain), ("wired", wired)):
        server = devserver.make_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"
        try:
            results[name] = post_media(
                base, {"orgId": HANBIT, "streamId": "st_film"}, "필름.jpg", b"\xff\xd8\r\n--jpeg"
            )
            if name == "wired":
                results["no_stream"] = post_media(base, {"orgId": HANBIT}, "필름.jpg", b"x")
                results["other"] = post_media(
                    base, {"orgId": HANBIT, "streamId": "st_film"}, "필름.jpg", b"x", org=DAON
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    status, body = results["plain"]
    assert status == 200 and body["detectorUnavailable"] is True
    assert body["message"] == "탐지 모델이 연결되지 않았습니다"
    status, body = results["wired"]
    assert status == 200 and body["detectorUnavailable"] is False
    assert body["contamination"]["claim"]["value"] == 12.5 and body["mode"] == "photos"
    sent = json.loads(calls[0]["body"])["input_data"]
    assert base64.b64decode(sent["content_base64"]) == b"\xff\xd8\r\n--jpeg"  # 파일이 그대로 갔다
    assert results["no_stream"][0] == 404 and results["other"][0] == 403


# ── 형식 검사는 탐지 모델 연결과 관계없이 먼저 한다 ───────────────────
def test_unsupported_format_is_refused_even_without_a_detector():
    calls = []
    for unset in (None, detector.DetectorClient("", "k", fake(PHOTO, calls=calls))):
        app = make_app(detector=unset)
        before = (len(app.registry.list(org_id=None)), app.store.get_stream("st_film"))
        for filename in ("메모.txt", "측정값.csv", "확장자없음", "사진.jpg.exe", ""):
            fails(
                "UNSUPPORTED_MEDIA",
                415,
                service.upload_media,
                app,
                HANBIT,
                "st_film",
                filename,
                b"x",
                HANBIT,
            )
        # 지원하는 형식이면 지금처럼 "미연결"이라고 답한다
        for filename in ("필름.JPG", "필름.png", "라인.mp4"):
            result = service.upload_media(app, HANBIT, "st_film", filename, b"x", HANBIT)
            assert result["detectorUnavailable"] is True and result["evidence"] == []
        assert (len(app.registry.list(org_id=None)), app.store.get_stream("st_film")) == before
    assert calls == []
    # 누가 부르는지와 어느 흐름인지는 형식보다 먼저 본다(남의 흐름이 있는지 형식 오류로 알려 주지 않는다)
    app = make_app()
    fails("FORBIDDEN", 403, service.upload_media, app, HANBIT, "st_film", "a.txt", b"x", DAON)
    fails(
        "STREAM_NOT_FOUND",
        404,
        service.upload_media,
        app,
        HANBIT,
        "st_daon_pp",
        "a.txt",
        b"x",
        HANBIT,
    )


def test_unsupported_format_over_http_without_a_detector():
    app = devserver.build_app(today=TODAY, env={})
    assert app.detector is None
    server = devserver.make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}{devserver.PREFIX}"
    try:
        fields = {"orgId": HANBIT, "streamId": "st_film"}
        refused = post_media(base, fields, "메모.txt", b"hello")
        accepted = post_media(base, fields, "필름.jpg", b"\xff\xd8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert refused[0] == 415 and refused[1]["error"]["code"] == "UNSUPPORTED_MEDIA"
    assert accepted[0] == 200 and accepted[1]["detectorUnavailable"] is True
