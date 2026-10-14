"""管理端整改 T29：客户运营参数持久化。

runtime_settings 追加两列，客户列表「余额不足」与客户运营「健康分」共用：
- ``customer_low_balance_threshold``：余额低于该值的客户算「余额不足」，同时是健康分
  「余额充足」加分的门槛（默认 100 积分，与此前写死的口径一致）；
- ``customer_health_alert_score``：健康分低于该值的客户标红提示运营介入（默认 40）。

此前阈值只能在客户列表手填、健康分写死在 SQL 里，两页口径各自为政。
只追加列与 CHECK，不改已发布 revision。
"""

from alembic import op

revision = "20261014T1100_customer_ops_params"
down_revision = "20261014T1000_support_ticket_priority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE runtime_settings ADD COLUMN customer_low_balance_threshold "
        "INTEGER NOT NULL DEFAULT 100 CONSTRAINT "
        "ck_runtime_settings_low_balance_threshold CHECK "
        "(customer_low_balance_threshold BETWEEN 0 AND 1000000)"
    )
    op.execute(
        "ALTER TABLE runtime_settings ADD COLUMN customer_health_alert_score "
        "INTEGER NOT NULL DEFAULT 40 CONSTRAINT "
        "ck_runtime_settings_health_alert_score CHECK "
        "(customer_health_alert_score BETWEEN 1 AND 100)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE runtime_settings DROP COLUMN customer_health_alert_score")
    op.execute("ALTER TABLE runtime_settings DROP COLUMN customer_low_balance_threshold")
