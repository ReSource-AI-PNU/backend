"""이 변경 전에 기록된 검수의 탄소 계산 기준을 고정한다(app/v3/outcome_carbon.py).

백엔드 컨테이너 안에서 서버와 같은 설정(DATABASE_URL 등)으로 돈다.

    python -m scripts.v3.backfill_outcome_carbon            # 미리 보기: 기록마다 상태만 적고 아무것도 쓰지 않는다
    python -m scripts.v3.backfill_outcome_carbon --apply    # 기준이 없는 기록을 재구성해 고정한다

기록할 때의 기준이 없는 검수는 확정한 합의의 배정 계산(그때의 현행 처리·월 발생량·배출계수 ID)을 지금 계수로 다시
계산해 값이 같을 때만 "합의 당시 기준으로 재구성"(reconstructed)으로 고정하고, 아니면 "당시 기준 확인 불가"
(unverified)로 고정해 탄소를 계산하지 않는다. 오늘의 폐기물 자료·업체 정보로 소급하지 않는다.
실적 리포트도 처음 읽을 때 같은 일을 하지만, 배포 직후 한 번 돌려 두면 계수를 고치기 전에 재구성 여부가 정해진다.
이미 고정한 기록은 건드리지 않으므로 여러 번 돌려도 결과가 같다. 한 줄에 기록 하나(JSON), 마지막 줄은 합계다.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

from app.v3 import feedback, outcome_carbon


def main(argv: list[str] | None = None, app: Any = None) -> int:
    parser = argparse.ArgumentParser(description="지난 검수 기록의 탄소 계산 기준 고정")
    parser.add_argument(
        "--apply", action="store_true", help="재구성한 기준을 근거 레지스트리에 쓴다"
    )
    args = parser.parse_args(argv)
    if app is None:
        from app.v3.api import build_app

        app = build_app()
    counts: Counter[str] = Counter()
    for record in feedback.outcomes(app):
        found = outcome_carbon.latest(app.registry, record)
        if found is not None:
            state, snapshot = "already", found
        else:
            snapshot = outcome_carbon.reconstruct(app, record)
            state = "frozen" if args.apply else "would_freeze"
            if args.apply:
                label = outcome_carbon.snapshot_label(record)
                outcome_carbon.freeze(app.registry, snapshot, record, label)
        counts[f"{state}:{snapshot['status']}"] += 1
        print(
            json.dumps(
                {
                    "outcome": record["id"],
                    "vendorOrgId": record["vendorOrgId"],
                    "emitterOrgId": record["emitterOrgId"],
                    "inspectedOn": record["inspectedOn"],
                    "state": state,
                    "status": snapshot["status"],
                    "reason": snapshot.get("reason"),
                },
                ensure_ascii=False,
            )
        )
    print(
        json.dumps(
            {"summary": dict(sorted(counts.items())), "applied": args.apply}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
