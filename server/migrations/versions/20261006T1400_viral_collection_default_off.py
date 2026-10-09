"""定时采集默认停用（人工采集 + 推送到前端，2026-10-06 拍板）.

新库 ``collection_enabled`` 直接落 0（开关只停关键词定时采集；人工搜索/
转存/推送不受影响）。存量库不回写——已显式配置过的环境由运营自行关闭，
避免发版即改变生产行为。

Revision ID: 20261006T1400_viral_collection_default_off
Revises: 20261006T1200_viral_push_publish
"""

from alembic import op

revision = "20261006T1400_viral_collection_default_off"
down_revision = "20261006T1200_viral_push_publish"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 只改默认值（影响新库/重置场景）；已存在的单行配置保持运营当前选择。
    op.execute("ALTER TABLE viral_runtime_controls ALTER COLUMN collection_enabled SET DEFAULT 0")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE viral_runtime_controls ALTER COLUMN collection_enabled SET DEFAULT 1")
