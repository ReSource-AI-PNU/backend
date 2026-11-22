"""근거 표에서 피드백 종류(meta.feedback.type)로 찾는 식 인덱스.

검수 결과 몇십 건을 찾으려고 근거 표 전체(json 열, 수천 줄)를 매번 파싱해 조회 한 번에 0.4초가 들었다
(app/v3/sql.py SqlEvidenceRegistry.list_feedback). 인덱스 식은 sql.FEEDBACK_TYPE_SQL과 글자까지 같아야 한다.
"""

from alembic import op

revision = "0007_v3_evidence_feedback_index"
down_revision = "0006_v3_activity"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_v3_evidence_feedback_type "
        "ON v3_evidence ((payload #>> '{meta,feedback,type}'))"
    )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_v3_evidence_feedback_type")
