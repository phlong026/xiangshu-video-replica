"""长视频复刻提示词（replica_prompt）持久化任务队列。

Revision ID: 20261016T1000_replica_prompt_tasks
Revises: 20261014T1200_admin_role_templates

与 analysis_tasks（4–15 秒复刻拆解）刻意保持两个任务族：长视频单次
provider 调用可达十分钟以上，需要独立的租约与超时参数；完成载荷也只有
提示词/口播/摘要，没有分镜卡。隔离演进，互不影响复刻付费主链路。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261016T1000_replica_prompt_tasks"
down_revision = "20261014T1200_admin_role_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "replica_prompt_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column(
            "result_version_id",
            sa.Text(),
            sa.ForeignKey("versions.id", ondelete="SET NULL"),
        ),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("failure_phase", sa.Text()),
        sa.Column("retryable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.Text()),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_replica_prompt_tasks_status",
        ),
        sa.CheckConstraint("duration_seconds > 0", name="ck_replica_prompt_tasks_duration"),
        sa.CheckConstraint("attempt >= 0", name="ck_replica_prompt_tasks_attempt"),
        sa.CheckConstraint("retryable IN (0, 1)", name="ck_replica_prompt_tasks_retryable"),
    )
    op.create_index(
        "idx_replica_prompt_tasks_claim",
        "replica_prompt_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "idx_replica_prompt_tasks_project_asset_created",
        "replica_prompt_tasks",
        ["project_id", "asset_id", "created_at", "id"],
    )
    op.create_index(
        "uq_replica_prompt_tasks_active_asset",
        "replica_prompt_tasks",
        ["project_id", "asset_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING', 'RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_replica_prompt_tasks_active_asset", table_name="replica_prompt_tasks")
    op.drop_index(
        "idx_replica_prompt_tasks_project_asset_created", table_name="replica_prompt_tasks"
    )
    op.drop_index("idx_replica_prompt_tasks_claim", table_name="replica_prompt_tasks")
    op.drop_table("replica_prompt_tasks")
