"""Azure Maps 2025-01-01 truck matrix; no silent straight-line fallback on API errors."""

import math

from app.core.config import settings
from app.platform.adapters import AdapterError, call


def matrix(points, client=None):
    unique = list(dict.fromkeys(tuple(p) for p in points))
    if len(unique) > 50:
        raise AdapterError("도로 행렬은 최대 50개 고유 지점입니다")
    if settings.platform_road_matrix_url:
        result = call(
            settings.platform_road_matrix_url,
            {"contractVersion": "resource-matrix-v1", "points": unique, "travelMode": "truck"},
            settings.platform_road_matrix_token,
            client=client,
        )
        meters, seconds = result.get("meters"), result.get("seconds")
        basis = "연결된 도로 서비스 · 트럭 경로"
    elif settings.azure_maps_key:
        result = call(
            "https://atlas.microsoft.com/route/matrix?api-version=2025-01-01",
            {
                "type": "FeatureCollection",
                "travelMode": "truck",
                "traffic": "historical",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "MultiPoint", "coordinates": unique},
                        "properties": {"pointType": kind},
                    }
                    for kind in ["origins", "destinations"]
                ],
            },
            client=client,
            extra_headers={
                "subscription-key": settings.azure_maps_key,
                "Content-Type": "application/geo+json",
            },
        )
        meters, seconds = (
            [[None] * len(unique) for _ in unique],
            [[None] * len(unique) for _ in unique],
        )
        properties = result.get("properties")
        if not isinstance(properties, dict) or not isinstance(properties.get("matrix"), list):
            raise AdapterError("도로 행렬 응답 형식 오류")
        seen = set()
        for cell in properties["matrix"]:
            if not isinstance(cell, dict):
                raise AdapterError("도로 행렬 셀 형식 오류")
            i, j = cell.get("originIndex"), cell.get("destinationIndex")
            if (
                not isinstance(i, int)
                or not isinstance(j, int)
                or not 0 <= i < len(unique)
                or not 0 <= j < len(unique)
                or cell.get("statusCode") != 200
                or (i, j) in seen
            ):
                raise AdapterError("도로 행렬에 도달 불가·잘못된 지점이 있습니다")
            seen.add((i, j))
            meters[i][j], seconds[i][j] = (
                cell.get("distanceInMeters"),
                cell.get("durationInSeconds"),
            )
        basis = "Azure Maps 트럭 도로 거리 · 과거 교통 기준"
    else:
        return None
    for values in [meters, seconds]:
        if (
            not isinstance(values, list)
            or len(values) != len(unique)
            or any(
                not isinstance(row, list)
                or len(row) != len(unique)
                or any(
                    not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in row
                )
                for row in values
            )
        ):
            raise AdapterError("거리·이동 시간 행렬이 누락되었거나 잘못되었습니다")
    indices = [unique.index(tuple(p)) for p in points]
    return (
        [[math.ceil(meters[i][j]) for j in indices] for i in indices],
        [[math.ceil(seconds[i][j] / 60) for j in indices] for i in indices],
        basis,
    )
