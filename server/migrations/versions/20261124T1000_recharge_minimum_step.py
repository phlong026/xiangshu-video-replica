"""自定义充值改为 500 元起、每 100 元递增。

只调整运行配置及新配置行的默认值。套餐金额、到账积分和历史订单的
计价快照均保持原样；固定套餐仍由现有 package_snapshot_json 豁免步长。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261124T1000_recharge_minimum_step"
down_revision = "20261122T1000_replica_prompt_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "runtime_settings", "min_recharge_fen", existing_type=sa.Integer(), server_default="50000"
    )
    op.alter_column(
        "runtime_settings", "recharge_step_fen", existing_type=sa.Integer(), server_default="10000"
    )
    op.execute(
        "UPDATE runtime_settings SET min_recharge_fen=50000, recharge_step_fen=10000, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=1 "
        "AND (min_recharge_fen<>50000 OR recharge_step_fen<>10000)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 不能猜测升级前的运营配置，也不能覆盖升级后管理员的新调整。
    # 回退仅恢复建表默认值，已保存的充值规则和全部账目继续保留。
    op.alter_column(
        "runtime_settings", "min_recharge_fen", existing_type=sa.Integer(), server_default="10000"
    )
    op.alter_column(
        "runtime_settings", "recharge_step_fen", existing_type=sa.Integer(), server_default="1000"
    )
