"""M1 退款最小版（ADM-08）：退付执行登记表。

账本侧退款（REFUND_APPROVAL 反向调账冲减积分）在 admin_adjustments 已闭环；
SOP §10 要求「调账 + ZPay 后台人工退付」双事实对齐——此前退付执行结果
（渠道 / 第三方退款单号 / 时间 / 经办人）只能留在聊天记录里，事后无从对账。
本表一行对应一笔退款调账的退付执行事实，主键即调账 id：一笔调账至多一次
退付登记，重放按幂等键由写契约挡在门外。

关联约束指向 admin_adjustments(id)；调账表是 append-only（039 触发器），
不会出现悬挂引用被删的窗口。
"""

from alembic import op

revision = "20261008T1100_refund_executions"
down_revision = "20261008T1000_viral_cohort_import_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE refund_executions (
        adjustment_id TEXT PRIMARY KEY
            REFERENCES admin_adjustments(id) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK(channel IN
            ('wechat_manual','offline_transfer','other')),
        provider_ref TEXT NOT NULL,
        executed_by_user_id TEXT NOT NULL,
        executed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        note TEXT NOT NULL DEFAULT ''
    )""")
    op.execute("CREATE INDEX refund_executions_executed_at_idx ON refund_executions(executed_at)")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE refund_executions")
