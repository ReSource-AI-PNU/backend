"""서비스 함수. 프레임워크와 무관하고, api.py(FastAPI)와 devserver.py(표준 라이브러리)가 같이 부른다.

viewer는 요청한 조직 ID다. 권한이 없으면 ServiceError(code, message, status)를 낸다.
조직 경계: 배출 기업은 자기 진단만 보고, 업체는 자기가 들어간(열린) 스레드와 그 흐름만 본다.
다른 배출 기업의 흐름·근거·스레드는 어떤 함수로도 보이지 않는다.

한 진단에는 진행(run, 답, 승인)이 한 번에 하나만 돈다. 진행하는 동안 실행 자리(store.claim_run)를
차지하고 살아 있다고 적으며(live.Heartbeat), 자리가 살아 있으면 다른 진행은 409 RUN_IN_PROGRESS다.
실시간 진행 기록은 stream_events가 보는 조직에 맞춰 GET /sessions/{id}와 같은 규칙으로 거른다.
"""

from __future__ import annotations

import contextlib
import copy
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from app.v3 import feedback, ids, live
from app.v3 import fleet as fleet_input
from app.v3 import names as naming
from app.v3.a2a.message import STAGES
from app.v3.a2a.policy import QuestionPolicy, evidence_owner
from app.v3.a2a.thread import Thread
from app.v3.account_store import InMemoryAccounts, LoginLimiter
from app.v3.agents.draft import model_label
from app.v3.agents.gateway import LocalVendorGateway
from app.v3.agents.orchestrator import (
    Orchestrator,
    OrchestratorError,
    block_codes,
    memo_result,
    thread_names,
    thread_outcome,
    vendor_model,
)
from app.v3.agents.policy_rule import RulePolicy
from app.v3.agents.pool import ClaimPool, is_private
from app.v3.agents.vendor import VendorAgent
from app.v3.demo.seed import DEMO_ANSWERS, EMITTER, ORGS, seed_demo
from app.v3.evidence import search
from app.v3.evidence.model import KINDS, PUBLIC, Evidence
from app.v3.evidence.registry import EvidenceForbidden, EvidenceNotFound
from app.v3.evidence.seeds import load_seed_dir
from app.v3.evidence.units import UnitError, same_value
from app.v3.evidence.validator import validate_message, validate_text
from app.v3.intake import csv_measure, documents, shields
from app.v3.intake import detector as media
from app.v3.judge import basis
from app.v3.judge.common import quality, without_emitter_only
from app.v3.judge.defaults import register_defaults
from app.v3.store import InMemoryStore, NotFound
from app.v3.untrusted import (
    contaminant_labels,
    counterparty_stream,
    form_key,
    org_label,
    stream_label,
)

logger = logging.getLogger(__name__)

STREAM_FIELDS = (
    "name",
    "process",
    "wasteItem",
    "material",
    "origin",
    "form",
    "lot",
    "bulkDensityKgPerM3",
    "contaminants",
)
MODEL_PROVIDERS = ("foundry", "ollama")
# 화면에 보이는 날짜(기준일)의 시간대 Asia/Seoul. 서머타임이 없어 UTC+9 고정이다(이미지에 tzdata가 없어도 된다)
SEOUL = timezone(timedelta(hours=9), "Asia/Seoul")
VENDOR_EVENT_KINDS = ("message", "handoff", "approval", "thread", "document")
VENDOR_CHECK_KEYS = ("vendor_condition", "min_volume", "loading", "loading_volume")
# 업체가 보는 판단 그래프에 쓰는 검사(judge_route). 업체의 매입 기준·허가 품목과, 대화에서 업체에 열린
# 흐름 자료(오염도, 수분, 형태, 혼입물, 물량의 근거)를 대조한 것만이다. 배출 기업의 현행 처리에 관한
# 검사와 비용·탄소 검사는 넣지 않는다
VENDOR_GRAPH_CHECKS = (
    "permit",
    "criteria",
    "vendor",
    "form",
    "contaminationPct",
    "moisturePct",
    "contamination_basis",
    "moisture_basis",
    "mixture",
    "monthly_kg",
    "monthly_kg_basis",
    "material_basis",
)


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, detail: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


class App:
    """저장소, 근거 레지스트리, 모델, 업체 게이트웨이, 시계를 묶는다.

    llm_factory(role, org_id, model) -> LLM | None. role은 "company" | "vendor", model은 업체
    프로필의 {"provider", "name"} 또는 None이다. None을 돌려주거나 llm_factory가 없으면
    규칙 정책(모델 미연결)으로 돈다.
    """

    def __init__(
        self,
        store: Any,
        registry: Any,
        *,
        llm_factory: Any = None,
        vendor_gateway: Any = None,
        today: str | None = None,
        shields: Any = None,
        documents: Any = None,
        grade_client: Any = None,
        distance_provider: Any = None,
        demo_open: bool = False,
        detector: Any = None,
        auth_mode: str = "header",
        accounts: Any = None,
    ) -> None:
        self.store = store
        # HTTP 층이 요청한 조직을 정하는 방법. header는 시연 전용이다. bff면 실제 계정과 조직이 있고
        # 시연 되돌리기는 시연 조직만 되돌린다(reset_demo)
        self.auth_mode = str(auth_mode)
        # 사용자, 소속, 초대(account_store.py). bff 모드에서만 쓴다. 없으면 메모리
        self.accounts = accounts if accounts is not None else InMemoryAccounts()
        self.login_limiter = LoginLimiter()
        # 체험하기(시연 조직 읽기 전용)로 만든 앱이면 True. 뷰를 만들 때 다시 확인(쓰기)을 하지 않는다
        self.read_only = False
        # True면 시연 되돌리기(POST /demo/reset)를 누구나 부를 수 있다(로컬 시연). 기본은 관리자만
        self.demo_open = bool(demo_open)
        self.registry = registry
        self.llm_factory = llm_factory
        self._today = today
        self.shields = shields
        self.documents = documents
        self.grade_client = grade_client
        self.detector = (
            detector  # 탐지 모델 엔드포인트(intake.detector.DetectorClient). 없으면 미연결
        )
        self.distance_provider = distance_provider
        self.vendor_memo: dict[str, list[dict[str, Any]]] = {}
        self.resumed = False  # 로컬 서버가 저장 파일에서 이전 진단을 이어 간 경우 True
        self._rule = RulePolicy()
        # 업체 게이트웨이를 주지 않았으면 같은 프로세스에서 이 앱으로 답한다(scoped가 다시 묶는다)
        self._local_gateway = vendor_gateway is None
        self.vendor_gateway = vendor_gateway or LocalVendorGateway(
            lambda org, message, context: handle_vendor_message(self, org, message, context)
        )
        # 실행 자리: 이 간격마다 살아 있다고 적고, 이만큼 적지 않은 자리는 멈춘 것으로 본다(초).
        # 시각은 wall_clock()으로 읽는다(테스트에서 바꿔 끼운다)
        self.run_heartbeat = live.HEARTBEAT_SECONDS
        self.run_stale_after = live.STALE_SECONDS
        self.wall_clock = live.wall_clock

    def scoped(self, store: Any, registry: Any) -> App:
        """저장소와 레지스트리만 바꾼 얕은 복사. 같은 프로세스의 업체 게이트웨이도 새 앱을 부르게 한다."""
        other = copy.copy(self)
        other.store, other.registry = store, registry
        if self._local_gateway:
            other.vendor_gateway = LocalVendorGateway(
                lambda org, message, context: handle_vendor_message(other, org, message, context)
            )
        return other

    def with_lock(self, lock: Any) -> App:
        """저장소와 레지스트리를 부를 때마다 lock을 잡는 앱(메모리 저장소에서 뒤에서 도는 실행이 쓴다)."""
        return self.scoped(live.Locked(self.store, lock), live.Locked(self.registry, lock))

    @property
    def today(self) -> str:
        """화면과 근거의 기준일. 사용자가 있는 한국 날짜다(서버 시계가 UTC여도 KST 06시 답은 그날이다).

        저장소의 시각(createdAt 등)은 그대로 UTC다.
        """
        return self._today or datetime.now(SEOUL).date().isoformat()

    def llm_for(self, role: str, org_id: str, model: dict[str, Any] | None) -> Any:
        llm = self.llm_factory(role, org_id, model) if self.llm_factory else None
        return llm if llm is not None else self._rule


# ── 공통 ─────────────────────────────────────────────────────────────


def _org(app: App, org_id: str | None) -> dict[str, Any]:
    if not org_id:
        raise ServiceError("ORG_REQUIRED", "요청한 조직(X-Org-Id)이 없습니다.", 401)
    try:
        return app.store.get_org(org_id)
    except NotFound:
        raise ServiceError("UNKNOWN_ORG", "알 수 없는 조직입니다.", 401) from None


def _admin(app: App, viewer: str | None) -> dict[str, Any]:
    me = _org(app, viewer)
    if me.get("role") != "admin":
        raise ServiceError("FORBIDDEN", "관리자만 할 수 있는 일입니다.", 403)
    return me


def _owner(app: App, org_id: str, viewer: str, role: str | None = None) -> dict[str, Any]:
    """viewer가 org_id 자신(또는 관리자)일 때만 통과한다."""
    me = _org(app, viewer)
    try:
        org = app.store.get_org(org_id)
    except NotFound:
        raise ServiceError("ORG_NOT_FOUND", "그런 조직이 없습니다.", 404) from None
    if viewer != org_id and me.get("role") != "admin":
        raise ServiceError("FORBIDDEN", "다른 조직의 자료는 볼 수 없습니다.", 403)
    if role and org.get("role") != role:
        label = "배출 기업" if role == "emitter" else "처리 업체"
        raise ServiceError("WRONG_ROLE", f"{label}만 할 수 있는 일입니다.", 403)
    return org


def _session(app: App, session_id: str) -> dict[str, Any]:
    try:
        return app.store.get_session(session_id)
    except NotFound:
        raise ServiceError("SESSION_NOT_FOUND", "그런 진단이 없습니다.", 404) from None


def _visible_threads(app: App, session: dict[str, Any], viewer: str) -> list[dict[str, Any]]:
    threads = app.store.list_threads(session_id=session["id"])
    if viewer == session["emitterOrgId"]:
        return threads
    # 업체는 자기와 실제로 열린 대화만 본다. 열리지 않은 스레드는 업체에게 전달된 적이 없다
    return [t for t in threads if t["vendorOrgId"] == viewer and t["status"] != "not_opened"]


def _session_for(app: App, session_id: str, viewer: str) -> tuple[dict[str, Any], bool]:
    """(세션, viewer가 배출 기업 본인인지). 당사자가 아니면 없는 것으로 답한다."""
    me = _org(app, viewer)
    session = _session(app, session_id)
    if viewer == session["emitterOrgId"] or me.get("role") == "admin":
        return session, True
    if not _visible_threads(app, session, viewer):
        raise ServiceError("SESSION_NOT_FOUND", "그런 진단이 없습니다.", 404)
    return session, False


def _wrap(call, *args):
    try:
        return call(*args)
    except OrchestratorError as error:
        raise ServiceError(error.code, error.message, error.status) from error
    except PermissionError as error:
        raise ServiceError("APPROVAL_BLOCKED", str(error), 409) from error


def _human(app: App, org_id: str, key: str, label: str, value: float, unit: str | None) -> str:
    """담당자가 화면에서 입력한 값을 human 근거로 등록한다."""
    evidence = app.registry.put(
        Evidence(
            id=ids.human_id(org_id, key),
            kind="human",
            label=label,
            value=float(value),
            unit=unit,
            excerpt=f"담당자 입력: {label} {value:g}{unit or ''}",
            source="담당자 입력",
            as_of=app.today,
            access=(org_id,),
            meta={"ownerOrgId": org_id, "key": key},
        )
    )
    return evidence.id


