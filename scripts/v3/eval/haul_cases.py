"""단일 PP 러너 1,000kg 사례에서 수거 거리나 운반 견적만 바꿀 때 결과가 어떻게 달라지는지 남긴다(평가 대응 D).

가상 배출 기업 하나(녹산산단, 흐름은 PP 러너 하나: 한 달 1,000kg 계근, 오염도 1.0%·수분 0.3% 측정, 현행 혼합 소각
kg당 250원)와 시연 업체 셋으로 찾기를 승인 대기까지 돌린다(규칙 정책, 단가 협상 없음, 모델을 부르지 않는다).
그다음 한 번에 하나만 바꾼다.

    기준          시연 업체 그대로(새길자원 노선이 녹산을 지나고 운반비는 단가에 포함)
    거리만        새길자원 차고지를 약 30km 먼 곳으로 옮긴다(운반 조건은 그대로)
    견적만        새길자원 노선 수거 운반비를 회당 50,000원 × 월 2회(공장 부담)로 바꾼다
    노선 밖 견적  새길자원 노선이 녹산을 지나지 않게 바꾸고(수거 가능 지역도 지움), 노선 밖 수거를 회당 30,000원 ×
                  월 2회로 답한다
    횟수 모름     노선 밖 수거를 회당 30,000원만 답하고 월 횟수는 비운다
    수거 불가     노선 밖 공장은 수거하지 않는다고 답한다

변형마다 두 번 잰다. 조건을 바꾼 뒤 새로 찾은 결과(표)와, 합의한 뒤에 업체가 업체 정보에서 같은 조건으로 고치고
(service.update_vendor) → 승인이 막히고(수거·운반 조건 변경) → 담당자가 다시 진행(service.run_session)한 결과다.
둘이 같아야 한다. 결과 한 장의 배정, 단가, 매각 수익, 운반비, 월 손익, 현행 대비, 탄소(처리 방식·운송), 공장 → 업체
거리, 수거 상태를 표로 남기고, 계산 범위 밖인 것을 함께 적는다.

    uv run python -m scripts.v3.eval.haul_cases --out docs/v3/eval/2026-11-19-haul-cases
"""

from __future__ import annotations

import argparse
import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

RUNNER_ORG = "org_runner_case"
RUNNER_STREAM = "st_runner_case"
SAEGIL = "org_saegil"
TODAY = "2026-11-19"
CLOCK = "2026-11-19T00:00:00+00:00"
FAR_DEPOT = [129.1600, 35.1700]  # 해운대 쪽 가상 지점(녹산에서 직선 약 30km 더 멂)

OUT_OF_SCOPE = [
    (
        "운송 배출은 '트럭 운송 계수(톤·km당) × 월 발생량 × 공장 → 업체 차고지 거리'로만 센다. 계수에 빈 차로 돌아오는 몫이 "
        "들어 있으므로 수거 횟수나 노선 밖 별도 방문을 바꿔도 운송 배출은 달라지지 않는다(차량 크기, 실제 경로, 다른 공장을 "
        "함께 도는 노선 효율은 넣지 않았다)."
    ),
    "지금 처리(혼합 소각)로 실어 가는 운송 배출은 알 수 없어 빼지 않았다(옮기는 쪽을 불리하게 본다).",
    "신재 대체 효과(재생 원료가 새 원료를 대신해 줄이는 배출)는 넣지 않았다.",
    (
        "월 손익은 실제로 오가는 돈(매각 수익 또는 처리비, 공장이 내는 운반비, 전처리 인건비)만이다. 탄소를 배출권 시세로 "
        "환산한 값은 배정을 고를 때만 쓰고 월 손익에 더하지 않았다."
    ),
    (
        "업체가 금액이나 월 수거 횟수를 밝히지 않은 운반비는 0원으로 두지 않고 '별도 운반비 반영 전'으로 표시한다. 그때 "
        "배정을 고르는 계산은 알 수 없는 운반비를 넣지 못한다."
    ),
    (
        "업체의 운행 단가(km당 운송비)는 업체만 보는 값이라 공장의 월 손익에 쓰지 않는다. 공장이 내는 운반비는 업체가 밝힌 "
        "견적뿐이다."
    ),
]


