"""外部发布结果未知治理（T23）：提交标记、稳定请求标识与未知状态。

发布记录此前没有「结果未知」语义：worker 在投递中崩溃后超租约一律重排
queued 重投，平台可能已接收却又投一次（重复发表）。本迁移加三件事：

- status 增加 'submission_unknown'：提交已发出但结果未知，终态，只允许
  人工核对收敛，绝不自动重投；
- submitted_at：worker 调平台 I/O 前打的提交标记——超租约时「没打过标记」
  说明请求从未发出（提交前，可安全重排），「打过标记」说明平台可能已收到
  （提交后，转未知）；
- submission_key：每次领取生成一个稳定外部请求标识，供人工到平台核对时
  对账（供应商是否已有去重机制须另验，不在本批承诺）。

已发布链只追加不改写；downgrade 要求库里没有 submission_unknown 行。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261013T1020_publish_submission_safety"
down_revision = "20261013T1010_studio_draft_tombstones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE publish_records DROP CONSTRAINT publish_records_status_check")
    op.execute("""ALTER TABLE publish_records ADD CONSTRAINT publish_records_status_check
        CHECK (status IN ('queued','publishing','submission_unknown','published',
                          'failed','cancelled'))""")
    op.execute("ALTER TABLE publish_records ADD COLUMN submitted_at timestamptz")
    op.execute("ALTER TABLE publish_records ADD COLUMN submission_key text")
    # T27：客户统计同步的尝试预算。worker 失败路径整事务回滚（含计费轮次
    # insert），预算必须落在自己的列上才不会被回滚吞掉。
    op.execute(
        "ALTER TABLE publish_records ADD COLUMN sync_attempts integer NOT NULL DEFAULT 0 "
        "CHECK (sync_attempts >= 0)"
    )
    # 未知记录的核对队列视图：按卡住时间倒序扫这张小分区。
    op.execute("""CREATE INDEX idx_publish_records_unknown
        ON publish_records (updated_at DESC) WHERE status = 'submission_unknown'""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 有未核对完的 submission_unknown 行时降级会丢「待核对」事实，先拒绝；
    # 运营把队列清空（人工收敛）后才能降级。
    pending = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM publish_records WHERE status = 'submission_unknown'")
        )
        .scalar()
    )
    if pending:
        raise RuntimeError("存在未核对的 submission_unknown 发布记录，先人工收敛再降级")
    op.execute("DROP INDEX IF EXISTS idx_publish_records_unknown")
    op.execute("ALTER TABLE publish_records DROP COLUMN sync_attempts")
    op.execute("ALTER TABLE publish_records DROP COLUMN submission_key")
    op.execute("ALTER TABLE publish_records DROP COLUMN submitted_at")
    op.execute("ALTER TABLE publish_records DROP CONSTRAINT publish_records_status_check")
    op.execute("""ALTER TABLE publish_records ADD CONSTRAINT publish_records_status_check
        CHECK (status IN ('queued','publishing','published','failed','cancelled'))""")