def _grounded(
    app: App, org_id: str, key: str, label: str, value: Any, unit: str | None, old: list[str]
) -> str | None:
    """값과 같은 기존 근거가 있으면 그것을, 없으면 새 human 근거를 돌려준다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    for evidence_id in old:
        try:
            found = app.registry.get(evidence_id, org_id)
            if isinstance(found.value, (int, float)) and same_value(
                float(value), unit, float(found.value), found.unit
            ):
                return evidence_id
        except (EvidenceNotFound, EvidenceForbidden, UnitError):
            continue
    return _human(app, org_id, key, label, float(value), unit)


def _redact(item: dict[str, Any], viewer: str) -> dict[str, Any]:
    """계산 근거의 입력 원본(meta.inputs)은 계산한 조직에게만 보인다."""
    meta = item.get("meta")
    inputs = meta.get("inputs") if isinstance(meta, dict) else None
    if isinstance(inputs, dict) and inputs.get("orgId") != viewer:
        item = {**item, "meta": {**meta, "inputs": {"hidden": True}}}
    return item


# ── 시연 데이터, 조직, 흐름, 업체 ────────────────────────────────────


def seed(app: App) -> dict[str, Any]:
    """가정값, 근거 시드(법령·기준값), 시연 데이터를 올린다. 서버가 뜰 때와 reset에서 부른다."""
    return _seed(app, app.registry, wipe=True)


def _seed(app: App, registry: Any, *, wipe: bool) -> dict[str, Any]:
    register_defaults(registry, app.today)
    loaded = load_seed_dir(registry)
    summary = seed_demo(app.store, registry, app.today, wipe=wipe)
    return {**summary, "seedEvidence": loaded}


def reset_demo(app: App, viewer: str | None = None) -> dict[str, Any]:
    """시연 데이터를 처음 상태로 되돌린다. 관리자만 부를 수 있다.

    App(demo_open=True)이면 누구나 부를 수 있다(로컬 시연). 조직이 하나도 없는 빈 저장소의
    첫 시드도 막지 않는다(관리자 조직이 아직 없어 아무도 부를 수 없게 되는 것을 피한다).

    bff 모드(실제 계정과 조직이 있다)에서는 시연 조직의 자료만 지우고 다시 올린 뒤, 시연 진단 한 건을
    승인 대기까지 돌려 둔다(_reset_demo_only). 실제 조직의 조직·흐름·진단·근거는 건드리지 않는다.
    """
    if not app.demo_open and app.store.list_orgs():
        _admin(app, viewer)
    if app.auth_mode == "bff":
        return _reset_demo_only(app)
    if hasattr(app.registry, "clear"):
        app.registry.clear()
    app.vendor_memo.clear()
    return seed(app)


class _KeepAccess:
    """시연 근거를 다시 올릴 때 실제 조직에 이미 열려 있던 것은 그대로 열어 둔다.

    시연 업체의 허가증·매입 기준 근거는 실제 배출 기업과 대화가 열릴 때 그 기업에도 열린다. 다시 올리며
    access를 처음 값으로 돌리면 실제 진단이 인용한 근거가 닫히고 판이 올라가므로, 그 조직들을 더해 둔다
    (내용이 같으면 판도 그대로다).
    """

    def __init__(self, registry: Any, extra: dict[str, tuple[str, ...]]) -> None:
        self._registry = registry
        self._extra = extra

    def put(self, evidence: Evidence) -> Evidence:
        more = self._extra.get(evidence.id)
        if more and PUBLIC not in evidence.access:
            evidence = replace(evidence, access=tuple(dict.fromkeys((*evidence.access, *more))))
        return self._registry.put(evidence)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._registry, name)


def _reset_demo_only(app: App) -> dict[str, Any]:
    """bff 모드의 시연 되돌리기. 시연 조직(시드 조직과 운영 조직)의 자료만 지우고 다시 올린다."""
    seeded = {org["id"] for org in ORGS}
    removed_threads = app.store.purge_orgs(seeded)
    for thread_id in removed_threads:
        app.vendor_memo.pop(thread_id, None)
    doomed: list[str] = []
    extra: dict[str, tuple[str, ...]] = {}
    for evidence in app.registry.list(org_id=None):
        if PUBLIC in evidence.access:
            continue
        outside = tuple(org for org in evidence.access if org not in seeded)
        if not outside:
            if evidence.access or evidence_owner(evidence) in seeded:
                doomed.append(evidence.id)
        elif len(outside) < len(evidence.access):
            extra[evidence.id] = outside
    if doomed:
        app.registry.delete(doomed)
    summary = _seed(app, _KeepAccess(app.registry, extra), wipe=False)
    session_id, status = _demo_diagnosis(app)
    return {**summary, "demoSessionId": session_id, "demoSessionStatus": status}


def _demo_diagnosis(app: App) -> tuple[str, str]:
    """시연 배출 기업의 진단 한 건을 규칙 정책으로 승인 대기까지 돌린다. 체험하기 화면이 비지 않게 한다.

    질문에는 시연 답(seed.DEMO_ANSWERS, 가상 값)으로, 그 밖에는 "모름"으로 답한다. 모델 설정과 상관없이
    규칙 정책으로 돌린다(업체 측도 같은 프로세스에서 규칙 정책으로 답한다).
    """
    trial = copy.copy(app)
    trial.llm_factory = None
    trial.vendor_gateway = LocalVendorGateway(
        lambda org, message, context: handle_vendor_message(trial, org, message, context)
    )
    session_id = create_session(trial, EMITTER, EMITTER)["session"]["id"]
    view = run_session(trial, session_id, EMITTER)
    for _ in range(4):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                value = DEMO_ANSWERS.get((question["streamId"], question["field"]), "unknown")
                answer_question(trial, session_id, question["id"], value, EMITTER)
        view = run_session(trial, session_id, EMITTER)
    return session_id, view["session"]["status"]


def list_orgs(app: App, viewer: str | None = None, scoped: bool = False) -> list[dict[str, Any]]:
    """조직 목록. 조직 레코드의 order(작은 값이 앞)로 늘어놓고, order가 없으면 뒤에 ID 순으로 둔다.

    scoped면(token 인증 모드) 요청한 조직 자신과 대화 상대만 준다: 배출 기업은 자기 진단의 스레드에
    있는 업체, 업체는 자기와 실제로 열린 대화의 배출 기업. 관리자는 모두 본다. scoped가 아니면
    (header 모드, 시연 전용) 조직을 밝히지 않아도 모든 조직을 준다(화면의 조직 고르기용).
    """

    def position(org: dict[str, Any]) -> tuple[bool, float, str]:
        order = org.get("order")
        known = isinstance(order, (int, float)) and not isinstance(order, bool)
        return (not known, float(order) if known else 0.0, str(org.get("id")))

    orgs = sorted(app.store.list_orgs(), key=position)
    if not scoped:
        return orgs
    me = _org(app, viewer)
    if me.get("role") == "admin":
        return orgs
    allowed = {str(viewer)}
    for session in app.store.list_sessions(viewer):
        mine = viewer == session["emitterOrgId"]
        for thread in _visible_threads(app, session, str(viewer)):
            allowed.add(thread["vendorOrgId"] if mine else thread["emitterOrgId"])
    return [org for org in orgs if org["id"] in allowed]


def _vendor_stream(stream: dict[str, Any]) -> dict[str, Any]:
    """업체가 받는 흐름: 배출 기업만의 항목을 빼고, 이름과 혼입물은 보이는 이름으로(untrusted)."""
    return counterparty_stream(without_emitter_only(stream))


def list_streams(app: App, org_id: str, viewer: str) -> list[dict[str, Any]]:
    me = _org(app, viewer)
    if viewer == org_id or me.get("role") == "admin":
        return app.store.list_streams(org_id)
    if me.get("role") == "vendor":
        shared = [s for s in app.store.streams_visible_to(viewer) if s["orgId"] == org_id]
        return [_vendor_stream(s) for s in shared]
    raise ServiceError("FORBIDDEN", "다른 조직의 폐기물은 볼 수 없습니다.", 403)


def update_stream(
    app: App, org_id: str, stream_id: str, patch: dict[str, Any], viewer: str
) -> dict[str, Any]:
    _owner(app, org_id, viewer, "emitter")
    if not isinstance(patch, dict):
        raise ServiceError("INVALID_BODY", "고칠 내용을 객체로 보내 주세요.", 422)
    try:
        stream = app.store.get_stream(stream_id)
        if stream["orgId"] != org_id:
            raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404)
    except NotFound:
        if not patch.get("name"):
            raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404) from None
        stream = {"id": stream_id, "orgId": org_id, "contaminants": []}
    for key in STREAM_FIELDS:
        if key in patch:
            stream[key] = patch[key]
    if "monthlyKg" in patch:
        value = patch["monthlyKg"]
        if value is None:
            stream.update(monthlyKg=None, monthlyKgBasis=None, monthlyKgEvidence=[])
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ServiceError("INVALID_VALUE", "월 발생량은 0 이상의 숫자여야 합니다.", 422)
        else:
            old = list(stream.get("monthlyKgEvidence") or [])
            evidence = _grounded(
                app, org_id, f"{stream_id}-월발생량-입력", "월 발생량", value, "kg/월", old
            )
            stream.update(
                monthlyKg=value,
                monthlyKgEvidence=[evidence],
                monthlyKgBasis=stream.get("monthlyKgBasis")
                if evidence in old
                else str(patch.get("monthlyKgBasis") or "estimated"),
            )
    if "currentTreatment" in patch:
        current = patch["currentTreatment"]
        if current is None:
            stream["currentTreatment"] = None  # 입력이 없으면 혼합 소각 가정값을 쓴다
        elif not isinstance(current, dict):
            raise ServiceError("INVALID_VALUE", "현행 처리는 객체로 보내 주세요.", 422)
        else:
            cost = current.get("unitCostKrwPerKg")
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
                raise ServiceError(
                    "INVALID_VALUE", "현행 처리 단가는 0 이상의 숫자(원/kg)여야 합니다.", 422
                )
            old = list((stream.get("currentTreatment") or {}).get("evidence") or [])
            evidence = _grounded(
                app, org_id, f"{stream_id}-현행처리단가", "현행 처리 단가", cost, "원/kg", old
            )
            stream["currentTreatment"] = {
                "method": str(current.get("method") or "현행"),
                "unitCostKrwPerKg": cost,
                "evidence": [evidence],
            }
    _update_costs(app, org_id, stream_id, stream, patch)
    saved = app.store.put_stream(stream)
    _recheck(app, org_id)
    return saved


def _amount(value: Any, message: str, *, positive: bool = False) -> float:
    bad = isinstance(value, bool) or not isinstance(value, (int, float))
    if bad or value < 0 or (positive and value <= 0):
        raise ServiceError("INVALID_VALUE", message, 422)
    return float(value)


def _update_costs(app: App, org_id: str, stream_id: str, stream: dict, patch: dict) -> None:
    """순비용 계산에 쓰는 배출 기업만의 입력: 전처리 작업 시간·인건비와 공정 재투입 값."""
    if "pretreatHoursPerMonth" in patch or "laborCostKrwPerHour" in patch:
        hours = patch.get("pretreatHoursPerMonth", stream.get("pretreatHoursPerMonth"))
        rate = patch.get("laborCostKrwPerHour", stream.get("laborCostKrwPerHour"))
        old = list(stream.get("pretreatEvidence") or [])
        if hours is None:
            stream.update(pretreatHoursPerMonth=None, laborCostKrwPerHour=None, pretreatEvidence=[])
        else:
            hours = _amount(hours, "전처리 작업 시간은 0 이상의 숫자(시간/월)여야 합니다.")
            evidence = [
                _grounded(
                    app,
                    org_id,
                    f"{stream_id}-전처리시간",
                    "월 전처리 작업 시간",
                    hours,
                    "시간/월",
                    old,
                )
            ]
            if rate is not None:
                rate = _amount(
                    rate, "시간당 인건비는 0보다 큰 숫자(원/시간)여야 합니다.", positive=True
                )
                evidence.append(
                    _grounded(
                        app,
                        org_id,
                        f"{stream_id}-시간당인건비",
                        "시간당 인건비",
                        rate,
                        "원/시간",
                        old,
                    )
                )
            stream.update(
                pretreatHoursPerMonth=hours,
                laborCostKrwPerHour=rate,
                pretreatEvidence=[e for e in evidence if e],
            )
    if "reuse" in patch:
        reuse = patch["reuse"]
        if reuse is None:
            stream["reuse"] = None
            return
        if not isinstance(reuse, dict):
            raise ServiceError("INVALID_VALUE", "재투입 값은 객체로 보내 주세요.", 422)
        old = list((stream.get("reuse") or {}).get("evidence") or [])
        virgin = _amount(
            reuse.get("virginPriceKrwPerKg"),
            "신재 구입 단가는 0보다 큰 숫자(원/kg)여야 합니다.",
            positive=True,
        )
        limit = _amount(
            reuse.get("reusableKgPerMonth"),
            "다시 넣을 수 있는 양은 0보다 큰 숫자(kg/월)여야 합니다.",
            positive=True,
        )
        regrind = reuse.get("regrindCostKrwPerKg")
        regrind = (
            0.0 if regrind is None else _amount(regrind, "분쇄 비용은 0 이상의 숫자여야 합니다.")
        )
        evidence = [
            _grounded(app, org_id, f"{stream_id}-신재단가", "신재 구입 단가", virgin, "원/kg", old),
            _grounded(
                app,
                org_id,
                f"{stream_id}-재투입가능량",
                "다시 넣을 수 있는 양",
                limit,
                "kg/월",
                old,
            ),
            _grounded(app, org_id, f"{stream_id}-분쇄비용", "분쇄 비용", regrind, "원/kg", old),
        ]
        stream["reuse"] = {
            "virginPriceKrwPerKg": virgin,
            "reusableKgPerMonth": limit,
            "regrindCostKrwPerKg": regrind,
            "evidence": [e for e in evidence if e],
        }


def get_vendor(app: App, org_id: str, viewer: str) -> dict[str, Any]:
    me = _org(app, viewer)
    try:
        profile = app.store.get_vendor_profile(org_id)
        org = app.store.get_org(org_id)
    except NotFound:
        raise ServiceError("VENDOR_NOT_FOUND", "그런 업체가 없습니다.", 404) from None
    if viewer == org_id or me.get("role") == "admin":
        return {**profile, "name": org.get("name")}
    if app.auth_mode == "bff" and not org.get("demo"):
        # 실제 업체의 공개 프로필은 대화 상대(그 업체와 스레드가 있는 배출 기업)에게만 보인다
        counterpart = any(
            thread.get("vendorOrgId") == org_id and thread.get("emitterOrgId") == viewer
            for thread in app.store.list_threads(org_id=viewer)
        )
        if not counterpart:
            raise ServiceError("VENDOR_NOT_FOUND", "그런 업체가 없습니다.", 404)
    # 다른 조직에는 허가 품목과 모델 선택만 보인다(매입 기준, 차량, 가드레일은 보이지 않는다)
    return {
        "orgId": org_id,
        "name": org.get("name"),
        "permits": [
            {"wasteItem": p.get("wasteItem"), "materials": p.get("materials")}
            for p in profile.get("permits") or []
        ],
        "model": profile.get("model"),
    }


def update_vendor(app: App, org_id: str, patch: dict[str, Any], viewer: str) -> dict[str, Any]:
    _owner(app, org_id, viewer, "vendor")
    if not isinstance(patch, dict):
        raise ServiceError("INVALID_BODY", "고칠 내용을 객체로 보내 주세요.", 422)
    try:
        profile = app.store.get_vendor_profile(org_id)
    except NotFound:
        profile = {"orgId": org_id, "permits": [], "criteria": [], "fleet": {}, "guardrails": {}}
    if "model" in patch:
        model = patch["model"] or {}
        if model.get("provider") not in MODEL_PROVIDERS or not model.get("name"):
            raise ServiceError(
                "INVALID_VALUE", "모델은 foundry 또는 ollama 가운데 고르고 이름을 적어 주세요.", 422
            )
        profile["model"] = {"provider": model["provider"], "name": str(model["name"])}
    if "guardrails" in patch:
        if not isinstance(patch["guardrails"], dict):
            raise ServiceError("INVALID_VALUE", "가드레일은 객체로 보내 주세요.", 422)
        profile["guardrails"] = {**(profile.get("guardrails") or {}), **patch["guardrails"]}
    if "permits" in patch:
        old = {p.get("wasteItem"): p for p in profile.get("permits") or []}
        permits = []
        for item in patch["permits"] or []:
            waste, materials = item.get("wasteItem"), list(item.get("materials") or [])
            kept = old.get(waste)
            if kept and list(kept.get("materials") or []) == materials:
                permits.append(kept)
                continue
            evidence = app.registry.put(
                Evidence(
                    id=ids.human_id(org_id, f"허가품목-{waste}"),
                    kind="human",
                    label=f"허가 품목(업체 입력): {waste}",
                    excerpt=f"영업대상 폐기물: {waste} ({', '.join(materials)})",
                    source="업체 담당자 입력(허가증 원본 확인 전)",
                    as_of=app.today,
                    access=(org_id,),
                    meta={"ownerOrgId": org_id},
                )
            )
            permits.append({"wasteItem": waste, "materials": materials, "evidence": [evidence.id]})
        profile["permits"] = permits
    if "criteria" in patch:
        old = {c.get("id"): c for c in profile.get("criteria") or []}
        criteria = []
        for item in patch["criteria"] or []:
            cid = str(item.get("id") or f"pc_{ids.digest(item)[:6]}")
            before = list((old.get(cid) or {}).get("evidence") or [])
            evidence = []
            for key, label, unit in (
                ("unitPriceKrwPerKg", "단가", "원/kg"),
                ("minMonthlyKg", "최소 물량", "kg/월"),
                ("maxContaminationPct", "오염도 상한", "%"),
                ("maxMoisturePct", "수분 상한", "%"),
            ):
                found = _grounded(
                    app, org_id, f"{cid}-{label}", f"{cid} {label}", item.get(key), unit, before
                )
                if found:
                    evidence.append(found)
            criteria.append({**item, "id": cid, "evidence": list(dict.fromkeys(evidence))})
        profile["criteria"] = criteria
    if "fleet" in patch:
        # 노선·차량은 app/v3/fleet.py가 검사한다(모르는 항목, 범위 밖 값은 422). null 값은 그 항목을 지운다
        try:
            changes = fleet_input.clean_patch(patch["fleet"])
        except ValueError as error:
            raise ServiceError("INVALID_VALUE", str(error), 422) from None
        fleet = dict(profile.get("fleet") or {})
        for key, value in changes.items():
            if value is None:
                fleet.pop(key, None)
            else:
                fleet[key] = value
        before = list((profile.get("fleet") or {}).get("evidence") or [])
        evidence = []
        for key, label, unit in (
            ("capacityKg", "차량 적재 중량", "kg"),
            ("capacityM3", "차량 적재 부피", "m3"),
            ("costPerKm", "km당 운송비", "원/km"),
        ):
            found = _grounded(app, org_id, f"노선차량-{label}", label, fleet.get(key), unit, before)
            if found:
                evidence.append(found)
        fleet["evidence"] = list(dict.fromkeys(evidence))
        profile["fleet"] = fleet
    saved = app.store.put_vendor_profile(profile)
    _recheck(app, org_id)
    return {**saved, "name": app.store.get_org(org_id).get("name")}


# ── 진단 ─────────────────────────────────────────────────────────────


def create_session(app: App, emitter_org_id: str, viewer: str) -> dict[str, Any]:
    _owner(app, emitter_org_id, viewer, "emitter")
    session = Orchestrator(app).create(emitter_org_id)
    return get_view(app, session["id"], viewer)


def list_sessions(app: App, viewer: str) -> list[dict[str, Any]]:
    """내가 당사자인 진단의 목록(최근 것이 앞). 다른 기기에서 들어와도 이어 볼 수 있게 한다.

    배출 기업은 자기 진단, 업체는 자기에게 실제로 열린 대화가 있는 진단, 운영자는 전부를 본다.
    목록에는 진단 ID, 상태, 배출 기업, 시각만 싣는다. 내용은 GET /sessions/{id}가 조직 경계를 지켜 돌려준다.
    """
    me = _org(app, viewer)
    admin = me.get("role") == "admin"
    out = []
    for session in app.store.list_sessions(None if admin else viewer):
        mine = viewer == session["emitterOrgId"]
        if not (admin or mine or _visible_threads(app, session, viewer)):
            continue
        out.append(
            {
                "id": session["id"],
                "emitterOrgId": session["emitterOrgId"],
                "emitterName": app.store.get_org(session["emitterOrgId"])["name"],
                "status": _shown_status(app, session),
                "createdAt": session.get("createdAt"),
                "updatedAt": session.get("updatedAt") or session.get("createdAt"),
                "as": "admin" if admin else ("emitter" if mine else "vendor"),
            }
        )
    return sorted(out, key=lambda item: (item["updatedAt"] or "", item["id"]), reverse=True)


def run_session(app: App, session_id: str, viewer: str) -> dict[str, Any]:
    """다음 멈춤 지점까지 이 요청 안에서 돌리고 진단 뷰를 준다. 다른 진행이 돌고 있으면 409."""
    _check_runner(app, session_id, viewer)
    with _exclusive(app, session_id, "sync") as run:
        _execute(app, session_id, run["runId"])
    return get_view(app, session_id, viewer)


def start_run(app: App, session_id: str, viewer: str) -> dict[str, Any]:
    """뒤에서 돌릴 실행의 자리를 차지한다(POST /sessions/{id}/run?mode=async). 돌리는 것은 execute_run.

    응답의 after는 이 실행이 남길 첫 진행 기록 바로 앞의 seq다(GET /stream?after=로 이 실행만 본다).
    """
    session = _check_runner(app, session_id, viewer)
    run = _take_run(app, session_id, "async")
    feed = live.Recorder(app.store)
    first = feed.status({**session, "status": "running"})
    feed.note(session_id, "진단을 진행합니다.", org_id=session["emitterOrgId"])
    return {
        "sessionId": session_id,
        "status": "running",
        "runId": run["runId"],
        "after": max(int(first["seq"]) - 1, 0) if first else 0,
    }


def execute_run(app: App, session_id: str, run_id: str) -> None:
    """start_run이 차지한 자리에서 다음 멈춤 지점까지 돌린다(뒤에서 도는 스레드가 부른다).

    오류는 실행 기록(kind "error")과 진행 기록에 남기고 진단을 멈춘 상태로 돌려 둔 뒤 다시 낸다.
    끝나면(오류여도) 자리를 비운다.
    """
    try:
        with live.Heartbeat(app.store, session_id, run_id, app.run_heartbeat, app.wall_clock):
            _execute(app, session_id, run_id)
    finally:
        _release(app, session_id, run_id)


def answer_question(
    app: App, session_id: str, question_id: str, value: Any, viewer: str
) -> dict[str, Any]:
    _, owner = _session_for(app, session_id, viewer)
    if not owner:
        raise ServiceError("FORBIDDEN", "질문에는 배출 기업 담당자만 답할 수 있습니다.", 403)
    with _exclusive(app, session_id, "answer"):
        _wrap(Orchestrator(app).answer, session_id, question_id, value)
    return get_view(app, session_id, viewer)


def approve(app: App, session_id: str, thread_id: str, viewer: str) -> dict[str, Any]:
    session, _ = _session_for(app, session_id, viewer)
    if thread_id not in {t["id"] for t in _visible_threads(app, session, viewer)}:
        raise ServiceError("THREAD_NOT_FOUND", "그런 대화가 없습니다.", 404)
    with _exclusive(app, session_id, "approve"):
        _wrap(Orchestrator(app).approve, session_id, thread_id, viewer)
    return get_view(app, session_id, viewer)


# ── 실행 자리(한 진단에 진행 하나) ───────────────────────────────────


def _check_runner(app: App, session_id: str, viewer: str) -> dict[str, Any]:
    session, owner = _session_for(app, session_id, viewer)
    if not owner:
        raise ServiceError("FORBIDDEN", "진단은 배출 기업 담당자만 진행할 수 있습니다.", 403)
    return session


def _live_run(app: App, session_id: str) -> dict[str, Any] | None:
    """살아 있는 실행 자리. 없거나 멈춘(heartbeat가 오래된) 자리면 None."""
    run = app.store.get_run(session_id)
    return run if live.alive(run, app.run_stale_after, app.wall_clock()) else None


def _shown_status(app: App, session: dict[str, Any]) -> str:
    """화면에 보일 상태. 실행 자리가 살아 있으면 running, 멈춘 실행이 남긴 running은 idle이다."""
    if _live_run(app, session["id"]) is not None:
        return "running"
    return "idle" if session.get("status") == "running" else str(session.get("status"))


def _take_run(app: App, session_id: str, mode: str) -> dict[str, Any]:
    """실행 자리를 차지한다. 다른 진행이 살아 있으면 409 RUN_IN_PROGRESS.

    멈춘 자리(프로세스가 죽어 heartbeat가 끊김)는 차지하고, 그랬다고 실행 기록에 남긴다.
    """
    before = app.store.get_run(session_id)
    now = app.wall_clock()
    run = {
        "runId": app.store.next_id("run"),
        "mode": mode,
        "startedAt": app.store.now(),
        "heartbeat": now,
    }
    if app.store.claim_run(session_id, run, now - app.run_stale_after) is not None:
        raise ServiceError(
            "RUN_IN_PROGRESS", "이 진단은 지금 진행 중입니다. 끝난 뒤에 다시 해 주세요.", 409
        )
    if before is not None and before.get("runId") != run["runId"]:
        app.store.add_event(
            session_id,
            "system",
            "error",
            "앞선 진행이 응답하지 않아(서버 재시작 등) 멈춘 것으로 보고 이어서 진행합니다.",
            detail={"runId": before.get("runId"), "reason": "stale", "takenBy": run["runId"]},
        )
    live.HUB.bump(session_id)
    return run


def _release(app: App, session_id: str, run_id: str) -> None:
    try:
        app.store.release_run(session_id, run_id)
    finally:
        live.HUB.bump(session_id)


@contextlib.contextmanager
def _exclusive(app: App, session_id: str, mode: str) -> Iterator[dict[str, Any]]:
    """이 요청 안에서 끝나는 진행(동기 run, 답, 승인)이 실행 자리를 차지하고 끝나면 비운다."""
    run = _take_run(app, session_id, mode)
    try:
        heartbeat = live.Heartbeat(
            app.store, session_id, run["runId"], app.run_heartbeat, app.wall_clock
        )
        with heartbeat:
            yield run
    finally:
        _release(app, session_id, run["runId"])


def _execute(app: App, session_id: str, run_id: str) -> None:
    orchestrator = Orchestrator(app)
    try:
        _wrap(orchestrator.run, session_id)
    except Exception as error:
        _run_failed(app, orchestrator, session_id, run_id, error)
        raise
    # 아무것도 바꾸지 않고 끝났어도(이미 승인 대기 등) 마지막 상태를 남긴다(같은 값이면 남지 않는다)
    orchestrator.feed.status(app.store.get_session(session_id))


def _run_failed(
    app: App, orchestrator: Orchestrator, session_id: str, run_id: str, error: Exception
) -> None:
    """진행 중 오류: 실행 기록에 남기고, running으로 남은 진단을 멈춘 상태로 돌린다."""
    text = "진행 중 오류가 나서 멈췄습니다. 다시 진행할 수 있습니다."
    try:
        session = app.store.get_session(session_id)
        if session.get("status") == "running":
            open_questions = [
                q for q in app.store.list_questions(session_id) if not q.get("answered")
            ]
            session["status"] = "awaiting_answers" if open_questions else "idle"
            session = app.store.put_session(session)
        code = getattr(error, "code", None) or type(error).__name__
        app.store.add_event(
            session_id, "system", "error", text, detail={"runId": run_id, "error": str(code)}
        )
        orchestrator.feed.note(session_id, text, org_id=session["emitterOrgId"])
        orchestrator.feed.status(session)
    except Exception:  # 원래 오류를 가리지 않는다
        logger.exception("진행 중 오류를 기록하지 못했다: %s", session_id)


def handle_vendor_message(
    app: App, vendor_org_id: str, message: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """업체 측 에이전트가 받은 메시지 하나에 답 메시지(dict) 하나를 돌려준다.

    LocalVendorGateway와 별도 서비스(vendor_service.py)가 같이 부른다. 답의 meta에는
    validation(시도 횟수), model(실제로 쓴 구현), trace(도구 호출과 검증 기록)가 붙는다.
    """
    try:
        profile = app.store.get_vendor_profile(vendor_org_id)
    except NotFound:
        raise ServiceError("VENDOR_NOT_FOUND", "그런 업체가 없습니다.", 404) from None
    agent = VendorAgent(
        store=app.store,
        registry=app.registry,
        today=str((context or {}).get("today") or app.today),
        llm=app.llm_for("vendor", vendor_org_id, profile.get("model")),
        distance_provider=app.distance_provider,
        memo=app.vendor_memo,
    )
    try:
        return agent.handle(vendor_org_id, message, context)
    except (KeyError, ValueError) as error:
        raise ServiceError(
            "INVALID_MESSAGE", f"메시지를 처리할 수 없습니다: {error}", 422
        ) from error


# ── 진단 뷰 (계약 7장) ───────────────────────────────────────────────


def _claim(pool: ClaimPool, claim_id: str | None) -> dict[str, Any] | None:
    entry = pool.get(claim_id) if claim_id else None
    return entry.claim.to_dict() if entry else None


def _visible_ids(app: App, evidence_ids, viewer: str) -> list[str]:
    """보는 조직이 열 수 있는 근거 ID만, 같은 ID는 한 번만(처음 나온 순서대로) 남긴다."""
    out = []
    for evidence_id in dict.fromkeys(evidence_ids or []):
        try:
            app.registry.get(evidence_id, viewer)
        except (EvidenceNotFound, EvidenceForbidden):
            continue
        out.append(evidence_id)
    return out


def _stream_view(
    app: App, stream: dict[str, Any], session: dict[str, Any], viewer: str, owner: bool
) -> dict[str, Any]:
    label = stream_label(stream)
    if not owner:
        # 현행 처리비 답과 '모름' 표시도 업체에는 없다. 이름과 혼입물은 보이는 이름이다(untrusted)
        stream = counterparty_stream(without_emitter_only(stream))
    pool = ClaimPool(app.registry, viewer)
    facts = pool.add_stream(stream, private=owner)
    quality_of = quality(stream)
    grade = ((session["work"]["identify"].get(stream["id"]) or {}).get("grade")) or {}
    view = {
        "id": stream["id"],
        "orgId": stream["orgId"],
        "name": stream.get("name"),
        "label": label,
        "process": stream.get("process"),
        "wasteItem": stream.get("wasteItem"),
        "material": stream.get("material"),
        "origin": stream.get("origin"),
        "form": stream.get("form"),
        "lot": stream.get("lot"),
        "contaminants": (
            [c.get("name") or c.get("label") for c in stream.get("contaminants") or []]
            if owner
            else contaminant_labels(stream)
        ),
        "monthlyKg": _claim(pool, facts.get("monthlyKg")),
        "monthlyKgBasis": stream.get("monthlyKgBasis"),
        "contamination": _claim(pool, facts.get("contaminationPct")),
        "photoContamination": _claim(pool, facts.get("photoContaminationPct")),
        "moisture": _claim(pool, facts.get("moisturePct")),
        # contamination·moisture 값의 출처: measured(측정) | answered(담당자 답) | photo_estimate | null
        "contaminationBasis": _basis_of(
            facts.get("contaminationPct"), quality_of["contaminationBasis"]
        ),
        "moistureBasis": _basis_of(facts.get("moisturePct"), quality_of["moistureBasis"]),
        "unknownFields": list(stream.get("unknownFields") or []),
        "grade": {
            "value": grade.get("value"),
            "basis": grade.get("basis"),
            "label": grade.get("label") or "확인 필요",
            "evidence": _visible_ids(app, grade.get("evidence"), viewer),
        },
        "currentTreatment": None,
    }
    if owner:
        current = stream.get("currentTreatment") or {}
        cost = _claim(pool, facts.get("currentUnitCost"))
        assumed = cost is None
        if assumed:
            found = basis.resolve(app.registry, viewer, f"current.cost.{stream.get('wasteItem')}")
            if found is not None:
                cost = naming.annotate(
                    {
                        "id": f"{stream['id']}_currentUnitCost",
                        "kind": "number",
                        "value": found.value,
                        "unit": found.unit,
                        "evidence": [found.id],
                    },
                    "currentUnitCost",
                )
        view["currentTreatment"] = {
            "method": "혼합 소각" if assumed else current.get("method"),
            "unitCost": cost,
            "assumed": assumed,
        }
        # 순비용에 쓰는 배출 기업만의 입력(자료 화면에서 고친다). 업체 뷰에는 없다
        reuse = stream.get("reuse") or None
        view["costInputs"] = {
            "pretreatHoursPerMonth": stream.get("pretreatHoursPerMonth"),
            "laborCostKrwPerHour": stream.get("laborCostKrwPerHour"),
            "reuse": {
                k: reuse.get(k)
                for k in ("virginPriceKrwPerKg", "reusableKgPerMonth", "regrindCostKrwPerKg")
            }
            if reuse
            else None,
        }
    return view


def _basis_of(claim_id: str | None, basis_name: str | None) -> str | None:
    """화면에 보인 claim의 출처. 사진 추정 오염 비율은 photoContamination에 따로 있다."""
    if not claim_id:
        return None
    return "answered" if "answered" in claim_id else ("measured" if basis_name else None)


# 대화 화면에 보여 줄 작성자 이름. 구현 이름(모델 배포 이름, "규칙 정책(모델 미연결)")은 쓰지 않는다.
# 구현 이름은 messages[].model과 threads[].vendorModel(그대로 둔다)과 관리자 화면(GET /admin/status)에 있다
AUTHOR_LABELS = {
    ("company", "agent"): live.AGENT_LABELS["company"],  # 회사 측 AI
    ("vendor", "agent"): live.AGENT_LABELS["vendor"],  # 업체 측 AI
    ("company", "system"): live.SYSTEM_LABEL,  # 자동 안내
    ("vendor", "system"): live.SYSTEM_LABEL,
}


def _author(side: str, model: dict[str, Any] | None = None) -> dict[str, str]:
    """작성자 표시. side는 company | vendor, kind는 agent(에이전트가 씀) | system(코드가 쓴 안내)."""
    kind = "system" if (model or {}).get("provider") == "code" else "agent"
    return {"side": side, "kind": kind, "label": AUTHOR_LABELS[(side, kind)]}


def _message_view(message: dict[str, Any], emitter_org_id: str | None = None) -> dict[str, Any]:
    meta = message.get("meta") or {}
    terms = meta.get("terms") or {}
    side = "company" if message["from"] == emitter_org_id else "vendor"
    return {
        "id": message["id"],
        "seq": message["seq"],
        "from": message["from"],
        "to": message["to"],
        "type": message["type"],
        "stage": message["stage"],
        "streamIds": message["streamIds"],
        "text": message["text"],
        "claims": message["claims"],
        "validation": message["validation"],
        "createdAt": message["createdAt"],
        "author": _author(side, meta.get("model")),
        "model": meta.get("model"),
        "meta": {
            "asks": meta.get("asks") or [],
            "excluded": meta.get("excluded") or [],
            "reason": meta.get("reason"),
            # 조건의 값은 claims에만 있다. 여기에는 어느 claim이 어느 흐름의 단가인지만 둔다
            "terms": {
                "items": [
                    {"streamId": i.get("streamId"), "claimId": i.get("claimId")}
                    for i in terms.get("items") or []
                ],
                "days": terms.get("days") or [],
                "conditions": terms.get("conditions") or [],
            }
            if terms
            else None,
        },
    }


def _thread_view(app: App, thread: dict[str, Any], session: dict[str, Any], viewer: str) -> dict:
    vendor = thread["vendorOrgId"]
    try:
        profile = app.store.get_vendor_profile(vendor)
    except NotFound:
        profile = {"orgId": vendor}
    return {
        "id": thread["id"],
        "emitterOrgId": thread["emitterOrgId"],
        "vendorOrgId": vendor,
        "vendorName": org_label(app.store.get_org(vendor).get("name"), vendor),
        "vendorModel": vendor_model(app, session, profile),
        "vendorAuthor": _author("vendor"),
        "status": thread["status"],
        "statusReason": thread["statusReason"],
        "reasonEvidence": _visible_ids(app, thread["reasonEvidence"], viewer),
        "streamIds": thread["streamIds"],
        "assignedStreamIds": list(session["work"]["assigned"].get(thread["id"]) or []),
        "turns": thread["turns"],
        "maxTurns": thread["maxTurns"],
        "agreedSeq": thread.get("agreedSeq"),
        # 실행이 멈춘 뒤의 끝 상태와 문장(agreed, confirmed, declined, handoff, not_selected, ended,
        # waiting). 진행 중이거나 열지 않은 대화는 null이다
        **_outcome_view(session, thread),
        "messages": [_message_view(m, thread["emitterOrgId"]) for m in thread["messages"]],
    }


def _outcome_view(session: dict[str, Any], thread: dict[str, Any]) -> dict[str, Any]:
    outcome, text = thread_outcome(session, thread["id"], thread["status"])
    return {"outcome": outcome, "outcomeText": text or None}


def _graph(
    app: App, session: dict[str, Any], threads: list[dict], streams: list[dict], viewer: str
):
    """판단 그래프. 노드는 흐름·기준·업체·근거 문서, 엣지는 충족·조건부·불충족이다."""
    work = session["work"]
    owner = viewer == session["emitterOrgId"]
    stream_ids = {s["id"] for s in streams}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def node(node_id: str, kind: str, label: str) -> None:
        nodes.setdefault(node_id, {"id": node_id, "type": kind, "label": label})

    def edge(source: str, target: str, status: str, label: str, evidence=(), **extra) -> None:
        edges.append(
            {
                "from": source,
                "to": target,
                "status": status,
                "label": label,
                "evidence": _visible_ids(app, evidence, viewer),
                **extra,
            }
        )

    def evidence_node(evidence_id: str) -> bool:
        try:
            found = app.registry.get(evidence_id, viewer)
        except (EvidenceNotFound, EvidenceForbidden):
            return False
        node(evidence_id, "evidence", found.label or evidence_id)
        return True

    org_names = {o["id"]: o.get("name") for o in app.store.list_orgs()}
    criteria: dict[tuple[str, str], dict[str, Any]] = {}
    for profile in app.store.list_vendor_profiles():
        for item in profile.get("criteria") or []:
            criteria[(profile["orgId"], str(item.get("id")))] = item
    for stream in streams:
        node(
            stream["id"], "stream", (stream.get("name") if owner else None) or stream_label(stream)
        )
    vendors = {t["vendorOrgId"] for t in threads}
    for vendor in sorted(vendors):
        node(vendor, "vendor", org_names.get(vendor) or vendor)
    for item in work["permits"]:
        if item["streamId"] not in stream_ids or item["vendorOrgId"] not in vendors:
            continue
        edge(
            item["streamId"],
            item["vendorOrgId"],
            "met" if item["matched"] else "unmet",
            "허가 품목 일치" if item["matched"] else "허가 품목 없음",
            item["evidence"],
            kind="permit",
        )
        for evidence_id in item["evidence"]:
            if evidence_node(evidence_id):
                edge(
                    item["vendorOrgId"],
                    evidence_id,
                    "met",
                    "허가증",
                    [evidence_id],
                    kind="document",
                )
    assigned = {
        (sid, t["vendorOrgId"]) for t in threads for sid in work["assigned"].get(t["id"]) or []
    }
    for thread in threads:
        vendor = thread["vendorOrgId"]
        memo = work["threadTools"].get(thread["id"]) or []
        for sid in thread["streamIds"]:
            if sid not in stream_ids:
                continue
            route = memo_result(memo, "judge_route", streamId=sid)
            if not route:
                continue
            checks = route.get("checks") or []
            if owner:
                pending = route["status"] != "calculated"
            else:
                # 업체는 자기 매입 기준·허가 품목과 흐름을 대조한 검사만 본다. 같은 판단 도구가 같은
                # 입력(업체에 열린 흐름 자료와 업체의 기준표)으로 낸 결과다. 배출 기업의 현행 처리와
                # 비용·탄소에 관한 검사, 그 문장(output.reason)은 쓰지 않는다
                checks = [c for c in checks if c["key"] in VENDOR_GRAPH_CHECKS]
                pending = any(c["status"] == "conditional" for c in checks)
            open_checks = [c for c in checks if c["status"] != "met" and c["key"] != "permit"]
            status = (
                "unmet"
                if any(c["status"] == "unmet" for c in checks)
                else ("conditional" if pending else "met")
            )
            label = open_checks[0]["text"] if open_checks else "매입 기준 충족"
            evidence = [e for c in checks for e in c.get("evidence") or []]
            criterion = route["output"].get("criterionId")
            if criterion:
                # 기준 노드의 이름은 ID가 아니라 재질·등급·형태다(예: "PP · A등급 · 분쇄")
                label_text = naming.criterion_label(
                    criteria.get((vendor, str(criterion))),
                    f"{org_names.get(vendor) or vendor} 매입 기준",
                )
                node(criterion, "criterion", label_text)
                edge(sid, criterion, status, label, evidence, kind="criterion")
                edge(criterion, vendor, "met", "매입 기준표", [], kind="criterion")
            else:
                edge(
                    sid,
                    vendor,
                    status,
                    (route["output"].get("reason") or label)
                    if owner
                    else (label if open_checks else "맞는 매입 기준이 정해지지 않았습니다."),
                    evidence,
                    kind="criterion",
                )
            if (sid, vendor) in assigned:
                edge(
                    sid,
                    vendor,
                    status if status != "unmet" else "conditional",
                    "최적안 배정",
                    [],
                    kind="assignment",
                )
    for item in work["excluded"]:
        if item["streamId"] in stream_ids and item["vendorOrgId"] in vendors:
            edge(
                item["streamId"],
                item["vendorOrgId"],
                "unmet",
                item["reason"],
                item.get("evidence") or [],
                kind="excluded",
            )
    if owner:
        for sid, required in sorted(work["requirements"].items()):
            for found in required["output"].get("items") or []:
                evidence_id = found.get("evidenceId")
                if evidence_id and evidence_node(evidence_id):
                    edge(
                        sid,
                        evidence_id,
                        "conditional",
                        f"'{found['topic']}' 관련 조항(적용 여부는 담당자 확인)",
                        [evidence_id],
                        kind="regulation",
                    )
    return {"nodes": list(nodes.values()), "edges": edges}


def _is_evidence_id(value: Any) -> bool:
    """근거 ID처럼 생긴 문자열인지(종류 접두어 + 콜론). 실제 근거인지는 레지스트리로 확인한다."""
    if not isinstance(value, str) or ":" not in value:
        return False
    head, _, rest = value.partition(":")
    return head in KINDS and bool(rest) and not any(c.isspace() for c in value)


def _from_current(evidence_id: str) -> bool:
    """배출 기업의 현행 처리(입력이 없으면 혼합 소각 가정)에서 나온 근거인지: 가정 단가와 그 계산값."""
    if evidence_id.startswith("assumption:current."):
        return True
    return evidence_id.startswith("calc:") and is_private(evidence_id.rsplit(":", 1)[-1])


def _event_view(
    app: App, event: dict[str, Any], viewer: str, seen: dict[str, bool], owner: bool = True
) -> dict:
    """실행 기록 한 줄에서 보는 조직이 열 수 없는 근거 ID를 뺀다.

    상대 쪽 도구 호출의 detail.evidence 같은 곳에는 상대만 볼 수 있는 계산 근거의 ID가 실린다.
    ID에는 도구 이름과 값의 종류가 드러나므로 빼고, 뺀 개수만 detail.hiddenEvidenceCount에 남긴다.
    업체가 보는 뷰에서는 현행 처리에서 나온 계산 근거의 ID도 뺀다(업체 쪽 도구가 현행 처리를 모른 채
    가정값으로 낸 것이라, 배출 기업의 현행 처리로 읽히지 않게 한다).
    """
    hidden: set[str] = set()

    def can_open(evidence_id: str) -> bool:
        if not owner and _from_current(evidence_id):
            return False
        if evidence_id not in seen:
            try:
                app.registry.get(evidence_id, viewer)
                seen[evidence_id] = True
            except EvidenceForbidden:
                seen[evidence_id] = False
            except EvidenceNotFound:
                # 레지스트리에 없는 것은 근거 ID가 아니다("model:rule" 같은 표시). 그대로 둔다
                seen[evidence_id] = True
        return seen[evidence_id]

    def scrub(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if _is_evidence_id(key) and not can_open(key):
                    hidden.add(key)  # 근거 ID를 키로 쓴 표(evidenceMarks 같은 것)
                    continue
                if _is_evidence_id(value) and not can_open(value):
                    hidden.add(value)
                    out[key] = None
                    continue
                out[key] = scrub(value)
            return out
        if isinstance(node, list):
            kept = []
            for value in node:
                if _is_evidence_id(value) and not can_open(value):
                    hidden.add(value)
                    continue
                kept.append(scrub(value))
            return kept
        return node

    detail = scrub(event.get("detail") or {})
    if hidden:
        detail["hiddenEvidenceCount"] = len(hidden)
    if naming.tool_label(detail.get("tool")):
        # 화면에는 도구 ID(detail.tool) 대신 이 이름을 쓴다
        detail["toolLabel"] = naming.tool_label(detail.get("tool"))
    return {**event, "detail": detail}


def _result_view(session: dict[str, Any], viewer: str, owner: bool) -> dict[str, Any] | None:
    result = session["work"].get("result")
    if result is None or owner:
        return result
    mine = []
    for item in result["assignments"]:
        if item["vendorOrgId"] != viewer:
            continue
        mine.append(
            {
                **item,
                # 배출 기업의 현행 대비 손익과 다른 업체의 제안은 업체에게 보이지 않는다
                "monthlyAfter": None,
                "monthlyNow": None,
                "difference": None,
                "revenue": None,
                "transport": None,
                "labor": None,
                "reuse": None,
                "carbon": None,
                "carbonValue": None,
                # 업체가 내건 조건과 최소 물량·적재 검사만 남긴다(나머지는 배출 기업의 판단이다)
                "checks": [c for c in item["checks"] if c["key"] in VENDOR_CHECK_KEYS],
                "alternatives": [],
            }
        )
    return {
        "status": result["status"] if mine else "needs_information",
        "assignments": mine,
        "totals": dict.fromkeys(("monthlyNow", "monthlyAfter", "difference", "carbon")),
        "assumptions": [],
        "sensitivity": [],
        "referencePrices": result.get("referencePrices") or [],
    }


def _events_view(
    app: App, events: list[dict[str, Any]], viewer: str, owner: bool = True
) -> list[dict[str, Any]]:
    seen: dict[str, bool] = {}
    return [_event_view(app, event, viewer, seen, owner) for event in events]


def _recheck(app: App, org_id: str) -> None:
    """org_id의 자료가 바뀐 뒤: 그 조직이 들어간, 승인을 기다리는 진단의 합의한 대화를 다시 확인한다.

    바뀐 근거나 흐름을 인용한 대화는 승인을 시도하기 전에 모두 막힌다(approvals[].blocked).
    """
    for session in app.store.list_sessions(org_id):
        if session.get("status") == "awaiting_approval":
            Orchestrator(app).recheck(session["id"])


def get_view(app: App, session_id: str, viewer: str) -> dict[str, Any]:
    session, owner = _session_for(app, session_id, viewer)
    if (
        session.get("status") == "awaiting_approval"
        and not app.read_only
        and _live_run(app, session_id) is None
    ):
        # 저장소를 직접 고친 변경까지 잡으려고 뷰를 만들 때도 다시 확인한다(바뀐 것이 없으면 아무것도 쓰지 않는다).
        # 체험하기(읽기 전용)에서는 쓰지 않고, 진행이 돌고 있으면 그 진행에 맡긴다(같은 대화를 함께 고치지 않게)
        Orchestrator(app).recheck(session_id)
    me = app.store.get_org(viewer)
    work = session["work"]
    threads = _visible_threads(app, session, viewer)
    thread_ids = {t["id"] for t in threads}
    approvable = {t["id"] for t in threads if t["status"] in ("agreed", "confirmed")}
    if owner:
        streams = app.store.list_streams(session["emitterOrgId"])
    else:
        wanted = {sid for t in threads for sid in t["streamIds"]}
        streams = [
            _vendor_stream(s) for s in app.store.streams_visible_to(viewer) if s["id"] in wanted
        ]
    events = app.store.list_events(session_id)
    if not owner:
        events = [
            e
            for e in events
            if e.get("threadId") in thread_ids
            and (e["kind"] in VENDOR_EVENT_KINDS or (e.get("detail") or {}).get("orgId") == viewer)
        ]
    return {
        "session": {
            "id": session["id"],
            "emitterOrgId": session["emitterOrgId"],
            "status": _shown_status(app, session),
            "phase": session.get("phase"),
            "version": session.get("version"),
            "today": session.get("today"),
        },
        "viewer": {"orgId": viewer, "role": me.get("role")},
        "stages": _stages_view(work["stages"]),
        "streams": [_stream_view(app, s, session, viewer, owner) for s in streams],
        "threads": [_thread_view(app, t, session, viewer) for t in threads],
        "graph": _graph(app, session, threads, streams, viewer),
        "result": _result_view(session, viewer, owner),
        "questions": [
            _question_view(q) for q in (app.store.list_questions(session_id) if owner else [])
        ],
        "questionBatch": _question_batch(app.store.list_questions(session_id)) if owner else None,
        "unresolved": list(work.get("unresolved") or []) if owner else [],
        "approvals": [
            {
                **a,
                "blockedCodes": block_codes(a.get("blockedReason")),
                "streamIds": list(work["assigned"].get(a["threadId"]) or []),
                # 막으면서 지운 승인(emitter·vendor). 비어 있으면 지운 승인이 없다
                "clearedApprovals": list(a.get("clearedApprovals") or []),
            }
            for a in app.store.list_approvals(session_id)
            # 승인할 수 있는 대화만 낸다. 담당자에게 넘어간 대화의 승인 칸은 보이지 않는다
            if a["threadId"] in approvable
        ],
        "documents": [
            d for d in app.store.list_documents(session_id) if d.get("threadId") in thread_ids
        ],
        "events": _events_view(app, events, viewer, owner),
        "feedback": _feedback_view(app, session, viewer, owner),
    }


QUESTION_KEYS = (
    "id",
    "batch",
    "streamId",
    "field",
    "text",
    "answered",
    "unknown",
    "answerType",
    "unit",
    "choices",
)


def _question_view(question: dict[str, Any]) -> dict[str, Any]:
    return {k: question.get(k) for k in QUESTION_KEYS}


def _question_batch(questions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """답을 기다리는 질문 묶음의 요약과 화면 머리 문장. 기다리는 질문이 없으면 None.

    질문은 묶음당 최대 3개, 진단당 최대 2묶음이다(QuestionPolicy). 첫 묶음 뒤에 질문이 더 올 수 있으므로
    첫 묶음 머리에 "…만"이라고 쓰지 않고, 둘째 묶음은 마지막이라고 밝힌다. count는 이 묶음의 질문 수다.
    """
    waiting = [q for q in questions if not q.get("answered")]
    if not waiting:
        return None
    policy = QuestionPolicy()
    batch = min(int(q.get("batch") or 1) for q in waiting)
    count = sum(1 for q in questions if int(q.get("batch") or 1) == batch)
    last = batch >= policy.max_batches
    intro = (
        f"{count}가지만 더 여쭙겠습니다. 이번이 마지막 질문입니다."
        if last
        else f"찾기 전에 {count}가지를 여쭙겠습니다."
    )
    return {
        "batch": batch,
        "maxBatches": policy.max_batches,
        "maxPerBatch": policy.max_per_batch,
        "count": count,
        "open": sum(1 for q in waiting if int(q.get("batch") or 1) == batch),
        "last": last,
        "intro": intro,
        "note": None if last else "답을 보고 한 번 더 여쭐 수 있습니다.",
    }


def _stages_view(stages: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "stage": number,
            "key": key,
            "label": label,
            "status": (stages or {}).get(str(number), "waiting"),
        }
        for number, (key, label) in sorted(STAGES.items())
    ]


# ── 실시간 진행 (계약 8장 "실시간 진행") ─────────────────────────────


def stream_events(app: App, session_id: str, viewer: str, after: int = 0) -> dict[str, Any]:
    """seq가 after보다 큰 진행 기록을 보는 조직에 맞춰 준다. GET /sessions/{id}/stream이 되풀이해 부른다.

    {"status", "running", "lastSeq", "events": [{"event", "id", "data"}]}. running은 실행 자리가
    살아 있는지다. lastSeq는 읽은 기록의 마지막 seq다(보는 조직에게 걸러진 기록도 센다).
    거르는 규칙은 get_view와 같다: 배출 기업(과 관리자)은 모든 기록, 업체는 자기와 열린 대화의 기록 가운데
    두 당사자가 함께 보는 것(보낸 메시지, 넘김, 대화 열기·합의·승인·확정·문서)과 자기가 한 일만 본다.
    질문 묶음은 배출 기업만 본다. 거부 코드(codes)는 관리자 조직에만 나간다.
    """
    _, owner = _session_for(app, session_id, viewer)
    admin = app.store.get_org(viewer).get("role") == "admin"
    # 자리 → 진단 → 기록 순으로 읽는다. 자리가 비었으면 그 앞에 쓴 기록은 모두 아래에서 읽힌다
    running = _live_run(app, session_id) is not None
    session = _session(app, session_id)
    status = "running" if running else _shown_status(app, session)
    entries = app.store.list_activity(session_id, after)
    if not entries:  # 새 기록이 없다(스트림이 기다리는 동안 되풀이하는 읽기는 여기서 끝난다)
        return {"status": status, "running": running, "lastSeq": int(after), "events": []}
    threads = {t["id"]: t for t in app.store.list_threads(session_id=session_id)}
    visible = {t["id"] for t in _visible_threads(app, session, viewer)}
    questions = {q["id"]: q for q in app.store.list_questions(session_id)} if owner else {}
    names: dict[str, str | None] = {}

    def name_of(org_id: str | None) -> str | None:
        if not org_id:
            return None
        if org_id not in names:
            try:
                names[org_id] = app.store.get_org(org_id).get("name")
            except NotFound:
                names[org_id] = None
        return names[org_id]

    events = []
    for entry in entries:
        kind = entry.get("event") or "activity"
        data: dict[str, Any] | None
        if kind == "status":
            data = {
                "seq": entry["seq"],
                "at": entry.get("at"),
                "status": entry.get("status"),
                "phase": entry.get("phase"),
                "stages": _stages_view(entry.get("stages") or {}),
            }
        elif kind == "question":
            data = (
                {
                    "seq": entry["seq"],
                    "at": entry.get("at"),
                    "stage": entry.get("stage"),
                    "questions": [
                        _question_view(questions[q])
                        for q in entry.get("questionIds") or []
                        if q in questions
                    ],
                }
                if owner
                else None
            )
        else:
            data = _activity_view(entry, viewer, owner, admin, visible, threads, name_of)
        if data is not None:
            events.append({"event": kind, "id": entry["seq"], "data": data})
    return {
        "status": status,
        "running": running,
        "lastSeq": int(entries[-1]["seq"]) if entries else int(after),
        "events": events,
    }


def _activity_view(
    entry: dict[str, Any],
    viewer: str,
    owner: bool,
    admin: bool,
    visible: set[str],
    threads: dict[str, dict[str, Any]],
    name_of,
) -> dict[str, Any] | None:
    thread_id = entry.get("threadId")
    if not owner and (
        thread_id not in visible or not (entry.get("shared") or entry.get("orgId") == viewer)
    ):
        return None
    thread = threads.get(thread_id) if thread_id else None
    vendor = (thread or {}).get("vendorOrgId") or entry.get("vendorOrgId")
    side = entry.get("side")
    author = (
        _author(side, {"provider": "code"} if entry.get("authorKind") == "system" else None)
        if side in ("company", "vendor")
        else None
    )
    data: dict[str, Any] = {
        "seq": entry["seq"],
        "at": entry.get("at"),
        "stage": entry.get("stage"),
        "actor": entry.get("actor"),
        "kind": entry.get("kind"),
        "threadId": thread_id,
        "vendorOrgId": vendor,
        "vendorName": name_of(vendor),
        "author": author,
        "text": entry.get("text") or "",
    }
    if entry.get("attempt") is not None:
        data["attempt"] = entry["attempt"]
    kind = entry.get("kind")
    if kind in ("sent", "handoff"):
        message = None
        if thread is not None and thread_id in visible:
            found = next(
                (m for m in thread["messages"] if m.get("seq") == entry.get("messageSeq")), None
            )
            message = _message_view(found, thread["emitterOrgId"]) if found else None
        data["message"] = message
        data["messageType"] = entry.get("messageType")
        if kind == "sent":
            data["evidenceCount"] = int(entry.get("evidenceCount") or 0)
    elif kind == "rejected":
        data["reasons"] = list(entry.get("reasons") or [live.DEFAULT_REASON])
        if admin:
            data["codes"] = list(entry.get("codes") or [])
    elif kind == "status" and entry.get("threadOutcome"):
        # 실행이 멈출 때 남긴 대화의 끝 상태. 화면은 그 대화의 상태 표시를 이것으로 바꾼다
        data["threadStatus"] = entry.get("threadStatus")
        data["threadOutcome"] = entry.get("threadOutcome")
    elif kind == "tool":
        name = entry.get("tool")
        data["tool"] = {
            "name": name,
            "label": live.TOOL_LABELS.get(name or "", "판단 도구"),
            "status": entry.get("toolStatus"),
        }
    return data


def _feedback_view(app: App, session: dict[str, Any], viewer: str, owner: bool) -> dict[str, Any]:
    """이 진단에 쌓인 교정과 실제 결과. 교정은 배출 기업만, 실제 결과는 그 스레드의 당사자만 본다."""
    recorded = feedback.outcomes(app, session["id"])
    if not owner:
        recorded = [r for r in recorded if r["vendorOrgId"] == viewer]
    return {
        "corrections": feedback.corrections(app, session["id"]) if owner else [],
        "outcomes": recorded,
    }


# ── 재학습 데이터 수집 (7단계) ───────────────────────────────────────


def _feedback_call(call, *args):
    try:
        return call(*args)
    except feedback.FeedbackError as error:
        raise ServiceError(error.code, error.message, error.status) from error


def record_correction(
    app: App, session_id: str, body: dict[str, Any], viewer: str
) -> dict[str, Any]:
    """배출 기업 담당자가 고친 값(박스·재질·출처·등급 등)을 human 근거로 남긴다.

    탐지 모델 재학습 데이터 후보로 쌓인다. 흐름의 값 자체는 바꾸지 않는다.
    """
    _org(app, viewer)
    session = _session(app, session_id)
    if viewer != session["emitterOrgId"]:
        if not _visible_threads(app, session, viewer):
            raise ServiceError("SESSION_NOT_FOUND", "그런 진단이 없습니다.", 404)
        raise ServiceError("FORBIDDEN", "값은 배출 기업 담당자만 고칠 수 있습니다.", 403)
    if not isinstance(body, dict):
        raise ServiceError("INVALID_BODY", "고친 내용을 객체로 보내 주세요.", 422)
    try:
        stream = app.store.get_stream(str(body.get("streamId") or ""), viewer)
    except NotFound:
        raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404) from None
    if stream.get("orgId") != viewer:
        raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404)
    record = _feedback_call(feedback.record_correction, app, session, stream, body)
    return {"correction": record, "evidence": [record["id"]]}


def record_outcome(app: App, session_id: str, body: dict[str, Any], viewer: str) -> dict[str, Any]:
    """업체가 확정 뒤에 기록하는 실제 등급·단가·중량. 그 스레드의 업체만, confirmed일 때만 받는다."""
    session, _ = _session_for(app, session_id, viewer)
    if not isinstance(body, dict):
        raise ServiceError("INVALID_BODY", "실제 결과를 객체로 보내 주세요.", 422)
    thread = next(
        (
            t
            for t in _visible_threads(app, session, viewer)
            if t["id"] == str(body.get("threadId") or "")
        ),
        None,
    )
    if thread is None:
        raise ServiceError("THREAD_NOT_FOUND", "그런 대화가 없습니다.", 404)
    if thread["vendorOrgId"] != viewer:
        raise ServiceError("FORBIDDEN", "실제 결과는 그 대화의 업체만 기록할 수 있습니다.", 403)
    if thread["status"] != "confirmed":
        raise ServiceError(
            "THREAD_NOT_CONFIRMED",
            "양쪽 승인으로 확정된 대화에만 실제 결과를 기록할 수 있습니다.",
            409,
        )
    stream_id = str(body.get("streamId") or "")
    if stream_id not in (session["work"]["assigned"].get(thread["id"]) or []):
        raise ServiceError("STREAM_NOT_IN_THREAD", "이 대화에서 확정된 폐기물이 아닙니다.", 422)
    try:
        stream = app.store.get_stream(stream_id)
    except NotFound:
        raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404) from None
    record = _feedback_call(feedback.record_outcome, app, session, thread, stream, body)
    return {"outcome": record, "evidence": list(record["evidence"].values())}


def export_retraining(app: App, viewer: str) -> dict[str, Any]:
    """재학습 작업이 가져갈 행. 여러 조직의 기록이 섞여 있어 관리자만 받는다."""
    _admin(app, viewer)
    return {
        "gradeLabels": feedback.export_grade_labels(app),
        "detectionCorrections": feedback.export_detection_corrections(app),
        "gate": feedback.gate_status(app),
    }


def register_model_run(app: App, run: dict[str, Any], viewer: str) -> dict[str, Any]:
    """Azure ML 재학습 작업이 낸 결과(compare_and_gate.py의 출력)를 모델 버전 표에 올린다."""
    _admin(app, viewer)
    if not isinstance(run, dict):
        raise ServiceError("INVALID_BODY", "실행 결과를 객체로 보내 주세요.", 422)
    return {"run": _feedback_call(feedback.register_model_run, app, run, viewer)}


# ── 근거 ─────────────────────────────────────────────────────────────


def get_evidence(app: App, evidence_id: str, viewer: str) -> dict[str, Any]:
    _org(app, viewer)
    try:
        item = search.get_evidence(app.registry, evidence_id, viewer)
        chain = search.evidence_chain(app.registry, evidence_id, viewer)
    except (EvidenceNotFound, EvidenceForbidden):
        # 볼 수 없는 근거는 있는지 없는지도 알려 주지 않는다
        raise ServiceError(
            "EVIDENCE_NOT_FOUND", "그런 근거가 없거나 볼 수 없습니다.", 404
        ) from None
    return {"evidence": _redact(item, viewer), "chain": [_redact(c, viewer) for c in chain]}


def search_evidence(app: App, query: str, kinds: list[str] | None, viewer: str) -> list[dict]:
    _org(app, viewer)
    wanted = [k for k in kinds or [] if k]
    if any(k not in KINDS for k in wanted):
        raise ServiceError(
            "INVALID_KIND", f"근거 종류는 {', '.join(KINDS)} 가운데 하나입니다.", 422
        )
    found = search.search_evidence(
        app.registry, query or "", kinds=wanted or None, org_id=viewer, limit=20
    )
    return [{**item, "evidence": _redact(item["evidence"], viewer)} for item in found]


# ── 자료 올리기 ──────────────────────────────────────────────────────


def csv_template(app: App) -> bytes:
    return csv_measure.template_csv()


def upload_csv(app: App, org_id: str, filename: str, data: bytes, viewer: str) -> dict[str, Any]:
    _owner(app, org_id, viewer, "emitter")
    parsed = csv_measure.parse_csv(data)
    by_name = {s.get("name"): s for s in app.store.list_streams(org_id)}
    matched = [r for r in parsed["rows"] if r["streamName"] in by_name]
    unmatched = sorted({r["streamName"] for r in parsed["rows"] if r["streamName"] not in by_name})
    registered = csv_measure.register_measures(
        app.registry,
        org_id,
        lambda row: by_name[row["streamName"]]["id"] if row["streamName"] in by_name else None,
        matched,
        filename,
    )
    updated = []
    for name in sorted({r["streamName"] for r in matched}):
        row = max((r for r in matched if r["streamName"] == name), key=lambda r: r["measuredOn"])
        stream = by_name[name]
        sid = stream["id"]
        stream["measure"] = {
            "contaminationPct": row["contaminationPct"],
            "moisturePct": row["moisturePct"],
            "measuredOn": row["measuredOn"],
            "evidence": [
                ids.measure_id(org_id, sid, "오염도", row["measuredOn"]),
                ids.measure_id(org_id, sid, "수분", row["measuredOn"]),
            ],
        }
        if row.get("monthlyKg") is not None:
            stream.update(
                monthlyKg=row["monthlyKg"],
                monthlyKgBasis="measured",
                monthlyKgEvidence=[ids.measure_id(org_id, sid, "월발생량", row["measuredOn"])],
            )
        for key in ("origin", "form", "lot"):
            if row.get(key) and not stream.get(key):
                stream[key] = row[key]
        app.store.put_stream(stream)
        updated.append(sid)
    if registered:
        _recheck(app, org_id)
    return {
        "filename": filename,
        "encoding": parsed["encoding"],
        "accepted": len(matched),
        "errors": parsed["errors"],
        "unmatched": unmatched,
        "evidence": registered,
        "streamIds": updated,
    }


def upload_document(
    app: App, org_id: str, kind: str, filename: str, data: bytes, viewer: str
) -> dict[str, Any]:
    """문서를 읽어 값을 뽑는다. 뽑은 값은 사람이 확인하기 전에는 근거가 아니다.

    extraction은 document_intelligence(PDF·사진·Office 문서를 Document Intelligence로 읽음) |
    text(Document Intelligence가 연결돼 있지만 UTF-8 글 파일이라 그대로 읽음) |
    text_only(Document Intelligence가 연결되지 않아 글 파일만 읽음)다.
    문서 글은 Prompt Shields로 검사한다. 지시문 주입이 탐지되면 문서를 쓰지 않고, 올린 조직의
    진행 중인 대화를 handoff로 넘긴다. Prompt Shields가 설정되지 않았으면 검사하지 않았다는
    표시(shieldUnavailable)를 그대로 돌려준다.
    """
    _owner(app, org_id, viewer)
    if kind not in documents.EXTRACTORS:
        raise ServiceError(
            "INVALID_KIND", f"문서 종류는 {', '.join(documents.EXTRACTORS)} 가운데 하나입니다.", 422
        )
    client = app.documents
    text = documents.plain_text(data)
    if client is not None and client.configured and text is not None:
        # Document Intelligence는 글 파일(.txt 등)을 받지 않는다(400). 연결돼 있어도 그대로 읽는다
        layout, extraction = {"content": text}, "text"
    elif client is not None and client.configured:
        try:
            layout = client.analyze(data)
        except documents.DocumentError as error:
            raise ServiceError("DOCUMENT_FAILED", str(error), 502) from error
        extraction = "document_intelligence"
    else:
        try:
            layout = {"content": data.decode("utf-8")}
        except UnicodeDecodeError:
            raise ServiceError(
                "DOCUMENT_INTELLIGENCE_UNAVAILABLE",
                "Document Intelligence가 설정되지 않아 이 형식의 문서를 읽을 수 없습니다.",
                503,
            ) from None
        extraction = "text_only"
    try:
        shield = shields.check_document(documents.layout_text(layout), app.shields)
    except shields.ShieldError as error:
        raise ServiceError("SHIELD_FAILED", str(error), 502) from error
    base = {
        "kind": kind,
        "filename": filename,
        "extraction": extraction,
        "shield": shield,
        "shieldUnavailable": shield["shieldUnavailable"],
    }
    if shield["attackDetected"]:
        closed = Orchestrator(app).handoff_for_injection(org_id, filename)
        return {**base, "blocked": True, "handoff": True, "threadIds": closed, "fields": []}
    extracted = documents.EXTRACTORS[kind](layout)
    fields = extracted.pop("fields", [])
    return {
        **base,
        "blocked": False,
        "handoff": False,
        "threadIds": [],
        "fields": fields,
        "extracted": extracted,
        "needsReview": any(f.get("needsReview") for f in fields),
        "note": "뽑은 값은 담당자가 확인하기 전에는 근거로 쓰지 않습니다.",
    }


def upload_media(
    app: App, org_id: str, stream_id: str, filename: str, data: bytes, viewer: str
) -> dict[str, Any]:
    """사진·영상을 탐지 모델에 보내 재질 추정과 이물 비율을 model 근거로 등록한다.

    탐지 모델(Azure ML 엔드포인트)이 연결되지 않았으면 파일을 쓰지 않고 그렇다고 답한다.
    탐지 결과를 지어내지 않는다. 사진으로는 수지 종류를 가르지 못하므로 흐름의 재질은 바꾸지 않는다.
    """
    _owner(app, org_id, viewer, "emitter")
    try:
        stream = app.store.get_stream(str(stream_id or ""))
    except NotFound:
        raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404) from None
    if stream.get("orgId") != org_id:
        raise ServiceError("STREAM_NOT_FOUND", "그런 폐기물이 없습니다.", 404)
    # 형식 검사가 먼저다. 탐지 모델이 연결되지 않았어도 지원하지 않는 형식은 415로 답한다
    if media.media_type(filename) is None:
        raise ServiceError(
            "UNSUPPORTED_MEDIA",
            "사진("
            + ", ".join(media.IMAGE_TYPES)
            + ")이나 영상("
            + ", ".join(media.VIDEO_TYPES)
            + ") 파일만 올릴 수 있습니다.",
            415,
        )
    client = app.detector
    if client is None or not client.configured:
        return {
            "detectorUnavailable": True,
            "message": "탐지 모델이 연결되지 않았습니다",
            "streamId": stream["id"],
            "evidence": [],
        }
    try:
        result = client.detect(data, filename)
    except media.DetectorError as error:
        raise ServiceError("DETECTOR_FAILED", str(error), 502) from error
    found = media.register_detection(app.registry, org_id, stream, result, filename, app.today)
    photo = dict(stream.get("photo") or {})
    contamination = found["contamination"]
    if contamination is not None:
        photo.update(
            contaminationPct=contamination["contaminationPct"],
            evidence=list(contamination["evidence"]),
            reliable=contamination["reliable"],
        )
    if found["material"] is not None:
        photo["materialEstimate"] = found["material"]
    photo.update(file=filename, mode=result["mode"])
    stream["photo"] = photo
    saved = app.store.put_stream(stream)
    _recheck(app, org_id)
    claim = None
    if contamination is not None:
        claim = naming.annotate(
            {
                "id": f"{stream['id']}_photoContaminationPct",
                "kind": "number",
                "value": contamination["contaminationPct"],
                "unit": "%",
                "evidence": list(contamination["evidence"]),
            }
        )
    return {
        "detectorUnavailable": False,
        "streamId": stream["id"],
        "filename": filename,
        "mode": result["mode"],
        "framesAnalyzed": result["framesAnalyzed"],
        "tracksTotal": result["tracksTotal"],
        "model": {"name": media.MODEL_NAME, "version": result["modelVersion"] or None},
        "material": found["material"],
        "contamination": None
        if contamination is None
        else {"claim": claim, "reliable": contamination["reliable"], "note": contamination["note"]},
        "detections": result["detections"],
        "evidence": found["evidence"],
        "stream": saved,
        "note": "사진 추정값입니다. 재질(수지 종류)과 이물 비율은 담당자가 확인해 주세요.",
    }


def confirm_document(
    app: App, org_id: str, document_name: str, fields: list[dict[str, Any]], viewer: str
) -> dict[str, Any]:
    """담당자가 확인한 추출값(confirmed=True)만 doc 근거로 등록한다.

    업체가 확인한 허가증·매입 기준표 값은 업체 프로필의 허가 품목과 매입 기준에도 들어간다(근거는 방금
    등록한 doc 근거). 같은 문서를 다시 확인해도 품목과 기준이 겹쳐 생기지 않는다. 허가 품목이나 매입
    기준이 생기면 그때부터 업체 탐색에 나온다(orchestrator.discoverable_profiles).
    """
    org = _owner(app, org_id, viewer)
    registered = documents.confirm_field_map(
        app.registry, org_id, document_name, fields or [], viewer, today=app.today
    )
    profile = None
    if registered and org.get("role") == "vendor":
        profile = _profile_from_document(
            app, org_id, documents.profile_from_fields(fields or [], registered)
        )
    if registered:
        _recheck(app, org_id)
    return {"evidence": list(registered.values()), "profile": profile}


def _same(a: Any, b: Any) -> bool:
    return (a or None) == (b or None)


def _doc_criterion_id(material: str, grade: Any, form: Any) -> str:
    letters = "".join(c for c in material.lower() if c.isascii() and c.isalnum()) or "x"
    return f"pc_doc_{letters}_{ids.digest([material, grade or None, form or None])[:6]}"


def _profile_from_document(app: App, org_id: str, found: dict[str, Any]) -> dict[str, Any]:
    """문서에서 확인한 허가 품목·매입 기준을 업체 프로필에 더한다. 있는 것은 고치고 지우지는 않는다.

    - 허가 품목: 폐기물 종류가 같으면 재질을 합치고 근거를 붙인다(재질이 비어 있으면 모든 재질).
    - 매입 기준: 재질·등급·형태가 같으면 문서 값으로 고치고 근거를 붙인다. 없으면 새로 만든다.
    바뀐 것이 없으면 저장하지 않는다. 화면에 돌려줄 요약을 준다.
    """
    try:
        profile = copy.deepcopy(app.store.get_vendor_profile(org_id))
    except NotFound:
        profile = {"orgId": org_id, "permits": [], "criteria": [], "fleet": {}, "guardrails": {}}
    permits = list(profile.get("permits") or [])
    criteria = list(profile.get("criteria") or [])
    added = {"permits": 0, "criteria": 0}
    updated = {"permits": 0, "criteria": 0}
    for item in found["permits"]:
        same = next((p for p in permits if p.get("wasteItem") == item["wasteItem"]), None)
        if same is None:
            permits.append(
                {
                    "wasteItem": item["wasteItem"],
                    "materials": list(item["materials"]),
                    "evidence": list(dict.fromkeys(item["evidence"])),
                }
            )
            added["permits"] += 1
            continue
        before = (list(same.get("materials") or []), list(same.get("evidence") or []))
        materials = before[0]
        # 재질이 비어 있는 허가 품목은 그 폐기물의 모든 재질이다. 한쪽이라도 비어 있으면 비운다
        same["materials"] = (
            [] if not materials or not item["materials"]
            else list(dict.fromkeys([*materials, *item["materials"]]))
        )  # fmt: skip
        same["evidence"] = list(dict.fromkeys([*before[1], *item["evidence"]]))
        if (same["materials"], same["evidence"]) != before:
            updated["permits"] += 1
    for item in found["criteria"]:
        same = next(
            (
                c
                for c in criteria
                if c.get("material") == item["material"]
                and _same(c.get("grade"), item["grade"])
                and _same(form_key(c.get("form")), form_key(item["form"]))
            ),
            None,
        )
        values = {k: item[k] for k in documents.CRITERION_KEYS if item.get(k) is not None}
        if same is None:
            if values.get("unitPriceKrwPerKg") is None:
                found["skipped"].append(
                    f"{naming.criterion_label(item)}: 단가를 확인하지 않아 매입 기준을 만들지 않았습니다"
                )
                continue
            criteria.append(
                {
                    "id": _doc_criterion_id(item["material"], item["grade"], item["form"]),
                    "material": item["material"],
                    "grade": item["grade"],
                    "form": item["form"],
                    **values,
                    "evidence": list(dict.fromkeys(item["evidence"])),
                }
            )
            added["criteria"] += 1
            continue
        before = copy.deepcopy(same)
        same.update(values)
        same["evidence"] = list(dict.fromkeys([*item["evidence"], *(same.get("evidence") or [])]))
        if same != before:
            updated["criteria"] += 1
    if any(added.values()) or any(updated.values()):
        profile.update(permits=permits, criteria=criteria)
        app.store.put_vendor_profile(profile)
    return {
        "permitsAdded": added["permits"],
        "permitsUpdated": updated["permits"],
        "criteriaAdded": added["criteria"],
        "criteriaUpdated": updated["criteria"],
        "permits": permits,
        "criteria": criteria,
        "skipped": list(found["skipped"]),
    }


# ── 관리자 ───────────────────────────────────────────────────────────


def admin_status(app: App, viewer: str) -> dict[str, Any]:
    """색인·기준값 상태와 재학습 표. 여러 조직의 기록 건수가 섞여 있어 관리자만 본다."""
    _admin(app, viewer)
    public = app.registry.list(org_id=viewer)
    kinds = {kind: sum(1 for e in public if e.kind == kind) for kind in KINDS}
    refs = [e for e in public if e.kind == "ref"]
    known_keys = basis.keys()
    return {
        "today": app.today,
        # 요청한 조직을 정하는 방법. header는 X-Org-Id를 그대로 믿는 시연 전용 모드다
        "auth": {"mode": app.auth_mode, "demoOnly": app.auth_mode == "header"},
        "evidence": {"visible": len(public), "byKind": kinds},
        "laws": {
            "count": kinds["law"],
            "latestAsOf": max((e.as_of for e in public if e.kind == "law"), default=None),
        },
        "references": [{"id": e.id, "label": e.label, "asOf": e.as_of} for e in refs],
        "missingBasis": [
            *[k for k in known_keys if basis.resolve(app.registry, viewer, k) is None],
            *_missing_basis(),
        ],
        "models": {
            "company": model_label(app.llm_for("company", viewer, None)),
            "llmFactory": app.llm_factory is not None,
        },
        "promptShields": {"configured": bool(app.shields and app.shields.configured)},
        "documentIntelligence": {"configured": bool(app.documents and app.documents.configured)},
        "gradeEndpoint": {"configured": bool(app.grade_client and app.grade_client.configured)},
        "detector": {"configured": bool(app.detector and app.detector.configured)},
        "distance": {
            "provider": app.distance_provider is not None,
            # 어떤 제공자인지와 마지막 실패 이유(키는 싣지 않는다). 없으면 직선 거리 × 우회 계수(가정값)다
            "kind": type(app.distance_provider).__name__ if app.distance_provider else None,
            "lastError": getattr(app.distance_provider, "last_error", None),
        },
        "vendorGateway": type(app.vendor_gateway).__name__,
        "store": type(app.store).__name__,
        # 재학습 표: 실제 등급 라벨 수, 교정 건수, 배포 게이트, 기준값 보정 후보, 모델 버전
        "retrain": feedback.retrain_table(app, None),
    }


def _missing_basis() -> list[str]:
    from app.v3.judge import defaults

    return list(defaults.load().get("missing") or [])


def run_eval(app: App, viewer: str) -> dict[str, Any]:
    """사례 시험. 평가 사례 세트(app/v3/eval/cases, 모두 가상 수치)와 시연 시나리오를 돌린다.

    사례마다 따로 만든 저장소에서 돌리므로 운영 데이터는 바뀌지 않는다. 지금 연결된 모델 설정과
    공개 근거(법령, 기준값, 가정값)를 그대로 쓰므로, 규칙 정책으로 돌았으면 결과에 그렇게 적힌다.
    맨 위의 scenario~resultStatus는 시연 시나리오 한 건의 결과이고, cases가 사례 세트의 결과다.
    관리자만 부를 수 있다.
    """
    _admin(app, viewer)
    from app.v3.eval.loader import load_cases
    from app.v3.eval.runner import run_cases

    outcome = run_cases(app, load_cases())
    keep = ("id", "title", "review", "open", "openNote", "tags", "error", "verdictMatch")
    return {
        **_demo_eval(app),
        "cases": {
            "model": outcome["model"],
            "summary": outcome["summary"],
            "results": [
                {
                    **{key: result[key] for key in keep},
                    "mismatches": result["mismatches"],
                    "forbiddenViolations": result["forbiddenViolations"],
                    "validator": result["validator"],
                }
                for result in outcome["results"]
            ],
        },
    }


def _demo_eval(app: App) -> dict[str, Any]:
    """시연 시나리오(가상 데이터)를 따로 만든 저장소에서 처음부터 끝까지 돌려 본다.

    담당자 질문에는 모두 "모름"으로 답한다. 검증기 통과율은 나간 메시지와 문서를 다시 검증해 센다.
    """
    from app.v3.evidence.registry import InMemoryRegistry

    trial = App(
        InMemoryStore(),
        InMemoryRegistry(),
        llm_factory=app.llm_factory,
        today=app.today,
        distance_provider=app.distance_provider,
    )
    seed(trial)
    session_id = create_session(trial, EMITTER, EMITTER)["session"]["id"]
    view = run_session(trial, session_id, EMITTER)
    for _ in range(4):
        if view["session"]["status"] != "awaiting_answers":
            break
        for question in view["questions"]:
            if not question["answered"]:
                answer_question(trial, session_id, question["id"], "unknown", EMITTER)
        view = run_session(trial, session_id, EMITTER)
    for item in list(view["approvals"]):
        approve(trial, session_id, item["threadId"], EMITTER)
        vendor = next(t["vendorOrgId"] for t in view["threads"] if t["id"] == item["threadId"])
        approve(trial, session_id, item["threadId"], vendor)
    view = get_view(trial, session_id, EMITTER)
    checked = passed = rewritten = 0
    for raw in trial.store.list_threads(session_id=session_id):
        thread = Thread.from_dict(raw)
        names = thread_names(trial.store, thread)
        for message in thread.messages:
            checked += 1
            passed += validate_message(message, trial.registry, today=trial.today, names=names).ok
            rewritten += (message.meta.get("validation") or {}).get("attempts", 1) > 1
    for document in view["documents"]:
        checked += 1
        passed += validate_text(
            document["text"], document["claims"], trial.registry, org_id=EMITTER, today=trial.today
        ).ok
    return {
        "scenario": "시연 시나리오(모두 가상 데이터, 담당자 질문에는 모두 '모름'으로 답함)",
        "model": model_label(trial.llm_for("company", EMITTER, None)),
        "sessionStatus": view["session"]["status"],
        "checked": checked,
        "passed": passed,
        "validatorPassRate": (passed / checked) if checked else None,
        "rewritten": rewritten,
        "threads": [
            {
                "vendorOrgId": t["vendorOrgId"],
                "vendorModel": t["vendorModel"],
                "status": t["status"],
                "types": [m["type"] for m in t["messages"]],
            }
            for t in view["threads"]
        ],
        "resultStatus": (view["result"] or {}).get("status"),
    }
