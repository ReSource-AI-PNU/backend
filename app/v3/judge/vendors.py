"""④ 업체 탐색. 걸러진 업체는 이유와 근거를 함께 돌려준다."""

from __future__ import annotations

from app.v3.judge.common import Recorder, distance_matrix, num, permit_match


def find_vendors(
    registry,
    *,
    org_id,
    today,
    stream,
    vendors,
    origin=None,
    maxKm=None,
    distance_provider=None,
):
    args = {"stream": stream, "vendors": vendors, "origin": origin, "maxKm": maxKm}
    rec = Recorder("find_vendors", registry, org_id, today, args)
    candidates, excluded = [], []
    limit = num(maxKm)
    for i, vendor in enumerate(sorted(vendors, key=lambda v: str(v.get("orgId")))):
        vid = vendor.get("orgId")
        matched, evidence = permit_match(stream, vendor)
        if matched is not True:
            reason = (
                "허가 품목에 이 흐름의 품목·재질이 없습니다."
                if matched is False
                else "허가 품목이 올라와 있지 않습니다. 확인 필요."
            )
            excluded.append(
                {"vendorOrgId": vid, "reason": reason, "evidence": rec.visible(evidence)}
            )
            continue
        crits = [
            c for c in vendor.get("criteria") or [] if c.get("material") == stream.get("material")
        ]
        if not crits:
            excluded.append(
                {"vendorOrgId": vid, "reason": "이 재질의 매입 기준이 없습니다.", "evidence": []}
            )
            continue
        entry = {"vendorOrgId": vid, "distance": None, "distanceAssumed": None}
        depot = (vendor.get("fleet") or {}).get("depot")
        if origin and depot:
            matrix, assumed, ev = distance_matrix(rec, [origin, depot], distance_provider)
            if matrix is not None:
                formula = "직선 거리 × 우회 계수 (가정값)" if assumed else "도로 거리"
                claim = rec.number(f"distanceKm{i}", matrix[0][1], "km", formula, ev)
                entry.update(distance=claim["id"], distanceAssumed=assumed, km=claim["value"])
                if limit is not None and claim["value"] > limit:
                    excluded.append(
                        {"vendorOrgId": vid, "reason": "탐색 거리 밖입니다.", "evidence": ev}
                    )
                    continue
        candidates.append(entry)
    candidates.sort(key=lambda c: (c.get("km") is None, c.get("km") or 0.0, str(c["vendorOrgId"])))
    for c in candidates:
        c.pop("km", None)
    if any(c["distanceAssumed"] for c in candidates):
        rec.check("distance", "met", "거리는 직선 거리에 우회 계수를 곱한 가정값입니다.")
    output = {"streamId": stream.get("id"), "candidates": candidates, "excluded": excluded}
    output["excluded"] = [
        x for x in excluded if x["vendorOrgId"] not in {c["vendorOrgId"] for c in candidates}
    ]
    return rec.result("calculated", output)
