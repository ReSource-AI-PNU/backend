"""시연 데이터. 회사 이름, 좌표, 수치, 문서는 모두 가상이다(실제 업체나 견적이 아니다).

조직 레코드의 order는 조직 목록(GET /orgs)의 순서다. 시연 주인공 (주)한빛정밀이 맨 앞이다.
운영 조직(org_admin)을 뺀 시연 조직에는 demo: true를 붙인다. bff 모드의 체험하기(X-Demo-View)는
demo가 참인 조직만 읽기 전용으로 보여 준다. 운영 조직은 관리자 화면을 여는 조직이라 체험하기에 넣지 않는다.

seed_demo(store, registry, today)는 저장소를 비우고 같은 상태로 다시 채운다.
허가증, 매입 기준표, 계약서 값은 doc 근거로, 측정값은 measure 근거로, 사진 추정은 model 근거로
등록한다. 근거는 소유 조직만 볼 수 있고, 대화가 열릴 때 share_evidence로 상대에게 연다.
"""

from __future__ import annotations

import os
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
# 시연 진단에서 담당자가 답하는 값(가상). (흐름 ID, 항목) -> 값. 없는 질문에는 "모름"으로 답한다
DEMO_ANSWERS: dict[tuple[str, str], Any] = {
    ("st_film", "moisturePct"): 2.0,
    # 현행 처리비는 가정값으로 채우지 않는다. 시연 담당자가 계약대로 답하는 가상 값이다
    ("st_film", "currentCost"): 250,
    ("st_purge", "currentCost"): 250,
}

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
    """업체 프로필에 걸린 근거 ID(허가증, 올린 허가증의 AI 판독, 원문 대조 기록, 매입 기준표, 노선·차량)."""
    found: list[str] = []
    for permit in profile.get("permits") or []:
        found += permit.get("evidence") or []
    for document in profile.get("permitDocuments") or []:
        found += document.get("evidence") or []
    # 배출 기업 담당자의 허가증 원문 대조 기록(그 배출 기업만 본다)
    found += [c["evidence"] for c in profile.get("permitChecks") or [] if c.get("evidence")]
    for criterion in profile.get("criteria") or []:
        found += criterion.get("evidence") or []
    found += (profile.get("fleet") or {}).get("evidence") or []
    found += transport_evidence_ids(profile)
    return list(dict.fromkeys(found))


def transport_evidence_ids(profile: dict[str, Any]) -> list[str]:
    """운반 조건의 근거(노선 수거 조건과 노선 밖 수거 조건의 금액·횟수·조건 문장)."""
    transport = profile.get("transport") or {}
    found: list[str] = []
    for section in (transport, transport.get("offRoute") or {}):
        if not isinstance(section, dict):
            continue
        for key in ("evidence", "feeEvidence", "pickupsEvidence"):
            found += [e for e in section.get(key) or [] if isinstance(e, str)]
    return list(dict.fromkeys(found))


