"""保存项目来源及原片与音频绑定，支持跨页面恢复。"""

import sqlalchemy as sa
from alembic import op

revision = "20261115T1800_project_sources"
down_revision = "20261113T1000_replica_pipeline_submit_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_sources",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("declared_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("declared_duration_seconds", sa.Float()),
        sa.Column("original_video_asset_id", sa.Text()),
        sa.Column("audio_source_asset_id", sa.Text()),
        sa.Column("verified_duration_seconds", sa.Float()),
        sa.Column("verified_sha256", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "revision > 0 AND declared_size_bytes > 0", name="ck_project_sources_budget"
        ),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_project_sources_request"),
    )
    op.create_index("ix_project_sources_project", "project_sources", ["project_id", "created_at"])
    op.add_column("projects", sa.Column("current_source_id", sa.Text()))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM project_sources LIMIT 1")).fetchone():
        raise RuntimeError("来源记录非空，拒绝删除可恢复来源，请先归档。")
    op.drop_column("projects", "current_source_id")
    op.drop_index("ix_project_sources_project", table_name="project_sources")
    op.drop_table("project_sources")
