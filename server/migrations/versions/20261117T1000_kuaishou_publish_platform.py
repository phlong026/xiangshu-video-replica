"""发布账号、扫码会话与发布记录的平台 CHECK 追加快手。

只扩发布域的三张表，爆款采集仍保持原三平台契约。沿用原始内联 CHECK
在 PostgreSQL 中生成的真实约束名；不改写历史迁移，也不删除账号或发布事实。
降级前必须处理所有快手行，包含解绑后 account_id 已为空的发布历史。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261117T1000_kuaishou_publish_platform"
down_revision = "20261116T1000_recharge_orders_closed_at"
branch_labels = None
depends_on = None

_PLATFORM_CHECKS = (
    ("publish_browser_accounts", "publish_browser_accounts_platform_check"),
    ("publish_browser_logins", "publish_browser_logins_platform_check"),
    ("publish_records", "publish_records_platform_check"),
)
_PREVIOUS_PLATFORMS = "platform IN ('douyin','wechat_channels','xiaohongshu')"
_PUBLISH_PLATFORMS = "platform IN ('douyin','wechat_channels','xiaohongshu','kuaishou')"


def upgrade() -> None:
    # 离线归档工具仍遍历此链；这三张表仅存在于 PostgreSQL 发布车道。
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, constraint in _PLATFORM_CHECKS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, _PUBLISH_PLATFORMS)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 先锁定全部发布表再检查，避免预检后并发写入快手行造成半途失败。
    # 三张表全部预检通过才替换 CHECK，不靠删除快手数据强行回退。
    op.execute(
        "LOCK TABLE publish_browser_accounts, publish_browser_logins, publish_records "
        "IN ACCESS EXCLUSIVE MODE"
    )
    for table, _ in _PLATFORM_CHECKS:
        pending = bind.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE platform = 'kuaishou')")
        ).scalar()
        if pending:
            raise RuntimeError(
                f"{table} 仍有快手数据，无法降级发布平台约束；"
                "请先受控归档并清理全部快手账号、扫码会话和发布记录，再重试降级。"
            )
    for table, constraint in _PLATFORM_CHECKS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, _PREVIOUS_PLATFORMS)
