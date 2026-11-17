"""시연 데이터. 회사 이름, 좌표, 수치, 문서는 모두 가상이다(실제 업체나 견적이 아니다).

조직 레코드의 order는 조직 목록(GET /orgs)의 순서다. 시연 주인공 (주)한빛정밀이 맨 앞이다.

seed_demo(store, registry, today)는 저장소를 비우고 같은 상태로 다시 채운다.
허가증, 매입 기준표, 계약서 값은 doc 근거로, 측정값은 measure 근거로, 사진 추정은 model 근거로
등록한다. 근거는 소유 조직만 볼 수 있고, 대화가 열릴 때 share_evidence로 상대에게 연다.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import PUBLIC, Evidence

SYNTHETIC_NOTE = "시연용 가상 데이터 (실제 업체·견적·측정이 아님)"
MEASURED_ON = "2026-11-10"
DOC_DATE = "2026-11-01"

EMITTER = "org_hanbit"
OTHER_EMITTER = "org_daon"
VENDORS = ("org_saegil", "org_namhae", "org_miso")
ADMIN = "org_admin"

ORGS: list[dict[str, Any]] = [
    {
        "id": "org_hanbit",
        "order": 1,
        "name": "(주)한빛정밀",
        "role": "emitter",
        "region": "부산 강서구 녹산산단",
        "location": [128.8260, 35.0871],
    },
    {
        "id": "org_daon",
        "order": 2,
        "name": "(주)다온산업",
        "role": "emitter",
        "region": "부산 강서구 녹산산단",
        "location": [128.8412, 35.0935],
    },
    {
        "id": "org_saegil",
        "order": 3,
        "name": "(주)새길자원",
        "role": "vendor",
        "region": "부산 강서구 미음산단",
        "location": [128.8721, 35.1493],
    },
    {
        "id": "org_namhae",
        "order": 4,
        "name": "(주)남해열분해",
        "role": "vendor",
        "region": "경남 김해시 주촌면",
        "location": [128.8189, 35.2296],
    },
    {
        "id": "org_miso",
        "order": 5,
        "name": "(주)미소환경",
        "role": "vendor",
        "region": "부산 사상구 학장동",
        "location": [128.9795, 35.1462],
    },
    # 운영 조직. 관리자 화면(/admin/*)과 시연 되돌리기를 부른다. 흐름도 업체 프로필도 없다
    {
        "id": "org_admin",
        "order": 99,
        "name": "Re:Source AI 운영",
        "role": "admin",
        "region": None,
        "location": None,
    },
]


def share_evidence(registry: Any, evidence_id: str, org_id: str) -> Evidence:
    """근거를 org_id도 볼 수 있게 연다. 이미 볼 수 있으면 그대로 둔다."""
    evidence = registry.get(evidence_id, None)
    if PUBLIC in evidence.access or org_id in evidence.access:
        return evidence
    return registry.put(replace(evidence, access=(*evidence.access, org_id)))


def vendor_evidence_ids(profile: dict[str, Any]) -> list[str]:
    """업체 프로필에 걸린 근거 ID(허가증, 매입 기준표, 노선·차량)."""
    found: list[str] = []
    for permit in profile.get("permits") or []:
        found += permit.get("evidence") or []
    for criterion in profile.get("criteria") or []:
        found += criterion.get("evidence") or []
    found += (profile.get("fleet") or {}).get("evidence") or []
    found += (profile.get("transport") or {}).get("evidence") or []
    return list(dict.fromkeys(found))


def share_vendor_terms(registry: Any, profile: dict[str, Any], org_id: str) -> list[str]:
    """대화가 열릴 때 업체의 허가증, 매입 기준, 운반 조건 근거를 배출 기업에 연다. fleet 근거는 열지 않는다."""
    shared: list[str] = []
    for section in ("permits", "criteria"):
        for item in profile.get(section) or []:
            for evidence_id in item.get("evidence") or []:
                share_evidence(registry, evidence_id, org_id)
                shared.append(evidence_id)
    for evidence_id in (profile.get("transport") or {}).get("evidence") or []:
        share_evidence(registry, evidence_id, org_id)
        shared.append(evidence_id)
    return list(dict.fromkeys(shared))


def stream_evidence_ids(stream: dict[str, Any]) -> list[str]:
    found: list[str] = list(stream.get("monthlyKgEvidence") or [])
    for key in ("measure", "photo", "currentTreatment"):
        found += (stream.get(key) or {}).get("evidence") or []
    return list(dict.fromkeys(found))


def share_stream(registry: Any, stream: dict[str, Any], org_id: str) -> list[str]:
    """대화가 열릴 때 그 흐름의 측정값·사진 추정·물량 근거를 업체에 연다. 현행 계약 단가는 열지 않는다."""
    shared = list(stream.get("monthlyKgEvidence") or [])
    for key in ("measure", "photo"):
        shared += (stream.get(key) or {}).get("evidence") or []
    for evidence_id in shared:
        share_evidence(registry, evidence_id, org_id)
    return list(dict.fromkeys(shared))


class _Seeder:
    def __init__(self, registry: Any, today: str) -> None:
        self.registry = registry
        self.today = today
        self.evidence_ids: list[str] = []

    def put(
        self,
        evidence_id: str,
        owner: str,
        label: str,
        excerpt: str,
        source: str,
        as_of: str,
        value: float | None = None,
        unit: str | None = None,
    ) -> str:
        self.registry.put(
            Evidence(
                id=evidence_id,
                kind=evidence_id.split(":", 1)[0],
                label=label,
                excerpt=excerpt,
                source=f"{source} · {SYNTHETIC_NOTE}",
                as_of=as_of,
                value=value,
                unit=unit,
                access=(owner,),
                meta={"ownerOrgId": owner, "synthetic": True},
            )
        )
        self.evidence_ids.append(evidence_id)
        return evidence_id

    # ── 배출 기업 흐름 ────────────────────────────────────────────────
    def monthly(self, org: str, stream_id: str, kg: float, measured: bool) -> list[str]:
        if measured:
            eid = ids.measure_id(org, stream_id, "월발생량", MEASURED_ON)
            return [
                self.put(
                    eid,
                    org,
                    "월 발생량",
                    f"계근 기록 월 합계 {kg:g}kg",
                    "계근대 기록",
                    MEASURED_ON,
                    kg,
                    "kg/월",
                )
            ]
        eid = ids.human_id(org, f"{stream_id}-월발생량-추정")
        return [
            self.put(
                eid,
                org,
                "월 발생량(담당자 추정)",
                f"담당자 추정 월 {kg:g}kg",
                "담당자 입력",
                self.today,
                kg,
                "kg/월",
            )
        ]

    def measure(self, org: str, stream_id: str, contamination: float, moisture: float) -> dict:
        c = self.put(
            ids.measure_id(org, stream_id, "오염도", MEASURED_ON),
            org,
            "오염도",
            f"오염도 {contamination:g}%",
            "측정값 CSV",
            MEASURED_ON,
            contamination,
            "%",
        )
        m = self.put(
            ids.measure_id(org, stream_id, "수분", MEASURED_ON),
            org,
            "수분",
            f"수분 {moisture:g}%",
            "측정값 CSV",
            MEASURED_ON,
            moisture,
            "%",
        )
        return {
            "contaminationPct": contamination,
            "moisturePct": moisture,
            "measuredOn": MEASURED_ON,
            "evidence": [c, m],
        }

    def photo(self, org: str, stream_id: str, contamination: float, found: str) -> dict:
        eid = ids.model_id("resource-ai-detector", "v1", f"{stream_id}-오염비율")
        self.put(
            eid,
            org,
            "사진 추정 오염 비율(시연용 고정값)",
            f"사진에서 추정한 이물 면적 비율 {contamination:g}% ({found}). "
            "시연용으로 넣어 둔 값이고 탐지 모델을 돌린 결과가 아니다",
            "시연용 고정값(탐지 모델 미실행)",
            self.today,
            contamination,
            "%",
        )
        return {"contaminationPct": contamination, "evidence": [eid]}

    def current(self, org: str, stream_id: str, cost: float) -> dict:
        eid = ids.doc_id(org, "위탁계약서", f"{stream_id}-처리단가")
        self.put(
            eid,
            org,
            "현행 위탁 처리 단가",
            f"혼합 폐합성수지 소각 위탁 처리 단가 kg당 {cost:g}원",
            "폐기물 위탁 처리 계약서",
            DOC_DATE,
            cost,
            "원/kg",
        )
        return {"method": "혼합 소각", "unitCostKrwPerKg": cost, "evidence": [eid]}

    # ── 업체 프로필 ───────────────────────────────────────────────────
    def permit(self, org: str, waste_item: str, materials: list[str]) -> dict:
        eid = ids.doc_id(org, "허가증", waste_item)
        self.put(
            eid,
            org,
            f"허가 품목: {waste_item}",
            f"영업대상 폐기물: {waste_item} ({', '.join(materials)})",
            "폐기물 종합재활용업 허가증",
            DOC_DATE,
        )
        return {"wasteItem": waste_item, "materials": materials, "evidence": [eid]}

    def criterion(self, org: str, cid: str, **spec: Any) -> dict:
        doc = "매입기준표"
        evidence = [
            self.put(
                ids.doc_id(org, doc, f"{cid}-단가"),
                org,
                "단가",
                f"{spec['material']} {spec['grade']}등급 단가 kg당 {spec['unitPriceKrwPerKg']:g}원",
                "매입 기준표",
                DOC_DATE,
                spec["unitPriceKrwPerKg"],
                "원/kg",
            ),
            self.put(
                ids.doc_id(org, doc, f"{cid}-최소물량"),
                org,
                "최소 물량",
                f"1회 계약 최소 물량 월 {spec['minMonthlyKg']:g}kg",
                "매입 기준표",
                DOC_DATE,
                spec["minMonthlyKg"],
                "kg/월",
            ),
            self.put(
                ids.doc_id(org, doc, f"{cid}-오염도상한"),
                org,
                "오염도 상한",
                f"이물 혼입 {spec['maxContaminationPct']:g}% 이하",
                "매입 기준표",
                DOC_DATE,
                spec["maxContaminationPct"],
                "%",
            ),
            self.put(
                ids.doc_id(org, doc, f"{cid}-수분상한"),
                org,
                "수분 상한",
                f"수분 {spec['maxMoisturePct']:g}% 이하",
                "매입 기준표",
                DOC_DATE,
                spec["maxMoisturePct"],
                "%",
            ),
        ]
        return {"id": cid, **spec, "evidence": evidence}

    def transport(self, org: str, paid_by: str, fee: float | None = None) -> dict:
        """운반 조건. 업체 부담이면 단가에 포함(수거 포함), 배출 기업 부담이면 1회 운반비를 따로 받는다."""
        if paid_by == "vendor":
            eid = self.put(
                ids.doc_id(org, "매입기준표", "운반조건"),
                org,
                "운반 조건",
                "수거 차량 운반비는 매입 단가에 포함한다(수거 포함 단가)",
                "매입 기준표",
                DOC_DATE,
            )
            return {"paidBy": "vendor", "evidence": [eid]}
        eid = self.put(
            ids.doc_id(org, "반입조건표", "운반비"),
            org,
            "1회 운반비",
            f"운반비는 처리비와 따로 받는다: 1회 {fee:g}원",
            "반입 조건표",
            DOC_DATE,
            fee,
            "원/회",
        )
        return {"paidBy": "emitter", "feePerPickupKrw": fee, "evidence": [eid]}

    def fleet(self, org: str, depot: list[float], kg: float, m3: float, per_km: float) -> dict:
        doc = "노선차량"
        evidence = [
            self.put(
                ids.doc_id(org, doc, "적재중량"),
                org,
                "차량 적재 중량",
                f"수거 차량 적재 중량 {kg:g}kg",
                "노선·차량 등록",
                DOC_DATE,
                kg,
                "kg",
            ),
            self.put(
                ids.doc_id(org, doc, "적재부피"),
                org,
                "차량 적재 부피",
                f"수거 차량 적재 부피 {m3:g}m3",
                "노선·차량 등록",
                DOC_DATE,
                m3,
                "m3",
            ),
            self.put(
                ids.doc_id(org, doc, "km당비용"),
                org,
                "km당 운송비",
                f"km당 운송비 {per_km:g}원",
                "노선·차량 등록",
                DOC_DATE,
                per_km,
                "원/km",
            ),
        ]
        return {
            "depot": depot,
            "capacityKg": kg,
            "capacityM3": m3,
            "costPerKm": per_km,
            "evidence": evidence,
        }


def _streams(s: _Seeder) -> list[dict[str, Any]]:
    h, d = EMITTER, OTHER_EMITTER
    return [
        {
            "id": "st_sprue",
            "orgId": h,
            "name": "스프루·런너",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "가공 전",
            "lot": "2026-11-A",
            "monthlyKg": 600,
            "monthlyKgBasis": "measured",
            "monthlyKgEvidence": s.monthly(h, "st_sprue", 600, True),
            "bulkDensityKgPerM3": 250,
            "contaminants": [],
            "measure": s.measure(h, "st_sprue", 1.0, 0.3),
            "photo": None,
            "currentTreatment": s.current(h, "st_sprue", 250),
        },
        {
            "id": "st_defect",
            "orgId": h,
            "name": "불량 사출품",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "가공 전",
            "lot": "2026-11-B",
            "monthlyKg": 400,
            "monthlyKgBasis": "measured",
            "monthlyKgEvidence": s.monthly(h, "st_defect", 400, True),
            "bulkDensityKgPerM3": 300,
            "contaminants": [
                {"name": "종이 라벨", "ratio": 0.04},
                {"name": "금속 인서트", "ratio": 0.02},
            ],
            "measure": None,
            "photo": s.photo(h, "st_defect", 6.0, "종이 라벨, 금속 인서트"),
            "currentTreatment": s.current(h, "st_defect", 250),
        },
        {
            "id": "st_purge",
            "orgId": h,
            "name": "퍼지 덩어리",
            "process": "사출(색상 교체)",
            "wasteItem": "폐플라스틱",
            "material": "PP+ABS",
            "origin": "PIR",
            "form": "가공 전",
            "lot": None,
            "monthlyKg": 250,
            "monthlyKgBasis": "estimated",
            "monthlyKgEvidence": s.monthly(h, "st_purge", 250, False),
            "bulkDensityKgPerM3": 500,
            "contaminants": [
                {"name": "ABS 혼입", "ratio": 0.35},
                {"name": "탄화물", "ratio": 0.05},
            ],
            "measure": None,
            "photo": s.photo(h, "st_purge", 40.0, "ABS 혼입, 탄화물"),
            "currentTreatment": None,
        },
        {
            "id": "st_film",
            "orgId": h,
            "name": "포장 필름",
            "process": "입고·포장",
            "wasteItem": "포장재",
            "material": "LDPE",
            "origin": "PCR",
            "form": "필름",
            "lot": None,
            "monthlyKg": 300,
            "monthlyKgBasis": "measured",
            "monthlyKgEvidence": s.monthly(h, "st_film", 300, True),
            "bulkDensityKgPerM3": 60,
            "contaminants": [
                {"name": "종이 태그", "ratio": 0.02},
                {"name": "테이프", "ratio": 0.01},
            ],
            "measure": None,  # 수분 미확인: 질문으로 받는다
            "photo": s.photo(h, "st_film", 3.0, "종이 태그, 테이프"),
            "currentTreatment": None,
        },
        {
            "id": "st_daon_pp",
            "orgId": d,
            "name": "PP 분쇄품",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "분쇄",
            "lot": None,
            "monthlyKg": 900,
            "monthlyKgBasis": "measured",
            "monthlyKgEvidence": s.monthly(d, "st_daon_pp", 900, True),
            "bulkDensityKgPerM3": 400,
            "contaminants": [],
            "measure": s.measure(d, "st_daon_pp", 1.5, 0.4),
            "photo": None,
            "currentTreatment": s.current(d, "st_daon_pp", 230),
        },
    ]


def _vendors(s: _Seeder) -> list[dict[str, Any]]:
    sg, nh, ms = VENDORS
    pp = {"material": "PP", "form": None}
    return [
        {
            "orgId": sg,
            "permits": [
                s.permit(sg, "폐플라스틱", ["PP", "PE"]),
                s.permit(sg, "포장재", ["LDPE", "LLDPE"]),
            ],
            "criteria": [
                s.criterion(
                    sg,
                    "pc_saegil_pp_a",
                    **pp,
                    grade="A",
                    maxContaminationPct=2,
                    maxMoisturePct=1,
                    unitPriceKrwPerKg=320,
                    minMonthlyKg=500,
                ),
                s.criterion(
                    sg,
                    "pc_saegil_pp_b",
                    **pp,
                    grade="B",
                    maxContaminationPct=8,
                    maxMoisturePct=2,
                    unitPriceKrwPerKg=180,
                    minMonthlyKg=500,
                ),
                s.criterion(
                    sg,
                    "pc_saegil_film_b",
                    material="LDPE",
                    form="필름",
                    grade="B",
                    maxContaminationPct=5,
                    maxMoisturePct=3,
                    unitPriceKrwPerKg=150,
                    minMonthlyKg=250,
                ),
            ],
            "fleet": s.fleet(sg, [128.8721, 35.1493], 5000, 20, 1200),
            "transport": s.transport(sg, "vendor"),
            "process": "재활용",
            "guardrails": {
                "floorPriceKrwPerKg": 120,
                "ceilingPriceKrwPerKg": 340,
                "days": ["화", "목"],
                "minPickupsPerMonth": 2,
                "autoDecline": ["PVC", "유해 물질"],
            },
            "model": {"provider": "foundry", "name": "gpt-5-mini"},
            "synthetic": True,
        },
        {
            "orgId": nh,
            "permits": [s.permit(nh, "폐플라스틱", ["PP", "PE", "PS", "ABS", "PP+ABS"])],
            "criteria": [
                s.criterion(
                    nh,
                    "pc_namhae_mixed",
                    material="PP+ABS",
                    form=None,
                    grade="등외",
                    maxContaminationPct=50,
                    maxMoisturePct=5,
                    unitPriceKrwPerKg=-80,
                    minMonthlyKg=200,
                ),
                s.criterion(
                    nh,
                    "pc_namhae_pp_c",
                    **pp,
                    grade="C",
                    maxContaminationPct=20,
                    maxMoisturePct=5,
                    unitPriceKrwPerKg=-60,
                    minMonthlyKg=200,
                ),
            ],
            "fleet": s.fleet(nh, [128.8189, 35.2296], 8000, 30, 1500),
            # 위탁 처리라 운반비를 따로 받는다(1회 운반비 × 월 수거 횟수가 순비용에 들어간다)
            "transport": s.transport(nh, "emitter", 30000),
            "process": "열분해",
            "guardrails": {
                "floorPriceKrwPerKg": -150,
                "ceilingPriceKrwPerKg": -50,
                "days": ["월", "수", "금"],
                "minPickupsPerMonth": 1,
                "autoDecline": ["PVC", "금속 인서트"],
            },
            "model": {"provider": "ollama", "name": "qwen2.5:7b"},
            "synthetic": True,
        },
        {
            "orgId": ms,
            # 필름(포장재·LDPE) 허가 품목이 없다: 필름 흐름으로는 대화를 열지 않아야 한다
            "permits": [s.permit(ms, "폐플라스틱", ["PP", "ABS"])],
            "criteria": [
                s.criterion(
                    ms,
                    "pc_miso_pp_b",
                    **pp,
                    grade="B",
                    maxContaminationPct=8,
                    maxMoisturePct=2,
                    unitPriceKrwPerKg=200,
                    minMonthlyKg=300,
                ),
            ],
            "fleet": s.fleet(ms, [128.9795, 35.1462], 3500, 14, 1100),
            "transport": s.transport(ms, "vendor"),
            "process": "재활용",
            "guardrails": {
                "floorPriceKrwPerKg": 100,
                "ceilingPriceKrwPerKg": 230,
                "days": ["월", "화", "수", "목", "금"],
                "minPickupsPerMonth": 1,
                "autoDecline": ["PVC"],
            },
            "model": {"provider": "foundry", "name": "gpt-5-mini"},
            "synthetic": True,
        },
    ]


def seed_demo(store: Any, registry: Any, today: str) -> dict[str, Any]:
    """저장소를 비우고 시연 시작 상태로 채운다. 여러 번 불러도 같은 상태가 된다."""
    store.reset()
    seeder = _Seeder(registry, today)
    for org in ORGS:
        store.put_org({**org, "synthetic": True, "note": SYNTHETIC_NOTE})
    streams = _streams(seeder)
    for stream in streams:
        store.put_stream({**stream, "synthetic": True})
    vendors = _vendors(seeder)
    for profile in vendors:
        store.put_vendor_profile(profile)
    return {
        "synthetic": True,
        "note": SYNTHETIC_NOTE,
        "today": today,
        "emitterOrgId": EMITTER,
        "orgIds": [org["id"] for org in ORGS],
        "streamIds": [stream["id"] for stream in streams],
        "vendorOrgIds": [profile["orgId"] for profile in vendors],
        "evidenceIds": list(dict.fromkeys(seeder.evidence_ids)),
    }
