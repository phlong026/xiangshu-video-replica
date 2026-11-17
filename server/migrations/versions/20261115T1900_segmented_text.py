"""持久化分段转写、长文档与逐段改写检查点。"""

import sqlalchemy as sa
from alembic import op

revision = "20261115T1900_segmented_text"
down_revision = "20261115T1800_project_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("studio_saved_scripts", sa.Column("text_document_json", sa.Text()))
    op.add_column("studio_saved_scripts", sa.Column("original_document_json", sa.Text()))
    op.add_column("script_rewrite_tasks", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    for table in ("script_from_audio_tasks", "script_rewrite_tasks"):
        op.add_column(table, sa.Column("processing_policy_json", sa.Text()))
        op.add_column(
            table,
            sa.Column("processing_version", sa.Text(), nullable=False, server_default="legacy-v1"),
        )
        op.add_column(table, sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
        op.add_column(
            table, sa.Column("processing_phase", sa.Text(), nullable=False, server_default="QUEUED")
        )
    op.create_table(
        "text_documents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "char_count BETWEEN 0 AND 1000000 AND revision > 0", name="ck_text_document_budget"
        ),
    )
    op.create_index("ix_text_documents_project", "text_documents", ["project_id", "created_at"])
    op.create_table(
        "text_document_blocks",
        sa.Column(
            "document_id",
            sa.Text(),
            sa.ForeignKey("text_documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("block_index", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "block_index >= 0 AND length(text) <= 5000", name="ck_text_block_budget"
        ),
    )
    op.create_table(
        "asr_segments",
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("script_from_audio_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("segment_index", sa.Integer(), primary_key=True),
        sa.Column("core_start", sa.Float(), nullable=False),
        sa.Column("core_end", sa.Float(), nullable=False),
        sa.Column("input_start", sa.Float(), nullable=False),
        sa.Column("input_end", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PREPARING"),
        sa.Column("object_key", sa.Text()),
        sa.Column("provider_task_id", sa.Text()),
        sa.Column("billing_attempt_id", sa.Text()),
        sa.Column("result_json", sa.Text()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.Text()),
        sa.CheckConstraint(
            "core_start >= 0 AND core_end > core_start AND input_start <= "
            "core_start AND input_end >= core_end",
            name="ck_asr_segment_times",
        ),
        sa.CheckConstraint(
            "status IN "
            "('PREPARING','READY','SUBMITTING','POLLING','SUCCEEDED','FAILED','UNCERTAIN','CANCELLED')",
            name="ck_asr_segment_status",
        ),
    )
    op.create_index("ix_asr_segments_inflight", "asr_segments", ["status", "updated_at"])
    op.create_table(
        "rewrite_segments",
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("script_rewrite_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("segment_index", sa.Integer(), primary_key=True),
        sa.Column("stage", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.Text(), nullable=False, server_default="rewrite"),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("result_text", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="READY"),
        sa.Column("billing_attempt_id", sa.Text()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("error_code", sa.Text()),
        sa.CheckConstraint(
            "status IN ('READY','SUBMITTING','SUCCEEDED','FAILED','UNCERTAIN','CANCELLED')",
            name="ck_rewrite_segment_status",
        ),
    )

    # 清理凭据不随项目级联，避免用户删项目后留下暂存音轨。
    op.create_table(
        "media_temp_objects",
        sa.Column("object_key", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("storage_namespace", sa.Text(), nullable=False),
        sa.Column(
            "keep_until", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "text_task_retries",
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("billing_round", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    for table in ("text_documents", "asr_segments", "rewrite_segments", "media_temp_objects", "text_task_retries"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).fetchone():
            raise RuntimeError("长文档任务非空，拒绝丢弃交付与成本记录。")
    op.drop_table("media_temp_objects")
    op.drop_table("text_task_retries")
    op.drop_table("rewrite_segments")
    op.drop_table("asr_segments")
    op.drop_table("text_document_blocks")
    op.drop_table("text_documents")
    op.drop_column("studio_saved_scripts", "original_document_json")
    op.drop_column("studio_saved_scripts", "text_document_json")
    op.drop_column("script_rewrite_tasks", "next_attempt_at")
    for table in ("script_from_audio_tasks", "script_rewrite_tasks"):
        op.drop_column(table, "processing_policy_json")
        op.drop_column(table, "processing_phase")
        op.drop_column(table, "cancel_requested_at")
        op.drop_column(table, "processing_version")
