"""ADM-C07 首页容量配置化：viral_runtime_controls 增加 homepage_capacity。

此前容量在 homepage 端点硬编码 None（无上限），客户端首页推荐条数不受控。
运营按平台展示位约定配置上限后，首页编排页在超限时给出提示；未配置（NULL）
维持不限，行为向后兼容。
"""

from alembic import op

revision = "20261008T1300_viral_homepage_capacity"
down_revision = "20261008T1200_announcements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE viral_runtime_controls "
        "ADD COLUMN homepage_capacity INTEGER CHECK(homepage_capacity IS NULL "
        "OR homepage_capacity >= 1)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE viral_runtime_controls DROP COLUMN homepage_capacity")
