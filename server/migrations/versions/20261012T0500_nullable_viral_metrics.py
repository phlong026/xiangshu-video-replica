"""缺失互动统计保留未知值，不伪造为零。"""

from alembic import op

revision = "20261012T0500_nullable_viral_metrics"
down_revision = "20261012T0300_refund_source_allocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE viral_videos ALTER COLUMN likes DROP NOT NULL")
    op.execute("ALTER TABLE viral_videos ALTER COLUMN likes DROP DEFAULT")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 与非空约束恢复共用事务锁，不能在检查后并发写入未知值。
    op.execute("LOCK TABLE viral_videos IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM viral_videos WHERE likes IS NULL) THEN
            RAISE EXCEPTION '存在未知互动统计，不能无损降级';
        END IF;
    END $$""")
    op.execute("ALTER TABLE viral_videos ALTER COLUMN likes SET DEFAULT 0")
    op.execute("ALTER TABLE viral_videos ALTER COLUMN likes SET NOT NULL")
