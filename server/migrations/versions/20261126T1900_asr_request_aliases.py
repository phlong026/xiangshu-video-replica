"""持久化复用转写时返回的任务，防止请求重放创建新的收费任务。"""

import sqlalchemy as sa
from alembic import op

revision = "20261126T1900_asr_request_aliases"
down_revision = "20261125T1000_software_licenses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "script_from_audio_request_aliases",
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("script_from_audio_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("project_id", "idempotency_key"),
    )
    op.create_index(
        "idx_script_from_audio_alias_task", "script_from_audio_request_aliases", ["task_id"]
    )


def downgrade() -> None:
    op.drop_table("script_from_audio_request_aliases")
