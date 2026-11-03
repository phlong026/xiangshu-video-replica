"""客户会话 7 天滑动有效期：last_seen_at 活动时间列。

2026-11-02 契约变更（替代 SES-01 的 90 秒租约 + 30 秒心跳）：lease_until
语义从「90 秒租约」换为「7 天滑动到期」，由 fence 校验时的节流续期
（renew_session_expiry）与登录/激活写入。lease_until 列、既有 CHECK 与
响应字段名全部不动——「会话是否有效」的判定（lease_until > now）在两种
契约下同构，旧客户端无感。

本迁移只补「最近活动」列：管理端「设备/会话在线」的判定口径从
「租约存活」（7 天契约下会恒真）改为 last_seen_at 10 分钟内有活动。
不回填历史行——上线瞬间存量 90 秒租约要么在最后一次请求里被滑动续期
（新代码写入 last_seen_at），要么在 90 秒内自然过期走重登，没有需要
保留的长尾数据。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261102T0900_customer_session_last_seen"
down_revision = "20261101T1000_video_long_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "customer_session_state",
        sa.Column("last_seen_at", sa.Text()),
    )
    op.create_check_constraint(
        "ck_customer_session_state_last_seen_not_before_created",
        "customer_session_state",
        "last_seen_at IS NULL OR last_seen_at::timestamptz >= created_at::timestamptz",
    )
    # 表按设备一行、量级为设备数，本可全表扫；建索引是为了让管理端
    # 「在线设备」过滤（last_seen_at > now() - 10 分钟）保持索引扫描，
    # 不随设备数增长拖慢控制台列表页。
    op.create_index(
        "ix_customer_session_state_last_seen_at",
        "customer_session_state",
        ["last_seen_at"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index("ix_customer_session_state_last_seen_at", table_name="customer_session_state")
    op.drop_constraint(
        "ck_customer_session_state_last_seen_not_before_created",
        "customer_session_state",
        type_="check",
    )
    op.drop_column("customer_session_state", "last_seen_at")
