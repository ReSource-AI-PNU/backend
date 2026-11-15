"""Optional live PostGIS test; always isolates its tables in a disposable test schema."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.platform import db as module
from scripts.seed_platform import seed


@pytest.mark.skipif(
    not os.environ.get("TRACK_D_TEST_POSTGRES_URL"), reason="별도 PostGIS 테스트 DB 미설정"
)
def test_postgis_migrations_spatial_matching_and_persistence(monkeypatch):
    from test_platform import consent, headers, match

    url = os.environ["TRACK_D_TEST_POSTGRES_URL"]
    admin = create_engine(url)
    schema = f"track_d_test_{uuid4().hex}"
    with admin.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public"))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    eng = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        monkeypatch.setattr(module, "engine", lambda: eng)
        monkeypatch.setattr(settings, "platform_demo", True)
        monkeypatch.setattr(settings, "platform_service_key", "test-key")
        cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with Session(eng) as db:
            assert seed(db) == 100
            assert seed(db) == 0
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM platform_companies WHERE ST_SRID(location::geometry)=4326"
                    )
                ).scalar_one()
                == 100
            )

        def database():
            with Session(eng) as db:
                yield db

        app.dependency_overrides[module.database] = database
        with TestClient(app) as client:
            state = consent(client, match(client))
            assert state["bundles"][0]["ready"]
            saved = client.get(
                f"/api/v1/sessions/{state['session']['id']}", headers=headers()
            ).json()
            assert saved["version"] == state["version"]
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM platform_streams")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM platform_recipients")).scalar_one() == 3
        command.downgrade(cfg, "base")
    finally:
        app.dependency_overrides.clear()
        eng.dispose()
        # schema is constructed exclusively from a fixed prefix and a generated UUID.
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
