"""爆款视频「推送到前端」（人工策展发布）.

新增列（``viral_videos``）：
- ``pushed_at`` —— 运营点击「推送到前端」且素材就绪后的上架时间（UTC ISO 文本）。
  客户可见口径据此豁免「发布时间 7 天窗口」：人工上架的内容展示多久由运营
  控制（下架走 visibility），不再随源站发布时间自然滚出列表。

Revision ID: 20261006T1200_viral_push_publish
Revises: 20261002T1015_merge_oral_compatibility_alerts
"""

import sqlalchemy as sa
from alembic import op

revision = "20261006T1200_viral_push_publish"
down_revision = "20261002T1015_merge_oral_compatibility_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — viral collection is a PG-only concern.
        return
    op.add_column("viral_videos", sa.Column("pushed_at", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("viral_videos", "pushed_at")
