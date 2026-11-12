"""全自动复刻总开关：runtime_settings.replica_pipeline_enabled.

方案 §8 裁决「总开关不进 viral_runtime_controls（固定列结构）」。runtime_settings
本身就是固定列结构的开关家目录（fair_queue_enabled / publish_stats_sync_enabled
等先例），在此加列符合现有设置域惯例，且控制台快照读取路径现成。

默认 false：随发布灰度打开。关闭语义（方案 §6）：拒绝新发起、后台推进挂起
（drive_pipeline_once 检查并 mark_suspended）；已入队子任务的供应商调用点
fencing 见 M6b 迭代登记。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261112T1100_replica_pipeline_switch"
down_revision = "20261112T1000_replica_pipeline_awaiting_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "runtime_settings",
        sa.Column(
            "replica_pipeline_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_column("runtime_settings", "replica_pipeline_enabled")