def build_app() -> Any:
    from app.v3 import service
    from app.v3.evidence.registry import InMemoryRegistry
    from app.v3.store import InMemoryStore

    app = service.App(
        InMemoryStore(lambda: CLOCK), InMemoryRegistry(), today=TODAY, negotiate=False
    )
    service.seed(app)
    _add_runner(app)
    return app


def _add_runner(app: Any) -> None:
    """가상 배출 기업 하나와 PP 러너 흐름 하나를 더한다(시연 시드와 같은 근거 모양)."""
    from app.v3.demo.seed import SYNTHETIC_NOTE, _Seeder

    seeder = _Seeder(app.registry, TODAY)
    app.store.put_org(
        {
            "id": RUNNER_ORG,
            "order": 50,
            "name": "(가상) 러너정밀",
            "role": "emitter",
            "region": "부산 강서구 녹산산단",
            "location": [128.8260, 35.0871],
            "synthetic": True,
            "note": SYNTHETIC_NOTE,
            "demo": False,
        }
    )
    app.store.put_stream(
        {
            "id": RUNNER_STREAM,
            "orgId": RUNNER_ORG,
            "name": "PP 러너",
            "process": "사출",
            "wasteItem": "폐플라스틱",
            "material": "PP",
            "origin": "PIR",
            "form": "가공 전",
            "lot": None,
            "monthlyKg": 1000,
            "monthlyKgBasis": "measured",
            "monthlyKgEvidence": seeder.monthly(RUNNER_ORG, RUNNER_STREAM, 1000, True),
            "bulkDensityKgPerM3": 250,
            "contaminants": [],
            "measure": seeder.measure(RUNNER_ORG, RUNNER_STREAM, 1.0, 0.3),
            "photo": None,
            "currentTreatment": seeder.current(RUNNER_ORG, RUNNER_STREAM, 250),
            "synthetic": True,
        }
    )


def _value(claim: dict[str, Any] | None) -> float | None:
    if not isinstance(claim, dict) or not isinstance(claim.get("value"), (int, float)):
        return None
    return float(claim["value"])


def _row(view: dict[str, Any]) -> dict[str, Any]:
    result = view.get("result") or {}
    item = next(
        (a for a in result.get("assignments") or [] if a["streamId"] == RUNNER_STREAM), None
    )
    if item is None:
        return {"status": view["session"]["status"], "vendor": None}
    pickup = item.get("pickup") or {}
    approval = next(
        (a for a in view.get("approvals") or [] if a["threadId"] == item.get("threadId")), None
    )
    return {
        "session": view["session"]["status"],
        "vendor": item.get("vendorName") or "지금 방식 그대로",
        "route": item.get("route"),
        "status": item.get("status"),
        "unitPrice": _value(item.get("unitPrice")),
        "revenue": _value(item.get("revenue")),
        "transport": _value(item.get("transport")),
        "transportPaidBy": item.get("transportPaidBy"),
        "transportPending": bool(item.get("transportPending")),
        "monthlyAfter": _value(item.get("monthlyAfter")),
        "monthlyNow": _value(item.get("monthlyNow")),
        "difference": _value(item.get("difference")),
        "carbon": _value(item.get("carbon")),
        "carbonProcess": _value(item.get("carbonProcess")),
        "carbonTransport": _value(item.get("carbonTransport")),
        "transportKm": _value(item.get("transportKm")),
        "pickup": pickup.get("label") or pickup.get("status"),
        "pickupFee": _value(pickup.get("fee")),
        "pickupCount": _value(pickup.get("pickups")),
        "monthlyFormula": (item.get("monthlyAfter") or {}).get("formula"),
        "alternatives": [
            {
                "vendor": alt.get("vendorName"),
                "monthlyAfter": _value(alt.get("monthlyAfter")),
                "note": alt.get("note"),
            }
            for alt in item.get("alternatives") or []
        ],
        "checks": [
            f"{c['key']}:{c['status']}" for c in item.get("checks") or [] if c["status"] != "met"
        ],
        "approvalBlocked": bool(approval and approval.get("blocked")),
        "totalDifference": _value((result.get("totals") or {}).get("difference")),
    }


