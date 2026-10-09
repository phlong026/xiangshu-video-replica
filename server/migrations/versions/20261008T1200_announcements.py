"""M2 公告与消息中心（ADM-09）：管理端发布、客户端展示与已读回执。

一对表：
- ``announcements``——运营公告（标题/正文/色调/生效区间/置顶），区间采用
  timestamptz，客户端按数据库时钟取"当前生效"集合；
- ``announcement_reads``——客户已读回执（主键 = 公告 × 用户），幂等插入，
  管理端据已读数评估触达效果。公告删除为软删（``deleted_at``），已读回执
  保留供复盘。
"""

from alembic import op

revision = "20261008T1200_announcements"
down_revision = "20261008T1100_refund_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE announcements (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 120),
        body TEXT NOT NULL CHECK(length(body) <= 4000),
        tone TEXT NOT NULL DEFAULT 'info' CHECK(tone IN ('info','maintenance','promo')),
        starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        ends_at TIMESTAMPTZ,
        pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
        created_by_user_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        deleted_at TIMESTAMPTZ
    )""")
    op.execute(
        "CREATE INDEX announcements_live_idx ON "
        "announcements(deleted_at, starts_at, ends_at, pinned DESC, created_at DESC)"
    )
    op.execute("""CREATE TABLE announcement_reads (
        announcement_id TEXT NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(announcement_id, user_id)
    )""")
    op.execute(
        "CREATE INDEX announcement_reads_ann_idx ON announcement_reads(announcement_id, read_at)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE announcement_reads")
    op.execute("DROP TABLE announcements")
