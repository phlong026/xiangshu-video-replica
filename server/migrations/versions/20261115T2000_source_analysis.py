"""统一来源分析派发与原时间轴片段来源。"""

import sqlalchemy as sa
from alembic import op

revision = "20261115T2000_source_analysis"
down_revision = "20261115T1900_segmented_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_sources", sa.Column("parent_source_id", sa.Text()))
    op.add_column("project_sources", sa.Column("range_start_seconds", sa.Float()))
    op.add_column("project_sources", sa.Column("range_end_seconds", sa.Float()))
    op.create_table(
        "source_analysis_actions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("route", sa.Text()),
        sa.Column("task_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "owner_user_id", "idempotency_key", name="uq_source_analysis_request"),
        sa.CheckConstraint("status IN ('NEEDS_ORIGINAL_UPLOAD','NEEDS_RANGE_SELECTION','DISPATCHED')", name="ck_source_analysis_status"),
    )
    op.create_index("ix_source_analysis_source", "source_analysis_actions", ["source_id", "source_revision"])
    op.create_table(
        "source_trim_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("range_start", sa.Float(), nullable=False),
        sa.Column("range_end", sa.Float(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("asset_id", sa.Text()),
        sa.Column("result_source_id", sa.Text()),
        sa.Column("result_action_id", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.Text()),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "owner_user_id", "idempotency_key", name="uq_source_trim_request"),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')", name="ck_source_trim_status"),
        sa.CheckConstraint("range_start >= 0 AND range_end-range_start BETWEEN 3.9 AND 180.1", name="ck_source_trim_range"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM source_trim_tasks LIMIT 1")).fetchone():
        raise RuntimeError("视频选段任务非空，拒绝丢弃恢复凭据。")
    if op.get_bind().execute(sa.text("SELECT 1 FROM source_analysis_actions LIMIT 1")).fetchone():
        raise RuntimeError("来源分析动作非空，拒绝丢弃任务恢复凭据。")
    op.drop_table("source_trim_tasks")
    op.drop_table("source_analysis_actions")
    op.drop_column("project_sources", "range_end_seconds")
    op.drop_column("project_sources", "range_start_seconds")
    op.drop_column("project_sources", "parent_source_id")
