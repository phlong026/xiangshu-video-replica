"""卡单积分核对队列：按任务与计费轮次跟踪未闭合预留（T40）。

证据门禁会按设计把部分预留留在 PENDING（成功缺时长、口播缺交付证据、
发布事务未收口、提交结果未知），队列把这些卡单变成可追踪、可升级、
可审计的条目；部分唯一索引保证同一 (service, source_id, billing_round)
同时只有一条 OPEN 记录。
"""

from alembic import op

revision = "20261013T1000_billing_reconciliation_items"
down_revision = "20261012T0600_viral_copy_upload_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史离线备份工具也读取迁移链；客户运行态仍只使用 PostgreSQL。
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE billing_reconciliation_items (
            id text PRIMARY KEY,
            service text NOT NULL,
            source_kind text NOT NULL,
            source_id text NOT NULL,
            billing_operation_id text,
            billing_round integer NOT NULL DEFAULT 1,
            owner_user_id text,
            reason text NOT NULL,
            detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            provider_receipt_id text,
            state text NOT NULL DEFAULT 'OPEN'
                CHECK (state IN ('OPEN','ESCALATED','RESOLVED')),
            check_count integer NOT NULL DEFAULT 0,
            last_checked_at timestamptz,
            escalated_at timestamptz,
            resolution text,
            resolution_note text,
            resolved_by text,
            resolved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    # 同一计费轮次同时只允许一条 OPEN 条目；收口后同键可再入队（新一轮预留）。
    op.execute("""CREATE UNIQUE INDEX billing_reconciliation_open_key
        ON billing_reconciliation_items(service, source_id, billing_round)
        WHERE state = 'OPEN'""")
    op.execute("""CREATE INDEX billing_reconciliation_state_idx
        ON billing_reconciliation_items(state, created_at)""")
    op.execute("""CREATE INDEX billing_reconciliation_operation_idx
        ON billing_reconciliation_items(billing_operation_id)""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS billing_reconciliation_items")
