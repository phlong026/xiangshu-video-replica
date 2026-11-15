"""卡单队列去重：唯一索引从 OPEN 扩到 OPEN+ESCALATED（审计修复 2026-11-15）。

原部分唯一索引只约束 state='OPEN'。条目升级为 ESCALATED 后即脱离索引保护，
入队扫描的下一次 INSERT 不再命中冲突目标，同一个 (service, source_id,
billing_round) 会被无限重建——生产上已出现单源 22774 条重复记录（2026-09-30
起的同一个卡单，每轮扫描插一行，新行继承计费行的 created_at 后首轮重查即再
升级，循环自我放大）。

本迁移先折叠存量重复（每个键保留 check_count 最高的一条，重查历史最多的
即是运营实际在盯的那条），再把唯一索引扩到全部"活跃"状态。键上仍只允许
一条活跃记录；RESOLVED 不受约束，新一轮预留照常另起条目。
"""

from alembic import op

revision = "20261115T1900_billing_reconciliation_active_dedupe"
down_revision = "20261113T1000_replica_pipeline_submit_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 滚动发布窗口：migrate 容器跑迁移时，旧镜像 worker 可能还在往队列写行。
    # DELETE 与 CREATE UNIQUE INDEX 之间若混入并发 INSERT（同键重复），建索引
    # 会因唯一冲突整体回滚。先拿 SHARE ROW EXCLUSIVE（阻塞其他会话的
    # INSERT/UPDATE/DELETE，放行 SELECT），锁随迁移事务持有到提交。
    # 旧镜像代码在锁释放后的混跑期仍可能以旧冲突目标 INSERT 同键行并撞新索引
    # 报唯一冲突——该轮对账事务会失败并由下一轮重试，属可接受的短窗口代价
    # （worker 镜像在同一发布内随即滚动）。
    op.execute("LOCK TABLE billing_reconciliation_items IN SHARE ROW EXCLUSIVE MODE")
    # 折叠存量重复：同一 (service, source_id, billing_round) 的活跃记录里保留
    # check_count 最高的一条（并列取最早创建、最小 id，保证确定性）。
    # RESOLVED 行不参与键约束，原样保留供明细审计。
    op.execute("""
        DELETE FROM billing_reconciliation_items a
        USING billing_reconciliation_items b
        WHERE a.state IN ('OPEN', 'ESCALATED')
          AND b.state IN ('OPEN', 'ESCALATED')
          AND a.service = b.service
          AND a.source_id = b.source_id
          AND a.billing_round = b.billing_round
          AND a.id <> b.id
          AND (b.check_count, b.created_at, b.id) >
              (a.check_count, a.created_at, a.id)
    """)
    op.execute("""CREATE UNIQUE INDEX billing_reconciliation_active_key
        ON billing_reconciliation_items(service, source_id, billing_round)
        WHERE state IN ('OPEN', 'ESCALATED')""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS billing_reconciliation_active_key")
    # 已折叠的重复记录不可恢复；降级只移除约束，不回灌数据。
