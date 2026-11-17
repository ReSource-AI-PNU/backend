"""Validate provenance and load normalized CSV exports atomically; no legal verification implied."""

import argparse
import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.platform.db import SourceRow, engine

KINDS = {"prices", "permits", "emissions", "laws", "tenants"}


def read_records(path, kind):
    if kind not in KINDS:
        raise ValueError("지원하지 않는 자료 종류")
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        for i, row in enumerate(csv.DictReader(file), 2):
            for key in ["source_url", "retrieved_at", "license", "record_id", "payload_json"]:
                if not row.get(key, "").strip():
                    raise ValueError(f"{i}행: {key} 누락")
            parsed = urlparse(row["source_url"])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise ValueError(f"{i}행: HTTPS 원문 주소 필요")
            date.fromisoformat(row["retrieved_at"])
            payload = json.loads(row.pop("payload_json"))
            if not isinstance(payload, dict):
                raise TypeError(f"{i}행: payload_json은 객체여야 합니다")
            identity = f"{kind}:{row['source_url']}:{row['record_id']}"
            rows.append(
                SourceRow(
                    id=hashlib.sha256(identity.encode()).hexdigest(),
                    kind=kind,
                    payload={**row, "record": payload, "verified": False},
                )
            )
    if len({row.id for row in rows}) != len(rows):
        raise ValueError("중복 원문 레코드 ID")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=sorted(KINDS))
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()
    rows = read_records(args.csv, args.kind)
    with Session(engine()) as db:
        for row in rows:
            db.merge(row)
        db.commit()
    print(f"{len(rows)}건 적재 · 내용 검증 전 · 판정 엔진에 자동 반영하지 않음")
