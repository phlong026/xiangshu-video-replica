"""Studio 工作草稿墓碑：物理删除改为版本推进的软删除（T35）。

放弃草稿后迟到的旧 PUT 会把旧稿原样写回。墓碑行（清空 payload、版本 +1）
让带旧版本号的条件写必然错位 409，也让删除动作第一次拥有可传递给客户端的
版本号——「真正的新稿」凭它复活，「迟到旧写」被拒之门外。

跟随 067 的双方言写法：草稿表由双方言存储共享，时间戳沿用文本列，
deleted_at 取值由应用层写入微秒文本（_utc_now_text）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261013T1010_studio_draft_tombstones"
down_revision = "20261013T1000_billing_reconciliation_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("studio_drafts", sa.Column("deleted_at", sa.Text(), nullable=True))


def downgrade() -> None:
    # downgrade 会丢弃「哪些草稿曾被放弃」的墓碑事实；已放弃的草稿在降级后
    # 表现为不存在（与升级前的物理删除语义一致），不丢任何未放弃内容。
    op.drop_column("studio_drafts", "deleted_at")