def _settle(app: Any, view: dict[str, Any]) -> dict[str, Any]:
    """질문이 남았으면 '모름'으로 답하고, 막힌 합의는 담당자처럼 다시 진행해 승인 대기까지 간다."""
    from app.v3 import service

    sid = view["session"]["id"]
    for _ in range(4):
        status = view["session"]["status"]
        if status == "awaiting_answers":
            for question in view["questions"]:
                if not question["answered"]:
                    service.answer_question(app, sid, question["id"], "unknown", RUNNER_ORG)
        elif (
            status == "awaiting_approval"
            and not any(a.get("blocked") for a in view.get("approvals") or [])
            or status not in ("awaiting_approval", "idle")
        ):
            return view
        view = service.run_session(app, sid, RUNNER_ORG)
        if view["session"]["status"] == "idle":
            return view
    return view


def _vendor_patch(app: Any, change: str) -> dict[str, Any] | None:
    from app.v3 import service

    profile = service.get_vendor(app, SAEGIL, SAEGIL)
    fleet = copy.deepcopy(profile.get("fleet") or {})
    routes_elsewhere = [
        {
            "name": "미음·지사 노선",
            "days": ["화", "목"],
            "regions": ["부산 강서구 미음동"],
            "complexes": ["미음산업단지"],
        }
    ]
    if change == "far":
        return {"fleet": {"depot": FAR_DEPOT}}
    if change == "route_fee":
        return {"transport": {"basis": "separate", "feePerPickupKrw": 50000, "pickupsPerMonth": 2}}
    off = {
        "off_fee": {
            "status": "visit",
            "basis": "separate",
            "feePerPickupKrw": 30000,
            "pickupsPerMonth": 2,
        },
        "off_fee_only": {"status": "visit", "basis": "separate", "feePerPickupKrw": 30000},
        "off_none": {"status": "unavailable"},
    }.get(change)
    if off is None:
        return None
    fleet["routes"] = routes_elsewhere
    # 노선을 녹산 밖으로 옮기고, 시드의 수거 가능 지역(부산 강서구 등)도 지워 노선 밖 조건만 이 공장에 걸리게 한다
    return {
        "fleet": {"routes": fleet["routes"], "serviceAreas": None, "maxPickupKm": None},
        "transport": {"offRoute": off},
    }


CASES = [
    ("기준", None),
    ("거리만", "far"),
    ("견적만(노선 수거 운반비 회당 5만 × 월 2회)", "route_fee"),
    ("노선 밖 견적(회당 3만 × 월 2회)", "off_fee"),
    ("노선 밖 견적(회당 3만, 횟수 모름)", "off_fee_only"),
    ("노선 밖 수거 불가", "off_none"),
]


def _search(app: Any) -> dict[str, Any]:
    from app.v3 import service

    view = service.create_session(app, RUNNER_ORG, RUNNER_ORG)
    return _settle(app, service.run_session(app, view["session"]["id"], RUNNER_ORG))


KEYS = ("vendor", "transport", "transportPending", "monthlyAfter", "difference", "pickup")


def run() -> list[dict[str, Any]]:
    """사례마다 두 번 잰다: 조건을 바꾼 뒤 새로 찾기(fresh)와, 합의한 뒤에 업체가 고치고 다시 진행하기(edited)."""
    from app.v3 import service

    rows = []
    for title, change in CASES:
        app = build_app()
        if change:
            service.update_vendor(app, SAEGIL, _vendor_patch(app, change), SAEGIL)
        fresh = _row(_search(app))
        row: dict[str, Any] = {"case": title, "change": change, "after": fresh}
        if change and change != "far":
            # 합의한 뒤에 업체가 같은 조건으로 고치면: 승인이 막히고, 담당자가 다시 진행하면 새로 찾은 것과 같아야 한다
            app = build_app()
            view = _search(app)
            service.update_vendor(app, SAEGIL, _vendor_patch(app, change), SAEGIL)
            blocked = service.get_view(app, view["session"]["id"], RUNNER_ORG)
            edited = _row(_settle(app, blocked))
            row["edited"] = {
                "blockedAfterEdit": _row(blocked)["approvalBlocked"],
                "sameAsFresh": all(edited.get(k) == fresh.get(k) for k in KEYS),
                **{k: edited.get(k) for k in KEYS},
            }
        rows.append(row)
    return rows


def _won(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):,.0f}원"


