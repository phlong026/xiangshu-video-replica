"""为音轨暂存对象保留崩溃恢复与重试清理凭据。"""

from alembic import op

revision = "20261012T0600_viral_copy_upload_receipts"
down_revision = "20261012T0500_nullable_viral_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史离线备份工具也读取迁移链；客户运行态仍只使用 PostgreSQL。
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE viral_copy_upload_receipts (
            id text PRIMARY KEY,
            owner_user_id text NOT NULL,
            idempotency_key text NOT NULL,
            request_hash text NOT NULL,
            storage_namespace text NOT NULL,
            object_key text NOT NULL,
            state text NOT NULL DEFAULT 'STAGED'
                CHECK (state IN ('STAGED','PUBLISHED','CLEANUP','REAPED')),
            asset_id text,
            lease_expires_at timestamptz NOT NULL DEFAULT now() + interval '20 minutes',
            next_cleanup_at timestamptz NOT NULL DEFAULT now() + interval '20 minutes',
            cleanup_attempts integer NOT NULL DEFAULT 0,
            cleanup_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(storage_namespace, object_key)
        )
    """)
    op.execute("""CREATE INDEX viral_copy_upload_receipts_request
        ON viral_copy_upload_receipts(owner_user_id,idempotency_key,created_at)""")
    op.execute("""CREATE INDEX viral_copy_upload_receipts_cleanup
        ON viral_copy_upload_receipts(next_cleanup_at) WHERE state <> 'PUBLISHED'""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM viral_copy_upload_receipts WHERE state <> 'PUBLISHED') THEN
            RAISE EXCEPTION '暂存清理凭据尚需保留，不能丢弃';
        END IF;
    END $$""")
    op.execute("DROP TABLE viral_copy_upload_receipts")
