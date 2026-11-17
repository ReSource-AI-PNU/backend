"""설계안 v3 계정: 사용자, 조직 소속, 초대 (V3_AUTH=bff).

조직 레코드는 그대로 v3_records(kind="orgs")에 둔다. 비밀번호는 scrypt 값만, 초대 토큰은 sha256 값만
저장한다. 시각은 UTC ISO 글이다.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_v3_accounts"
down_revision = "0004_v3"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "v3_users",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(300), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "v3_memberships",
        sa.Column("user_id", sa.String(40), primary_key=True),
        sa.Column("org_id", sa.String(100), primary_key=True),
        sa.Column("member_role", sa.String(10), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_v3_memberships_org_id", "v3_memberships", ["org_id"])
    op.create_table(
        "v3_invites",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(100), nullable=False),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("used_at", sa.String(40), nullable=True),
        sa.Column("used_by", sa.String(40), nullable=True),
    )
    op.create_index("ix_v3_invites_org_id", "v3_invites", ["org_id"])


def downgrade():
    op.drop_table("v3_invites")
    op.drop_table("v3_memberships")
    op.drop_table("v3_users")
