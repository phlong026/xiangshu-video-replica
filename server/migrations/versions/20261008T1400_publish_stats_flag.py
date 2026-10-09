"""ADM-18 发布效果回流（默认关闭）：runtime_settings 加开关列。

客户发布成片后的公开互动数据（播放/点赞/评论/分享）回流进 publish_records
.stats，用于"爆款赋能效果"报表。采集客户发布内容涉及授权边界（2026-10-08
改进方案 §7 待拍板 3），因此实现完整链路但**默认关闭**，运营确认授权口径后
由超管在管理端打开。
"""

from alembic import op

revision = "20261008T1400_publish_stats_flag"
down_revision = "20261008T1300_viral_homepage_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE runtime_settings "
        "ADD COLUMN publish_stats_sync_enabled INTEGER NOT NULL DEFAULT 0 "
        "CHECK(publish_stats_sync_enabled IN (0,1))"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE runtime_settings DROP COLUMN publish_stats_sync_enabled")
