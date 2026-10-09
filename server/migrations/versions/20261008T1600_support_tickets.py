"""ADM-21 工单/反馈 v1（管理端代录）：support_tickets 表。

客户问题此前散落在微信群——无记录、无状态、无复盘。v1 形态为管理端代录
（owner 拍板默认：客户端提单入口后续再开）：登记问题（客户/类目/摘要）→
处理（负责人/结论文本）→ 状态机 open → processing → resolved。
状态迁移与内容修改均写审计 ``support_ticket.*``。
"""

from alembic import op

revision = "20261008T1600_support_tickets"
down_revision = "20261008T1500_recharge_package_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE support_tickets (
        id TEXT PRIMARY KEY,
        target_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
        source TEXT NOT NULL DEFAULT 'admin_report'
            CHECK(source IN ('admin_report','customer_feedback')),
        category TEXT NOT NULL CHECK(category IN
            ('billing','generation','publish','content','account','other')),
        summary TEXT NOT NULL CHECK(length(trim(summary)) BETWEEN 1 AND 500),
        status TEXT NOT NULL DEFAULT 'open'
            CHECK(status IN ('open','processing','resolved')),
        handler_user_id TEXT,
        resolution TEXT NOT NULL DEFAULT '' CHECK(length(resolution) <= 2000),
        created_by_user_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute(
        "CREATE INDEX support_tickets_status_idx ON support_tickets(status, created_at DESC)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE support_tickets")
