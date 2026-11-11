"""确认点进入时刻：replica_pipeline_tasks.awaiting_since.

评审 A-r1#12：72 小时确认期限不能依赖 ``updated_at``（挂起/恢复/观察等
动作都会刷新它，期限会被无限延长）。新增不可变语义的 ``awaiting_since``：
- COMPOSING 迁移到 AWAIT_CONFIRM 时写入 now()；
- 确认持锁重验时按它判过期；
- 独立过期扫描按它判到期。
存量 AWAIT_CONFIRM 行在迁移时用 updated_at 回填（功能未上线，数据量可控）。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261112T1000_replica_pipeline_awaiting_since"
down_revision = "20261111T1000_replica_pipeline_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "replica_pipeline_tasks",
        sa.Column("awaiting_since", sa.DateTime(timezone=True)),
    )
    op.execute(
        "UPDATE replica_pipeline_tasks SET awaiting_since = updated_at "
        "WHERE status = 'AWAIT_CONFIRM' AND awaiting_since IS NULL"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_column("replica_pipeline_tasks", "awaiting_since")
