"""설계안 v3 실시간 진행 기록(v3_activity).

에이전트 대화를 화면에 바로 보여 주려고 진행기가 쓰는 기록이다(app/v3/live.py, 계약 8장 "실시간 진행").
기록 하나가 트랜잭션 하나라 실행 중에도 다른 요청(GET /sessions/{id}/stream)이 곧바로 읽는다.
실행 자리(한 진단에 실행 하나)는 테이블을 따로 두지 않고 v3_records(kind="runs")에 둔다.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_v3_activity"
down_revision = "0005_v3_accounts"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "v3_activity",
        sa.Column("session_id", sa.String(100), primary_key=True),
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )


def downgrade():
    op.drop_table("v3_activity")