def _kg(value: float | None) -> str:
    return "-" if value is None else f"{value * 1000:,.1f} kgCO2e"


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# 단일 PP 러너 1,000kg: 수거 거리·운반 견적만 바꾼 비교 (실행 {date.today().isoformat()})",
        "",
        (
            "가상 배출 기업 하나(녹산산단)의 PP 러너 한 달 1,000kg(오염도 1.0%·수분 0.3% 측정, 현행 혼합 소각 kg당 250원)을 "
            "시연 업체 셋에 찾고, 새길자원의 조건을 한 번에 하나만 바꿨다. 규칙 정책으로 돌렸고 단가 협상은 하지 않았다"
            "(`scripts/v3/eval/haul_cases.py`). 표는 조건을 바꾼 뒤 새로 찾은 결과이고, 견적을 바꾼 사례는 합의한 뒤에 업체가 "
            "고치고 담당자가 다시 진행한 결과도 함께 견줬다."
        ),
        "",
        "| 사례 | 받는 곳 | 수거 | 매각 수익 | 운반비 | 월 손익 | 현행 | 현행 대비 | 탄소(처리) | 탄소(운송) | 거리 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        a = row["after"]
        pending = " (별도 운반비 반영 전)" if a.get("transportPending") else ""
        lines.append(
            f"| {row['case']} | {a.get('vendor')} | {a.get('pickup') or '-'} | {_won(a.get('revenue'))} | "
            f"{_won(-a['transport']) if a.get('transport') else ('반영 전' if a.get('transportPending') else '0원(단가 포함)' if a.get('transportPaidBy') == 'vendor' else '-')} | "
            f"{_won(a.get('monthlyAfter'))}{pending} | {_won(a.get('monthlyNow'))} | {_won(a.get('difference'))}{pending} | "
            f"{_kg(a.get('carbonProcess'))} | {_kg(a.get('carbonTransport'))} | "
            f"{'-' if a.get('transportKm') is None else f'{a["transportKm"]:.1f} km'} |"
        )
    lines += ["", "## 무엇이 달라졌나", ""]
    base = rows[0]["after"]
    for row in rows[1:]:
        a = row["after"]
        changed = [
            name
            for key, name in (
                ("vendor", "받는 곳"),
                ("transport", "운반비"),
                ("monthlyAfter", "월 손익"),
                ("difference", "현행 대비"),
                ("carbonTransport", "운송 탄소"),
                ("transportKm", "거리"),
                ("pickup", "수거 상태"),
                ("transportPending", "운반비 반영 여부"),
            )
            if a.get(key) != base.get(key)
        ]
        same = [
            name
            for key, name in (("carbonProcess", "처리 방식 탄소"), ("monthlyNow", "현행 월 손익"))
            if a.get(key) == base.get(key)
        ]
        lines.append(
            f"- **{row['case']}**: 달라진 것 {', '.join(changed) or '없음'} · 그대로인 것 {', '.join(same) or '없음'}"
        )
        edited = row.get("edited")
        if edited:
            lines.append(
                "  - 합의한 뒤에 업체가 같은 조건으로 고치면: "
                + (
                    "승인이 막히고 앞선 승인이 지워진다"
                    if edited["blockedAfterEdit"]
                    else "승인이 막히지 않았다"
                )
                + " → 담당자가 다시 진행한 결과가 "
                + (
                    "새로 찾은 결과와 같다"
                    if edited["sameAsFresh"]
                    else f"새로 찾은 결과와 다르다({edited})"
                )
            )
        alts = [x for x in a.get("alternatives") or [] if x.get("note")]
        for alt in alts:
            lines.append(
                f"  - 다른 선택지 {alt['vendor']}: {_won(alt.get('monthlyAfter'))} · {alt['note']}"
            )
        if a.get("monthlyFormula"):
            lines.append(f"  - 월 손익 계산식: {a['monthlyFormula']}")
    lines += ["", "## 계산 범위 밖", ""] + [f"- {text}" for text in OUT_OF_SCOPE]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help="이 이름으로 .json과 .md를 쓴다")
    args = parser.parse_args(argv)
    rows = run()
    text = markdown(rows)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".json").write_text(
            json.dumps(
                {"date": TODAY, "rows": rows, "outOfScope": OUT_OF_SCOPE},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        out.with_suffix(".md").write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
