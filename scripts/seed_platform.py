"""Idempotent demo import. Never overwrite edited profiles or seed production implicitly."""

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.platform.db import CompanyRow, engine
from app.platform.models import CollectorPolicy, CompanyProfile, MaterialSupply

DATA = Path(__file__).resolve().parents[1] / "data" / "companies"


def profiles():
    for file in sorted(DATA.glob("*.json")):
        for row in json.loads(file.read_text(encoding="utf-8")):
            supplies = []
            for w in row["wasteStreams"]:
                detail = w.get("detail", "").upper()
                # Only explicitly identified resins become matching inputs. Unknown stays absent.
                material = next(
                    (m for m in ["LDPE", "HDPE", "PP", "PET", "ABS", "PVC"] if m in detail), None
                )
                if not material or not w["monthlyKg"]:
                    continue
                supplies.append(
                    MaterialSupply(
                        item=w["item"],
                        material=material,
                        form="film" if "필름" in detail else "rigid",
                        origin="PCR" if "포장" in detail or "스트레치" in detail else "PIR",
                        monthly_kg=w["monthlyKg"],
                    )
                )
            # Explicit, labelled synthetic kick-scene inputs; not factual company data.
            demo_film = {"noksan-001": 300, "noksan-017": 420, "noksan-018": 510}
            if row["id"] in demo_film:
                supplies.append(
                    MaterialSupply(
                        item="포장재",
                        material="LDPE",
                        form="film",
                        origin="PCR",
                        monthly_kg=demo_film[row["id"]],
                    )
                )
            policy = None
            if row["role"] in {"처리업체", "수요처"}:
                policy = CollectorPolicy(
                    permits=row.get("permits", []),
                    materials=["PP", "LDPE", "HDPE", "PET"],
                    forms=["film", "rigid"],
                    min_kg=1000,
                    truck_kg=1000,
                    days=["목", "금"],
                    max_per_kg=140,
                    max_per_month=2,
                    evidence="가상 시연 조건 · 실허가 미확인",
                    minimum_basis="month",
                )
            yield CompanyProfile(
                id=row["id"],
                name=row["name"],
                complex_id=file.stem,
                location=row["location"],
                role=row["role"],
                supplies=supplies,
                collector=policy,
                is_demo=True,
            )


def seed(db):
    count = 0
    for c in profiles():
        if db.get(CompanyRow, c.id):
            continue
        db.add(
            CompanyRow(
                id=c.id,
                complex_id=c.complex_id,
                longitude=c.location[0],
                latitude=c.location[1],
                payload=c.model_dump(mode="json", by_alias=True),
            )
        )
        count += 1
    db.commit()
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", required=True)
    parser.parse_args()
    with Session(engine()) as db:
        print(f"가상 기업 {seed(db)}곳 추가 (기존 행 보존)")
