"""ADM-19 营销活动 v1：充值套餐加活动时效窗口。

套餐表已建模"付 X 得 Y 积分 + 折扣权益"（满赠语义天然成立）；本迁移补
``available_from`` / ``available_to`` 活动区间：客户充值页只在区间内展示，
区间外自动隐藏——限时满赠活动不再靠人工上下架。NULL 边界 = 该侧不限。
"""

from alembic import op

revision = "20261008T1500_recharge_package_window"
down_revision = "20261008T1400_publish_stats_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE recharge_packages "
        "ADD COLUMN available_from TIMESTAMPTZ, "
        "ADD COLUMN available_to TIMESTAMPTZ, "
        "ADD CONSTRAINT ck_recharge_package_window "
        "CHECK(available_from IS NULL OR available_to IS NULL "
        "OR available_to > available_from)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE recharge_packages DROP CONSTRAINT ck_recharge_package_window")
    op.execute("ALTER TABLE recharge_packages DROP COLUMN available_to, DROP COLUMN available_from")
