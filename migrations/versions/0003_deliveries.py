"""Durable approved document outbox."""

import sqlalchemy as sa
from alembic import op

revision = "0003_deliveries"
down_revision = "0002_identity_indexes"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "platform_deliveries",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column(
            "session_id", sa.String(100), sa.ForeignKey("platform_sessions.id"), nullable=False
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("lease_until", sa.Integer, nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_index("ix_platform_deliveries_session_id", "platform_deliveries", ["session_id"])


def downgrade():
    op.drop_table("platform_deliveries")