def share_vendor_terms(registry: Any, profile: dict[str, Any], org_id: str) -> list[str]:
    """대화가 열릴 때 업체의 허가증(올린 허가증의 AI 판독 포함), 매입 기준, 운반 조건 근거를 배출 기업에 연다.
    fleet 근거는 열지 않는다."""
    shared: list[str] = []
    for section in ("permits", "criteria", "permitDocuments"):
        for item in profile.get(section) or []:
            for evidence_id in item.get("evidence") or []:
                share_evidence(registry, evidence_id, org_id)
                shared.append(evidence_id)
    for evidence_id in transport_evidence_ids(profile):
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
        # 탐지 모델을 돌린 값이 아니므로 model 근거가 아니라 가정값으로 남긴다(화면에 "가정값"으로 보인다)
        eid = ids.assumption_id(f"시연사진-{stream_id}-오염비율", "v1")
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
    def permit(
        self,
        org: str,
        waste_item: str,
        materials: list[str],
        business: str = "폐기물종합재활용업",
    ) -> dict:
        """허가증 문서에서 읽고 담당자가 확인한 허가 품목과 업종(가상)."""
        eid = ids.doc_id(org, "허가증", waste_item)
        self.put(
            eid,
            org,
            f"허가 품목: {waste_item}",
            f"업종: {business} | 영업대상 폐기물: {waste_item} ({', '.join(materials)})",
            f"{business} 허가증",
            DOC_DATE,
        )
        return {
            "wasteItem": waste_item,
            "materials": materials,
            "business": business,
            "evidence": [eid],
        }

    def typed_permit(self, org: str, waste_item: str, materials: list[str]) -> dict:
        """업체가 화면에 입력만 한 허가 품목(허가증을 아직 올리지 않음). 업종은 모른다."""
        eid = ids.human_id(org, f"허가품목-{waste_item}")
        self.put(
            eid,
            org,
            f"허가 품목(업체 입력): {waste_item}",
            f"영업대상 폐기물: {waste_item} ({', '.join(materials)})",
            "업체 담당자 입력(허가증 원본 확인 전)",
            DOC_DATE,
        )
        return {"wasteItem": waste_item, "materials": materials, "evidence": [eid]}

    def permit_document(
        self,
        org: str,
        holder: tuple[str, str],
        items: list[tuple[str, list[str], str]],
        business: str,
    ) -> dict:
        """업체가 올린 허가증(가상)의 검토 대기 기록. AI가 읽은 품목·업종·상호·소재지와 그 쪽·원문 표기.

        items는 [(폐기물 종류, 재질, 원문 표기)]다. 담당자의 원문 대조 기록은 따로(permit_check) 만든다.
        """
        name = f"{holder[0]} 폐기물처리업 허가증(가상).pdf"
        quotes = [f"1쪽 · {quote}" for _, _, quote in items]
        quotes += [f"1쪽 · 업종: {business}", f"1쪽 · 상호 {holder[0]} · 소재지 {holder[1]}"]
        eid = self.put(
            ids.doc_id(org, "허가증-AI판독", "demo"),
            org,
            f"허가증(AI가 읽음, 대조 전): {name}",
            "\n".join(quotes),
            f"{name} · AI가 읽은 표기(담당자 대조 전)",
            DOC_DATE,
        )
        return {
            "id": f"pd_demo_{org.removeprefix('org_')}",
            "orgId": org,
            "name": name,
            "uploadedAt": f"{DOC_DATE}T09:00:00+00:00",
            "uploadedBy": org,
            "pages": [1],
            "items": [
                {"wasteItem": item, "materials": list(materials), "page": 1, "quote": quote}
                for item, materials, quote in items
            ],
            "business": {"name": business, "page": 1, "quote": f"업종: {business}"},
            "holder": {
                "name": holder[0],
                "address": holder[1],
                "page": 1,
                "quote": f"상호 {holder[0]} · 소재지 {holder[1]}",
            },
            "permitNumber": None,
            "evidence": [eid],
            "readBy": "ai",
        }

    def permit_check(
        self,
        emitter: tuple[str, str],
        vendor: str,
        document: dict,
        waste_item: str,
        material: str,
        process: str,
        quotes: tuple[str, str],
    ) -> dict:
        """배출 기업 담당자가 허가증 원문에서 품목과 처리 방법을 대조한 기록(가상, 시연 시작 전에 해 둔 것)."""
        record = {
            "vendorOrgId": vendor,
            "streamId": None,
            "sessionId": None,
            "documentId": document["id"],
            "document": document["name"],
            "page": 1,
            "wasteItem": waste_item,
            "materials": [material],
            "process": process,
            "itemFound": True,
            "processFits": True,
            "itemQuote": quotes[0],
            "processQuote": quotes[1],
            "checkedBy": {"orgId": emitter[0], "orgName": emitter[1], "userName": None},
            "checkedAt": f"{DOC_DATE}T10:00:00+00:00",
        }
        record["id"] = f"pc_demo_{vendor.removeprefix('org_')}_{ids.digest(record)[:6]}"
        evidence_id = ids.human_id(emitter[0], f"허가증대조-{vendor}-{record['id']}")
        self.registry.put(
            Evidence(
                id=evidence_id,
                kind="human",
                label=f"허가증 원문 대조: {waste_item}({material}) · {process}",
                value="맞음",
                excerpt=(
                    f"{document['name']} 1쪽 원문 대조\n품목 {waste_item}({material}): 있음 (원문: {quotes[0]})\n"
                    f"처리 방식 {process}: 맞음 (원문: {quotes[1]})"
                ),
                source=f"{emitter[1]} 담당자 확인({DOC_DATE}) · {SYNTHETIC_NOTE}",
                as_of=DOC_DATE,
                access=(emitter[0],),
                meta={
                    "ownerOrgId": emitter[0],
                    "permitCheck": record["id"],
                    "documentId": document["id"],
                    "page": 1,
                    "synthetic": True,
                },
            )
        )
        self.evidence_ids.append(evidence_id)
        record["evidence"] = evidence_id
        return record

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
    # 허가 확인 상태를 세 가지로 보인다(judge/permits.py): 새길자원은 한빛정밀 담당자가 허가증 원문을 대조해 둔
    # 업체(대조 완료), 남해열분해는 허가증을 올렸지만 아직 아무도 대조하지 않은 업체(검토 대기), 미소환경은
    # 허가 품목만 입력한 업체(원본 확인 필요)다. 대조 기록은 한빛정밀의 것이라 다온산업에는 쓰이지 않는다
    sg_doc = s.permit_document(
        sg,
        ("(주)새길자원", "부산광역시 강서구 미음동"),
        [
            ("폐플라스틱", ["PP", "PE"], "영업대상 폐기물: 폐플라스틱(PP, PE)"),
            ("포장재", ["LDPE", "LLDPE"], "영업대상 폐기물: 폐포장재(LDPE, LLDPE 필름)"),
        ],
        "폐기물종합재활용업",
    )
    nh_doc = s.permit_document(
        nh,
        ("(주)남해열분해", "경상남도 김해시 주촌면"),
        [("폐플라스틱", [], "영업대상 폐기물: 폐합성수지")],
        "폐기물종합재활용업",
    )
    hanbit = (EMITTER, "(주)한빛정밀")
    sg_checks = [
        s.permit_check(
            hanbit,
            sg,
            sg_doc,
            "폐플라스틱",
            "PP",
            "재활용",
            ("영업대상 폐기물: 폐플라스틱(PP, PE)", "업종: 폐기물종합재활용업"),
        ),
        s.permit_check(
            hanbit,
            sg,
            sg_doc,
            "포장재",
            "LDPE",
            "재활용",
            ("영업대상 폐기물: 폐포장재(LDPE, LLDPE 필름)", "업종: 폐기물종합재활용업"),
        ),
    ]
    return [
        {
            "orgId": sg,
            "permits": [
                s.permit(sg, "폐플라스틱", ["PP", "PE"]),
                s.permit(sg, "포장재", ["LDPE", "LLDPE"]),
            ],
            "permitDocuments": [sg_doc],
            "permitChecks": sg_checks,
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
            "fleet": {
                **s.fleet(sg, [128.8721, 35.1493], 5000, 20, 1200),
                # 수거 노선(가상). ④ 업체 탐색이 공장 지역을 지나는지 본다
                "routes": [
                    {
                        "name": "녹산·신호 노선",
                        "days": ["화", "목"],
                        "regions": ["부산 강서구"],
                        "complexes": ["녹산국가산업단지", "신호일반산업단지"],
                    }
                ],
                # 수거 가능 지역(가상). 노선 밖이라도 이 안이면 따로 수거하러 간다(계약 검토가 본다)
                "serviceAreas": ["부산 강서구", "부산 사하구", "경남 김해시"],
                "maxPickupKm": 40,
            },
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
            "permitDocuments": [nh_doc],
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
            "fleet": {
                **s.fleet(nh, [128.8189, 35.2296], 8000, 30, 1500),
                "routes": [
                    {
                        "name": "김해·강서 노선",
                        "days": ["월", "수", "금"],
                        "regions": ["경남 김해시", "부산 강서구"],
                        "complexes": [],
                    }
                ],
                "serviceAreas": ["경남 김해시", "부산 강서구", "경남 창원시"],
                "maxPickupKm": 60,
            },
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
            # H100(Azure ML 계산 인스턴스)의 Ollama. 2026-11-19 시험셋 20사례(업체 측만 바꿈)에서 qwen2.5:7b는 10/20,
            # qwen2.5:32b는 20/20(초안 첫 시도 통과 0.968, 담당자 인계 0)으로 Foundry gpt-5-mini(19/20)와 같은 수준이었다
            "model": {"provider": "ollama", "name": "qwen2.5:32b"},
            "synthetic": True,
        },
        {
            "orgId": ms,
            # 필름(포장재·LDPE) 허가 품목이 없다: 필름 흐름으로는 대화를 열지 않아야 한다.
            # 허가증을 아직 올리지 않고 허가 품목만 입력한 업체다: 배정되면 "허가증 원본 확인 필요"로 확정이 보류된다
            "permits": [s.typed_permit(ms, "폐플라스틱", ["PP", "ABS"])],
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
            "fleet": {
                **s.fleet(ms, [128.9795, 35.1462], 3500, 14, 1100),
                "routes": [
                    {
                        "name": "사상·사하 노선",
                        "days": ["월", "수", "금"],
                        "regions": ["부산 사상구", "부산 사하구"],
                        "complexes": [],
                    }
                ],
            },
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


def demo_permits() -> str:
    """시연 업체의 허가 확인 상태. mixed(기본): 새길 대조 완료 · 남해 검토 대기 · 미소 원본 확인 필요.
    verified: 두 배출 기업 담당자가 세 업체의 허가 품목을 모두 원문 대조해 둔 상태(확정까지 가는 시험이 쓴다)."""
    return "verified" if os.environ.get("V3_DEMO_PERMITS") == "verified" else "mixed"


_HOLDERS = {
    "org_saegil": ("(주)새길자원", "부산광역시 강서구 미음동"),
    "org_namhae": ("(주)남해열분해", "경상남도 김해시 주촌면"),
    "org_miso": ("(주)미소환경", "부산광역시 사상구 학장동"),
}


def verify_all(
    seeder: _Seeder,
    vendors: list[dict[str, Any]],
    emitters: list[str],
    names: dict[str, str] | None = None,
    holders: dict[str, tuple[str, str]] | None = None,
) -> None:
    """모든 업체에 허가증을 올리고, emitters의 담당자가 허가 품목(재질마다)과 처리 방식을 모두 대조해 둔다.

    names는 조직 이름(없으면 시연 조직), holders는 업체마다 허가증의 상호·소재지다(없으면 조직 이름과 "부산광역시").
    """
    names = names or {org["id"]: org["name"] for org in ORGS}
    holders = holders or _HOLDERS
    for profile in vendors:
        org = profile["orgId"]
        documents = list(profile.get("permitDocuments") or [])
        if not documents:
            documents = [
                seeder.permit_document(
                    org,
                    holders.get(org, (names.get(org, org), "부산광역시")),
                    [
                        (
                            p["wasteItem"],
                            list(p.get("materials") or []),
                            f"영업대상 폐기물: {p['wasteItem']}",
                        )
                        for p in profile.get("permits") or []
                    ],
                    "폐기물종합재활용업",
                )
            ]
        checks = list(profile.get("permitChecks") or [])
        done = {
            (c["checkedBy"]["orgId"], c["wasteItem"], tuple(c["materials"]), c["process"])
            for c in checks
        }
        process = str(profile.get("process") or "재활용")
        for emitter in emitters:
            for permit in profile.get("permits") or []:
                for material in permit.get("materials") or []:
                    key = (emitter, permit["wasteItem"], (material,), process)
                    if key in done:
                        continue
                    done.add(key)
                    checks.append(
                        seeder.permit_check(
                            (emitter, names.get(emitter, emitter)),
                            org,
                            documents[0],
                            permit["wasteItem"],
                            material,
                            process,
                            (f"영업대상 폐기물: {permit['wasteItem']}", "업종: 폐기물종합재활용업"),
                        )
                    )
        profile["permitDocuments"] = documents
        profile["permitChecks"] = checks


def seed_demo(store: Any, registry: Any, today: str, *, wipe: bool = True) -> dict[str, Any]:
    """저장소를 비우고 시연 시작 상태로 채운다. 여러 번 불러도 같은 상태가 된다.

    wipe가 거짓이면 저장소를 비우지 않고 시연 조직의 레코드만 다시 쓴다(bff 모드의 되돌리기는
    시연 조직의 자료를 먼저 지운 뒤 이렇게 부른다).
    """
    if wipe:
        store.reset()
    seeder = _Seeder(registry, today)
    for org in ORGS:
        demo = org["role"] != "admin"
        store.put_org({**org, "synthetic": True, "note": SYNTHETIC_NOTE, "demo": demo})
    streams = _streams(seeder)
    for stream in streams:
        store.put_stream({**stream, "synthetic": True})
    vendors = _vendors(seeder)
    if demo_permits() == "verified":
        verify_all(seeder, vendors, [EMITTER, OTHER_EMITTER])
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
