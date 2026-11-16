"""充值订单补 ``closed_at``：CLOSED 单自动对账（F3）的时间线锚点.

2026-11-16 漏账缺口修复（方案 v2 F3）：订单被置 CLOSED 后，网关侧二维码
可能仍可支付（zpay 无过期字段；微信存在查单/关单竞态与客户删除后 2h 窗口），
而此前对账只查 PENDING——关单后支付且回调丢失的订单没有任何补账路径。
sweep 的 CLOSED 扫描段需要知道「何时关的单」才能框定有界重试窗口；三个
关单入口（客户删除、sweep 定向关单、CLI 全量关单）在应用层补写本列，历史
存量保持 NULL，由独立追查批次处理（不拿 created_at 冒充，评审 Q4）。

语义（冻结）：仅首次 PENDING→CLOSED 写入；重复删除不刷新；晚到支付转
PAID 时保留原关单时间（关单事实不因结算而消失）。列型跟随本表既有时间列
（created_at/paid_at 均为 Text + CURRENT_TIMESTAMP 隐式转换，扫描谓词用
``closed_at::timestamptz`` 与 created_at 同一手法）；可空列——旧行为不受
影响，回滚只需删列。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261116T1000_recharge_orders_closed_at"
down_revision = "20261115T1000_display_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "recharge_orders",
        sa.Column("closed_at", sa.Text(), nullable=True),
    )
    # 存量回填（评审 Q4 的 NULL 历史处理）：客户删除留有审计行
    # （customer.recharge_order.closed，entity_id=商户单号），关单时间可从
    # 审计created_at 确定性地恢复；sweep/CLI 关单无审计行，历史 NULL 保持
    # NULL——这部分走独立只读追查批次（对账脚本核对网关），不拿 created_at
    # 冒充。audit_logs.created_at 同为 Text 时间列，直接取文本即可。
    op.get_bind().execute(
        sa.text(
            """
            UPDATE recharge_orders AS ro
            SET closed_at = audit.closed_at
            FROM (
                SELECT entity_id AS order_no,
                       max(created_at)::text AS closed_at
                FROM audit_logs
                WHERE action = 'customer.recharge_order.closed'
                  AND entity_id IS NOT NULL
                GROUP BY entity_id
            ) AS audit
            WHERE ro.merchant_order_no = audit.order_no
              AND ro.status = 'CLOSED'
              AND ro.closed_at IS NULL
            """
        )
    )
    # CLOSED 扫描段的候选谓词按 provider+关单时间框定，部分索引只覆盖未扫
    # 完的存量（status='CLOSED' 且已落 closed_at），不随历史 NULL 与终态行膨胀。
    op.create_index(
        "ix_recharge_orders_closed_scan",
        "recharge_orders",
        ["provider", "closed_at"],
        postgresql_where=sa.text("status = 'CLOSED' AND closed_at IS NOT NULL"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index("ix_recharge_orders_closed_scan", table_name="recharge_orders")
    op.drop_column("recharge_orders", "closed_at")
