"""Track D storage; PostGIS point column is derived from validated coordinates."""

import sqlalchemy as sa
from alembic import op

revision = "0001_platform"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_companies",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("complex_id", sa.String(100), nullable=False),
        sa.Column("owner_id", sa.String(200), nullable=True),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_index("ix_platform_companies_complex_id", "platform_companies", ["complex_id"])
    op.create_index("ix_platform_companies_owner_id", "platform_companies", ["owner_id"])
    op.create_table(
        "platform_sessions",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("org_id", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_index("ix_platform_sessions_org_id", "platform_sessions", ["org_id"])
    op.create_table(
        "platform_sources",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_index("ix_platform_sources_kind", "platform_sources", ["kind"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        op.execute("""ALTER TABLE platform_companies ADD COLUMN location geography(Point,4326)
            GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude,latitude),4326)::geography) STORED""")
        op.execute(
            "CREATE INDEX ix_platform_companies_location ON platform_companies USING gist(location)"
        )


def downgrade():
    op.drop_table("platform_sources")
    op.drop_table("platform_sessions")
    op.drop_table("platform_companies")
