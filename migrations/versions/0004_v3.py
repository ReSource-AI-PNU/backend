"""설계안 v3 저장: 근거 레지스트리, JSON 레코드, 실행 기록.

미실행·미검증: 작성한 환경에서 alembic과 sqlalchemy를 불러올 수 없어 돌려 보지 못했다.

PostGIS와 pgvector 확장은 PostgreSQL일 때만 켠다. 서버에 설치돼 있지 않은 확장은 건너뛴다
(CI의 postgis 이미지에는 pgvector가 없다). 설치돼 있는데 켜지 못하면 마이그레이션이 실패한다.
v3 테이블은 JSON 타입만 써서 두 확장 없이도 만들어진다.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_v3"
down_revision = "0003_deliveries"
branch_labels = depends_on = None

EXTENSIONS = ("postgis", "vector")


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        available = sa.text("SELECT 1 FROM pg_available_extensions WHERE name = :name")
        for name in EXTENSIONS:
            if bind.execute(available, {"name": name}).first() is not None:
                op.execute(f"CREATE EXTENSION IF NOT EXISTS {name}")
    op.create_table(
        "v3_evidence",
        sa.Column("id", sa.String(300), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("as_of", sa.String(10), nullable=False),
        sa.Column("valid_until", sa.String(10), nullable=True),
        sa.Column("access", sa.JSON, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_index("ix_v3_evidence_kind", "v3_evidence", ["kind"])
    op.create_table(
        "v3_records",
        sa.Column("kind", sa.String(30), primary_key=True),
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_table(
        "v3_events",
        sa.Column("session_id", sa.String(100), primary_key=True),
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )


def downgrade():
    # 확장은 다른 테이블(platform_companies.location)이 쓰므로 지우지 않는다.
    op.drop_table("v3_events")
    op.drop_table("v3_records")
    op.drop_table("v3_evidence")
