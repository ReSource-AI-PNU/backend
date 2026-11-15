"""Organization membership and transactional session projections."""

import sqlalchemy as sa
from alembic import op

revision = "0002_identity_indexes"
down_revision = "0001_platform"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "platform_organizations",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("owner_id", sa.String(200), nullable=False, unique=True),
        sa.Column("biz_no", sa.String(20), nullable=False, unique=True),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_table(
        "platform_members",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column(
            "company_id", sa.String(100), sa.ForeignKey("platform_organizations.id"), nullable=False
        ),
        sa.Column("user_id", sa.String(200)),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("expires_at", sa.Integer),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.UniqueConstraint("company_id", "email"),
    )
    op.create_index("ix_platform_members_company_id", "platform_members", ["company_id"])
    op.create_index("ix_platform_members_user_id", "platform_members", ["user_id"])
    op.create_table(
        "platform_recipients",
        sa.Column(
            "session_id", sa.String(100), sa.ForeignKey("platform_sessions.id"), primary_key=True
        ),
        sa.Column("company_id", sa.String(100), primary_key=True),
        sa.Column("bundle_id", sa.String(100), primary_key=True),
    )
    op.create_index("ix_platform_recipients_company_id", "platform_recipients", ["company_id"])
    op.create_table(
        "platform_streams",
        sa.Column(
            "session_id", sa.String(100), sa.ForeignKey("platform_sessions.id"), primary_key=True
        ),
        sa.Column("stream_id", sa.String(100), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    # Backfill existing sessions without modifying their data or versions.
    bind = op.get_bind()
    companies = sa.table("platform_companies", sa.column("id"), sa.column("payload", sa.JSON))
    for cid, data in bind.execute(sa.select(companies.c.id, companies.c.payload)):
        if data.get("isDemo") and data.get("collector") and "minimumBasis" not in data["collector"]:
            data["collector"]["minimumBasis"] = "month"
            bind.execute(companies.update().where(companies.c.id == cid).values(payload=data))
    sessions = sa.table("platform_sessions", sa.column("id"), sa.column("payload", sa.JSON))
    recipients = sa.table(
        "platform_recipients",
        sa.column("session_id"),
        sa.column("company_id"),
        sa.column("bundle_id"),
    )
    streams = sa.table(
        "platform_streams",
        sa.column("session_id"),
        sa.column("stream_id"),
        sa.column("payload", sa.JSON),
    )
    for sid, payload in bind.execute(sa.select(sessions.c.id, sessions.c.payload)):
        for stream in payload["session"]["streams"]:
            bind.execute(
                streams.insert().values(session_id=sid, stream_id=stream["id"], payload=stream)
            )
        for b in payload.get("bundles", []):
            ids = {
                m["companyId"]
                for m in b["members"]
                if m["state"] in ["pending", "accepted", "declined"]
            }
            if b["ready"]:
                ids.add(b["collectorId"])
            for cid in ids:
                bind.execute(
                    recipients.insert().values(session_id=sid, company_id=cid, bundle_id=b["id"])
                )


def downgrade():
    for name in [
        "platform_streams",
        "platform_recipients",
        "platform_members",
        "platform_organizations",
    ]:
        op.drop_table(name)
